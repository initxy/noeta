"""Governance vocabulary — the guard *configuration* types, kernel-side.

Microkernel M2: the enforcement classes (``BudgetGuard`` / ``PermissionGuard``
/ ``RepetitionGuard`` / ``HookGuard`` and the live-only ``HookObserver``) moved
into the ``governance`` built-in plugin
(``noeta.builtins.governance.impl``), resolved through the plugin loader and
injected into the kernel builder as a ``guards_factory``. What sinks here is
the **vocabulary** those factories are configured with — plain frozen
dataclasses and ``Literal`` enums with zero behaviour — because the kernel's
own interface (``build_session_inputs``'s parameters, the ``GuardsFactory``
Protocol) and the SDK host both speak it: the same shape precedent as
``noeta.runtime.workspace`` (``WorkspaceRoot`` / ``FsWriteMode``) and
``noeta.runtime.shell_policy`` (``ShellMode``) from M1.

Everything here was moved verbatim from ``noeta.guards.{budget,permission,
repetition,hook}`` (that package is gone); the semantics commentary that
belongs to an *enforcement decision* lives with the guard class in the impl.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Optional


__all__ = [
    "Budget",
    "HookAction",
    "KNOWN_RISK_LEVELS",
    "MATCH_STRING_CAP",
    "MatchArg",
    "PermissionPolicy",
    "PreToolUseRule",
    "RepetitionAction",
    "RepetitionPolicy",
    "RiskLevel",
    "SkillEnforcementMode",
]


# ---------------------------------------------------------------------------
# Budget (formerly noeta.guards.budget)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Budget:
    """Hard caps on a Task's resource consumption.

    All fields optional; ``None`` means "no cap on this dimension".
    Per-instance (the same Budget covers every Task the BudgetGuard
    sees in Phase 1); Phase 2 introduces per-task budgets through
    the Principal / Contract record.
    """

    max_iterations: Optional[int] = None
    max_tool_calls: Optional[int] = None
    max_cost_usd: Optional[float] = None
    max_spawned_subtasks: Optional[int] = None
    #: SR1 — maximum **child depth** allowed to be created. Root depth is 0;
    #: ``max_subtask_depth=1`` lets a root spawn a child (depth 1) but denies
    #: that child spawning a grandchild (depth 2). ``None`` = no depth cap
    #: (existing behaviour). Checked only for ``ProposedSpawnSubtask``.
    max_subtask_depth: Optional[int] = None


# ---------------------------------------------------------------------------
# Permission (formerly noeta.guards.permission)
# ---------------------------------------------------------------------------


RiskLevel = Literal["low", "medium", "high"]

#: The ordered risk scale — ``low`` < ``medium`` < ``high`` — the project-wide
#: canonical neutral vocabulary every production tool declares one of. The
#: PermissionGuard's fail-closed ceiling check indexes into this order.
KNOWN_RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high")


#: Phase 4.5 Issue B — skill ``allowed-tools`` enforcement mode.
#: ``off`` (default) disables the feature; ``approval`` gates an
#: out-of-grant call through the Issue A HITL approval path;
#: ``deny`` fails it closed.
SkillEnforcementMode = Literal["off", "approval", "deny"]


@dataclass(frozen=True, slots=True)
class PermissionPolicy:
    """Allow/deny rules for tools and subtask spawns."""

    allowed_tools: Optional[frozenset[str]] = None
    denied_tools: frozenset[str] = field(default_factory=frozenset)
    max_risk_level: Optional[RiskLevel] = None
    allowed_subtask_agents: Optional[frozenset[str]] = None
    # Phase 4.5 Issue A — tools that, when they would otherwise be
    # allowed, require an explicit human approval (HITL). The guard
    # returns ``require_approval`` (→ ``yield_for_human`` suspend) rather
    # than ``allow``. Hard-deny conditions (denylist / allowlist / risk
    # ceiling) still take precedence — there is no point asking a human
    # about a call the policy already forbids.
    require_approval_tools: frozenset[str] = field(default_factory=frozenset)
    # Per-CALL conditional approval predicate. Built ABOVE the guard (in
    # noeta-sdk, which can see tool semantics) and injected as a plain callable
    # so the guard stays ``protocols``-only. Given ``(tool_name, arguments)`` it
    # returns True iff THIS specific call needs human sign-off — used for
    # ``shell_run`` under default/acceptEdits: a command already in the effective
    # allowlist runs silently, an unknown one is gated through the Issue A HITL
    # path. ``None`` (every pre-feature path: CLI / bypass) ⇒ no
    # per-call gate, byte-identical to before. Excluded from eq/hash (it is a
    # closure that is never serialized).
    conditional_approval: Optional[
        Callable[[str, Mapping[str, Any]], bool]
    ] = field(default=None, compare=False)
    # Phase 4.5 Issue B — skill ``allowed-tools`` enforcement.
    # ``skill_tool_enforcement`` is the mode (default ``off``).
    # ``skill_allowed_tools`` is a plain immutable
    # ``(skill_name, frozenset_of_neutral_noeta_tool_names)`` map — the
    # grants are **already parsed and alias-resolved** by noeta-sdk
    # (``noeta.policies.skill_tools.resolve_skill_allowed_tools``), which
    # knows both the product (Claude) and neutral Noeta vocabularies. The
    # guard stores the resolved map directly and never imports any product
    # vocabulary (preserving the guard impl's protocols-only diet AND
    # keeping the kernel free of provider tool-name opinion). A declaring
    # skill that resolved to an empty grant still appears here (fail-safe:
    # enforcement stays ON for that skill and it grants nothing).
    skill_tool_enforcement: SkillEnforcementMode = "off"
    skill_allowed_tools: tuple[tuple[str, frozenset[str]], ...] = ()
    # Phase 4.5 Issue E — skill-bundled script execution.
    # ``skill_script_tools`` names the exec tools whose execution is an
    # always-approval **guard invariant** (e.g. ``{"run_skill_script"}``):
    # such a call is gated by a fail-closed precheck → ``require_approval``
    # there, never falling through to ``allow`` and not relying on
    # ``require_approval_tools``. ``skill_scripts`` is the discovered
    # ``(skill, relpath)`` set the precheck validates against (the tool
    # gets the richer ``(skill, relpath, root_path)`` map separately).
    skill_script_tools: frozenset[str] = field(default_factory=frozenset)
    skill_scripts: frozenset[tuple[str, str]] = field(
        default_factory=frozenset
    )


# ---------------------------------------------------------------------------
# Repetition (formerly noeta.guards.repetition)
# ---------------------------------------------------------------------------


#: D-4b — what to do once the loop is detected. ``require_approval`` (default)
#: routes through the existing HITL approval suspend; ``deny`` fails the call
#: closed (with a model-visible result so the model can self-correct).
RepetitionAction = Literal["require_approval", "deny"]


@dataclass(frozen=True, slots=True)
class RepetitionPolicy:
    """Tuning for the ``RepetitionGuard``.

    ``threshold`` is the number of **consecutive** identical calls (counting
    the proposed one) that trips the action. ``threshold <= 0`` disables the
    guard entirely (a defensive off switch). ``window`` bounds how far back the
    Engine scans when building the history; it is an upper bound on the
    consecutive run the guard can observe.
    """

    #: Consecutive identical-call count (incl. the proposed call) that trips.
    #: Default 3 per D-4. ``<= 0`` disables the guard.
    threshold: int = 3
    #: Action on trip. Default ``require_approval`` per D-4 (decision to human).
    action: RepetitionAction = "require_approval"
    #: How many recent tool calls the Engine should fold into the history. Must
    #: be ``>= threshold`` for the guard to ever trip; defaulted generously.
    window: int = 8


# ---------------------------------------------------------------------------
# PreToolUse hook rules (formerly noeta.guards.hook)
# ---------------------------------------------------------------------------


#: Cap on the length of a string before ``contains`` / ``regex`` is
#: evaluated — bounds pathological-regex / huge-argument cost.
MATCH_STRING_CAP = 64 * 1024

HookAction = Literal["allow", "deny", "require_approval"]


@dataclass(frozen=True, slots=True)
class MatchArg:
    """A deterministic predicate on one tool argument.

    ``path`` is a tuple of **object keys** (dotted in the config, e.g.
    ``("opts", "force")``) — no array indexing. Exactly one operator is
    set. ``equals`` is structural/scalar JSON equality; ``contains`` /
    ``regex`` apply only when the resolved value is a ``str`` (else: no
    match). ``pattern`` is the regex compiled at parse time (fail-fast)."""

    path: tuple[str, ...]
    op: Literal["equals", "contains", "regex"]
    value: Any = None
    pattern: Optional[re.Pattern[str]] = None


@dataclass(frozen=True, slots=True)
class PreToolUseRule:
    match_tool: str
    action: HookAction
    match_arg: Optional[MatchArg] = None
    reason: Optional[str] = None
