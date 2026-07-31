"""The four enforcement guards and the live-only hook observer.

Their *configuration* vocabulary (``Budget``, ``PermissionPolicy``,
``RepetitionPolicy``, ``PreToolUseRule``) lives in
:mod:`noeta.runtime.governance` instead: the kernel's builder signature and
the SDK host both speak it, so it cannot sit behind the plugin loader's
doorway. :func:`build_default_guards` is the guards factory the kernel builder
calls with the assembly outputs, so the kernel never imports a guard class.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from noeta.builtins.governance.impl.budget import BudgetGuard
from noeta.builtins.governance.impl.hook_guard import HookGuard
from noeta.builtins.governance.impl.hook_observer import (
    HookObserver,
    NotificationRule,
    PostToolUseRule,
    make_subprocess_runner,
)
from noeta.builtins.governance.impl.permission import PermissionGuard
from noeta.builtins.governance.impl.repetition import RepetitionGuard
from noeta.core.hooks import HookManager
from noeta.protocols.hooks import Guard
from noeta.protocols.tool import Tool
from noeta.runtime.governance import (
    Budget,
    PermissionPolicy,
    PreToolUseRule,
    RepetitionAction,
    RepetitionPolicy,
    SkillGuardFacts,
)


__all__ = [
    "BudgetGuard",
    "HookGuard",
    "HookObserver",
    "NotificationRule",
    "PermissionGuard",
    "PostToolUseRule",
    "RepetitionGuard",
    "build_default_guards",
    "make_subprocess_runner",
]


def build_default_guards(
    *,
    tools: dict[str, Tool],
    budget: Budget,
    require_approval_tools: tuple[str, ...],
    shell_approval_predicate: Optional[Callable[[str, Mapping[str, Any]], bool]],
    guard_facts: Optional[Any],
    allowed_subtask_agents: Optional[frozenset[str]],
    repetition_threshold: int,
    repetition_action: RepetitionAction,
    repetition_window: int,
    hooks_pre_tool_use: tuple[PreToolUseRule, ...],
    extra_guards: tuple[Guard, ...],
) -> HookManager:
    """The default guard HookManager, in registration order.

    Registration order is load-bearing: a resumed Engine rebuilds the guard
    shape from this one function, so guard-origin events (an approval suspend,
    a deny) only reproduce if the order matches. Every input arrives
    pre-shaped — ``tools`` is the finished assembly,
    ``allowed_subtask_agents`` is already delegation-gated, and
    ``guard_facts`` is the skills pack's :class:`SkillGuardFacts` bundle
    forwarded opaquely; ``None`` means every skill-derived field keeps its
    off/empty default.
    """
    facts = guard_facts if guard_facts is not None else SkillGuardFacts()
    hooks = HookManager()
    hooks.register(BudgetGuard(budget=budget))
    hooks.register(
        PermissionGuard(
            policy=PermissionPolicy(
                allowed_tools=frozenset(tools),
                require_approval_tools=frozenset(
                    n for n in require_approval_tools if n in tools
                ),
                conditional_approval=shell_approval_predicate,
                skill_tool_enforcement=facts.tool_enforcement,
                skill_allowed_tools=facts.allowed_tools,
                allowed_subtask_agents=allowed_subtask_agents,
                skill_script_tools=facts.script_tools,
                skill_scripts=facts.scripts,
            ),
            tools=tools,
        )
    )
    if repetition_threshold > 0:
        hooks.register(
            RepetitionGuard(
                RepetitionPolicy(
                    threshold=repetition_threshold,
                    action=repetition_action,
                    window=repetition_window,
                )
            )
        )
    # Hook rules are never recovered from the ledger: a resume that omits them
    # rebuilds no HookGuard and diverges from the recorded verdicts. The
    # HookObserver is deliberately not rebuilt — it is a live-only side-effect.
    if hooks_pre_tool_use:
        hooks.register(HookGuard(hooks_pre_tool_use))
    # User guards go after the built-in stack; ordering among themselves is the
    # caller's own, via each Guard's ``priority``.
    for guard in extra_guards:
        hooks.register(guard)
    return hooks
