"""``governance`` built-in — the default guard stack + hook observer (impl).

Microkernel M2: the four enforcement guards (budget / permission / repetition
/ hook) and the live-only ``HookObserver`` moved here from ``noeta.guards.*``
/ ``noeta.observers.hook``. Their *configuration* vocabulary (``Budget``,
``PermissionPolicy``, ``RepetitionPolicy``, ``PreToolUseRule``, the ``Literal``
enums) sank into :mod:`noeta.runtime.governance` — the kernel's builder
signature and the SDK host both speak it, so it cannot live behind the loader
doorway.

:func:`build_default_guards` is the **guards factory** the kernel builder
requires (``build_session_inputs(guards_factory=…)``): the SDK host resolves it
through the plugin loader (:func:`noeta.client.parts.default_guards_factory`)
and the kernel calls it with the assembly outputs (the built tool dict + the
skill-derived guard facts) plus the operator passthrough fields. The kernel
never imports a guard class.
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
    """The default guard HookManager in the live session's registration order.

    The moved body of the kernel builder's former ``_build_guards`` (Issue A):
    rebuild the exact guard shape the live session ran so a resumed Engine
    reproduces guard-origin events (the approval suspend +
    ``ToolCallApprovalRequested``, or a guard deny) consistently. Registration
    order mirrors the live runner. Every input arrives pre-shaped: ``tools``
    is the finished assembly, ``allowed_subtask_agents`` is already
    delegation-gated (``None`` when delegation is off, Issue C), and
    ``guard_facts`` is the skills pack's :class:`SkillGuardFacts` bundle
    (Issues B + E — grants already sdk-resolved), forwarded opaquely by the
    kernel builder; ``None`` (no skills pack in the session) means every
    skill-derived field keeps its off/empty default.
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
    # Work item ④: same anti-loop RepetitionGuard the live session wired
    # (same threshold/action/window, registered after Permission), so a
    # recording whose guard tripped reproduces its require_approval suspend /
    # deny byte-equal. Default threshold 0 ⇒ not registered (matches live).
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
    # F3: rebuild the deterministic PreToolUse HookGuard from the same
    # rules the live session used (same priority-after-built-ins), so a
    # recording with hook-origin deny/approval events reproduces
    # byte-equal. The operator must pass the same --hooks-file at resume;
    # omitting it leaves the guard unbuilt and the recording diverges
    # (we never recover hooks config from the recording). The HookObserver
    # is intentionally NOT rebuilt (live-only side-effect).
    if hooks_pre_tool_use:
        hooks.register(HookGuard(hooks_pre_tool_use))
    # SDK ``Options.guards`` extension point (T3): user-supplied Guards
    # register AFTER the built-in stack, in the order given (the caller owns
    # ordering via each Guard's own ``priority``). Empty ⇒ byte-identical to
    # the built-in-only path.
    for guard in extra_guards:
        hooks.register(guard)
    return hooks
