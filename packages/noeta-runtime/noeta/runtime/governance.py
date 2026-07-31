"""Governance vocabulary — the guard *configuration* types, kernel-side.

The guards that enforce these live in the ``governance`` built-in plugin and
reach the kernel as an injected ``guards_factory``; only the vocabulary sinks
in here, because the kernel's own interface (``build_session_inputs``, the
``GuardsFactory`` Protocol) and the SDK host must name the same fields.
Everything below is a frozen dataclass or a ``Literal`` with zero behaviour —
the reasoning behind an enforcement *decision* belongs with the guard class.
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
    "SkillGuardFacts",
]


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Budget:
    """Hard caps on a Task's resource consumption.

    ``None`` on a field means no cap on that dimension. One instance holds the
    caps for every Task its ``BudgetGuard`` sees; the counters they are checked
    against are each Task's own folded governance state.
    """

    max_iterations: Optional[int] = None
    max_tool_calls: Optional[int] = None
    max_cost_usd: Optional[float] = None
    max_spawned_subtasks: Optional[int] = None
    #: Maximum **child depth** allowed to be created. Root depth is 0, so
    #: ``max_subtask_depth=1`` lets a root spawn a child but denies that child
    #: a grandchild. Checked only for ``ProposedSpawnSubtask``.
    max_subtask_depth: Optional[int] = None


# ---------------------------------------------------------------------------
# Permission
# ---------------------------------------------------------------------------


RiskLevel = Literal["low", "medium", "high"]

#: The ordered risk scale every tool declares one of. The PermissionGuard's
#: fail-closed ceiling check indexes into this order, so the sequence — not
#: just the membership — is load-bearing.
KNOWN_RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high")


#: Skill ``allowed-tools`` enforcement mode. ``off`` disables the feature;
#: ``approval`` routes an out-of-grant call to a human; ``deny`` fails it
#: closed.
SkillEnforcementMode = Literal["off", "approval", "deny"]


@dataclass(frozen=True, slots=True)
class PermissionPolicy:
    """Allow/deny rules for tools and subtask spawns."""

    allowed_tools: Optional[frozenset[str]] = None
    denied_tools: frozenset[str] = field(default_factory=frozenset)
    max_risk_level: Optional[RiskLevel] = None
    allowed_subtask_agents: Optional[frozenset[str]] = None
    # Tools that need explicit human approval when they would otherwise be
    # allowed. Hard-deny conditions (denylist / allowlist / risk ceiling)
    # take precedence — there is no point asking a human about a call the
    # policy already forbids.
    require_approval_tools: frozenset[str] = field(default_factory=frozenset)
    # Per-CALL approval predicate: given ``(tool_name, arguments)``, True iff
    # THIS specific call needs human sign-off (``shell_run`` under
    # default/acceptEdits gates an unknown command but not an allowlisted
    # one). Built above the guard, where tool semantics are visible, and
    # injected as a plain callable so the guard stays ``protocols``-only.
    # Excluded from eq/hash: a closure is never serialized.
    conditional_approval: Optional[
        Callable[[str, Mapping[str, Any]], bool]
    ] = field(default=None, compare=False)
    # ``skill_allowed_tools`` maps a skill to neutral Noeta tool names that
    # are **already parsed and alias-resolved** by the skills built-in, which
    # is the only place that knows both the product and the neutral tool
    # vocabularies; the guard stores the resolved map so neither it nor the
    # kernel carries provider tool-name opinion. A declaring skill whose
    # grant resolved to empty still appears here — fail-safe, enforcement
    # stays on for it and it grants nothing.
    skill_tool_enforcement: SkillEnforcementMode = "off"
    skill_allowed_tools: tuple[tuple[str, frozenset[str]], ...] = ()
    # ``skill_script_tools`` names exec tools whose execution is an
    # always-approval **guard invariant** (e.g. ``{"run_skill_script"}``): a
    # fail-closed precheck answers ``require_approval`` directly, never
    # falling through to ``allow`` and never depending on
    # ``require_approval_tools`` being configured. ``skill_scripts`` is the
    # discovered ``(skill, relpath)`` set that precheck validates against.
    skill_script_tools: frozenset[str] = field(default_factory=frozenset)
    skill_scripts: frozenset[tuple[str, str]] = field(
        default_factory=frozenset
    )


@dataclass(frozen=True, slots=True)
class SkillGuardFacts:
    """Skill-derived guard inputs, bundled by the skills plugin's session pack.

    The skills built-in produces it (``PackContribution.guard_facts``) and the
    governance built-in's guards factory unpacks it into the
    :class:`PermissionPolicy` fields of the same names. The builder in between
    forwards it OPAQUELY, which is why the type lives here next to those
    fields rather than in the builder: the kernel's construction machinery
    carries no skill vocabulary.
    """

    tool_enforcement: SkillEnforcementMode = "off"
    allowed_tools: tuple[tuple[str, frozenset[str]], ...] = ()
    script_tools: frozenset[str] = frozenset()
    scripts: frozenset[tuple[str, str]] = frozenset()


# ---------------------------------------------------------------------------
# Repetition
# ---------------------------------------------------------------------------


#: What to do once a loop is detected. ``require_approval`` routes through the
#: human-approval suspend; ``deny`` fails the call closed with a model-visible
#: result so the model can self-correct.
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

    threshold: int = 3
    action: RepetitionAction = "require_approval"
    #: How many recent tool calls the Engine folds into the history. Must be
    #: ``>= threshold`` or the guard can never trip.
    window: int = 8


# ---------------------------------------------------------------------------
# PreToolUse hook rules
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
