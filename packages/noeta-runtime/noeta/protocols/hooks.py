"""Hook system protocol layer.

There are only two hook roles in Noeta — ``Guard`` (sync,
three action points, returns one of three ``Verdict`` values) and
``Observer`` (async, subscribes to the EventLog). Phase 0 issue 05
ships the Guard half; Observer machinery beyond the inline child-
completion observer lands in Phase 1.

A Guard inspects a ``ProposedAction`` and returns a ``Verdict``. The
HookManager (in ``noeta.core.hooks``) is the single place that runs all
registered guards in priority order and decides what to do with the
combined outcome. Engine never touches a Guard directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol, Union

from noeta.protocols.decisions import SpawnSubtaskDecision, ToolCall
from noeta.protocols.task import GovernanceState


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


class Verdict(Enum):
    """Tri-state outcome of a Guard check.

    ``require_approval`` is mapped by Engine to
    ``yield_for_human`` (i.e. HITL is the carrier for approval). There is
    deliberately no ``ApprovalRequested / ApprovalGranted /
    ApprovalRejected`` event type.
    """

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class VerdictResult:
    """Verdict + an optional human-readable reason.

    Guards return one of these. ``HookManager.check`` returns the first
    non-allow result it sees (and a synthetic ALLOW with no reason when
    every guard allows).
    """

    verdict: Verdict
    reason: Optional[str] = None

    @classmethod
    def allow(cls) -> "VerdictResult":
        return cls(Verdict.ALLOW)

    @classmethod
    def deny(cls, reason: str) -> "VerdictResult":
        # A tool-call DENY always surfaces a failed ``ToolResultBlock`` to the
        # model (the kernel does this unconditionally in ``handle_tool_calls``)
        # so the message history stays balanced — no per-guard opt-in needed.
        return cls(Verdict.DENY, reason)

    @classmethod
    def require_approval(cls, reason: str) -> "VerdictResult":
        return cls(Verdict.REQUIRE_APPROVAL, reason)

    @property
    def is_allow(self) -> bool:
        return self.verdict is Verdict.ALLOW


# ---------------------------------------------------------------------------
# ProposedAction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposedToolCall:
    """The 'before_tool_call' Guard action point."""

    call: ToolCall


@dataclass(frozen=True, slots=True)
class ProposedSpawnSubtask:
    """The 'before_spawn_subtask' Guard action point."""

    decision: SpawnSubtaskDecision


@dataclass(frozen=True, slots=True)
class ProposedFinish:
    """The 'before_finish' Guard action point."""

    answer: Any


ProposedAction = Union[ProposedToolCall, ProposedSpawnSubtask, ProposedFinish]


# ---------------------------------------------------------------------------
# Guard context
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GuardContext:
    """Read-only context passed to every Guard check.

    Issue 18 added ``governance`` so built-in guards (BudgetGuard /
    PermissionGuard) can read the Task's folded resource counters
    without reaching outside the Guard surface. The Engine constructs
    each ``GuardContext`` by folding the EventLog prefix and passing
    a deepcopy of the resulting :class:`GovernanceState` — the
    snapshot is fully isolated from the live ``Task``, so a buggy
    Guard cannot perturb engine state by mutating fields on
    ``ctx.governance``.

    ``metadata`` remains a free-form bag for future fields (principal,
    trace_id) that later issues will populate.
    """

    task_id: str
    governance: GovernanceState = field(default_factory=GovernanceState)
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Phase 4.5 Issue B — the task's folded ``active_skills``
    #: (``TaskState.active_skills``). Defaulted so existing guards /
    #: constructions are unaffected. Engine fills it from the same
    #: ``fold`` it already runs for ``governance``, so a guard sees the
    #: identical active set in live and resume (skill
    #: ``allowed-tools`` enforcement is resume-safe by construction).
    active_skills: tuple[str, ...] = ()
    #: SR1 — the task's delegation depth (root=0, child=parent+1), folded
    #: from the genesis ``TaskCreated.subtask_depth``. Defaulted so existing
    #: guards / constructions are unaffected. The ``BudgetGuard``
    #: ``max_subtask_depth`` cap reads this; an explicit field (not a
    #: ``metadata`` key) keeps the depth seam typed and stable.
    subtask_depth: int = 0
    #: Work item ④ — the most recent tool calls as neutral identity keys
    #: ``(tool_name, canonical input bytes)`` in append order (oldest first).
    #: RepetitionGuard reads the tail of this run to detect a stuck loop
    #: (same name + same canonical arguments repeated). Engine fills it from a
    #: bounded scan of the recorded ``ToolCallStarted`` prefix, so the guard
    #: sees the identical history in live and resume. Ephemeral
    #: (never persisted to snapshot / event): defaulted to ``()`` so every
    #: existing GuardContext construction is byte-safe and unaffected.
    recent_tool_calls: tuple[tuple[str, bytes], ...] = ()


# ---------------------------------------------------------------------------
# Guard protocol
# ---------------------------------------------------------------------------


class Guard(Protocol):
    """A synchronous policy check at one of the three action points.

    The Guard reads the ``ProposedAction`` and ``GuardContext`` and
    returns a ``VerdictResult``. The Guard MUST NOT mutate either
    argument (single-writer invariant; mutation belongs to Policy /
    Composer, not to hooks).
    """

    name: str
    priority: int

    def check(
        self, action: ProposedAction, ctx: GuardContext
    ) -> VerdictResult: ...
