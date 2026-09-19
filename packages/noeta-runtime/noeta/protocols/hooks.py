"""Hook system protocol layer.

Noeta has exactly two hook roles: ``Guard`` — synchronous, three action points,
one of three ``Verdict`` values — and ``Observer``, which subscribes to the
EventLog. The Engine never touches a Guard directly: ``HookManager`` (in
``noeta.core.hooks``) is the single place that runs registered guards in
priority order and decides what their combined outcome means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional, Protocol, Union

from noeta.protocols.decisions import SpawnSubtaskDecision, ToolCall
from noeta.protocols.task import GovernanceState


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

    ``HookManager.check`` returns the first non-allow result it sees, or a
    synthetic ALLOW with no reason when every guard allows.
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


# -- approval identity ------------------------------------------------------
#
# A ``REQUIRE_APPROVAL`` verdict suspends the Task on
# ``HumanResponseReceived(handle="approval-{call_id}")`` and records the
# blocked action as a ``ToolCallApprovalRequested`` anchor under that same
# ``call_id``; ``approve`` / ``deny`` name the ``call_id`` to resolve it.
# ``before_tool_call`` already has an identifier to use — the model's own
# call_id. The two *decision* points have none, so they mint a reserved one
# derived from the Task id: derived, so any process can recompute it from the
# log, and unique, because a Task has at most one finish and one spawn in
# flight at a time.

#: Prefix of every approval suspend handle.
APPROVAL_WAKE_HANDLE_PREFIX = "approval-"

#: ``tool_name`` recorded on a gated finish / spawn anchor. These name the
#: guard action point, not a real tool: they are what the human-facing
#: surfaces that read the field (the SDK's ``can_use_tool`` callback, the
#: ``governance.approvals`` audit list) show for a gated decision.
FINISH_APPROVAL_TOOL = "finish"
SPAWN_APPROVAL_TOOL = "spawn_subtask"

#: Which of the two guard *decision* points an approval belongs to.
DecisionApprovalKind = Literal["finish", "spawn"]


def approval_wake_handle(call_id: str) -> str:
    """The suspend handle a pending approval on ``call_id`` waits on."""
    return f"{APPROVAL_WAKE_HANDLE_PREFIX}{call_id}"


def finish_approval_call_id(task_id: str) -> str:
    """The reserved ``call_id`` of a gated ``finish`` — handle
    ``approval-finish-{task_id}``."""
    return f"finish-{task_id}"


def spawn_approval_call_id(task_id: str) -> str:
    """The reserved ``call_id`` of a gated spawn — handle
    ``approval-spawn-{task_id}``."""
    return f"spawn-{task_id}"


def decision_approval_kind(
    call_id: str, task_id: str
) -> Optional[DecisionApprovalKind]:
    """Which guard decision point ``call_id`` names, or ``None`` when it is an
    ordinary gated tool call.

    Exact equality against the two derived ids, never a prefix test, so a
    model-minted call_id can never be mistaken for a decision approval.
    """
    if call_id == finish_approval_call_id(task_id):
        return "finish"
    if call_id == spawn_approval_call_id(task_id):
        return "spawn"
    return None


@dataclass(frozen=True, slots=True)
class GuardContext:
    """Read-only context passed to every Guard check.

    ``governance`` lets a guard read the Task's folded resource counters
    without reaching outside the Guard surface. The Engine builds each context
    by folding the EventLog prefix and passing a deepcopy of the resulting
    :class:`GovernanceState`, fully isolated from the live ``Task``, so a buggy
    Guard cannot perturb engine state by mutating ``ctx.governance``.
    ``metadata`` is a free-form bag.
    """

    task_id: str
    governance: GovernanceState = field(default_factory=GovernanceState)
    metadata: dict[str, Any] = field(default_factory=dict)
    #: The task's folded ``TaskState.active_skills``. The Engine fills it from
    #: the same ``fold`` it already runs for ``governance``, so a guard sees
    #: the identical active set live and on resume — skill ``allowed-tools``
    #: enforcement is resume-safe by construction.
    active_skills: tuple[str, ...] = ()
    #: Delegation depth (root=0, child=parent+1), folded from the genesis
    #: ``TaskCreated.subtask_depth``. An explicit field rather than a
    #: ``metadata`` key, so the depth seam stays typed for the ``BudgetGuard``
    #: ``max_subtask_depth`` cap.
    subtask_depth: int = 0
    #: The most recent tool calls as neutral identity keys ``(tool_name,
    #: canonical input bytes)``, oldest first, so ``RepetitionGuard`` can spot
    #: a stuck loop. The Engine fills it from a bounded scan of the recorded
    #: ``ToolCallStarted`` prefix, so the guard sees the identical history live
    #: and on resume; it is ephemeral and never persisted.
    recent_tool_calls: tuple[tuple[str, bytes], ...] = ()


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
