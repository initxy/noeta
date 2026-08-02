"""Typed-payload restore table shared by the persistent EventLog adapters.

A SQL-backed EventLog stores the envelope payload as canonical bytes and must
rebuild the typed payload dataclass on read. ``from_canonical_bytes`` handles
nested values that carry a ``__canonical_tag__``; this table covers the outer
payload classes, which carry no tag and would otherwise read back as plain
dicts. A reflection test over ``noeta.protocols.events`` fails the build the
moment a ``*Payload`` class has no entry here, so the table cannot fall behind
the event vocabulary.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from noeta.protocols.canonical import restore_dataclass
from noeta.protocols.errors import PayloadTooLarge
from noeta.protocols.events import (
    AssistantThinkingRecordedPayload,
    BackgroundShellExitedPayload,
    BackgroundShellKilledPayload,
    BackgroundShellLostPayload,
    BackgroundShellPolledPayload,
    BackgroundShellStartedPayload,
    BackgroundSubagentDeliveredPayload,
    BackgroundSubagentStartedPayload,
    CompactedPayload,
    CompactionRequestedPayload,
    ContextPlanComposedPayload,
    ConversationClosedPayload,
    ConversationReopenedPayload,
    AgentBoundPayload,
    LLMRequestFinishedPayload,
    LLMRetryScheduledPayload,
    TaskHostBoundPayload,
    LLMRequestStartedPayload,
    LLMResponseRecordedPayload,
    LeaseGrantedPayload,
    McpProvenanceRecordedPayload,
    McpServerSkippedPayload,
    MessageSelection,
    MessagesAppendedPayload,
    InjectionRequestedPayload,
    ModelBoundPayload,
    ContextContentRecordedPayload,
    SkillContentRecordedPayload,
    StepAttemptAbandonedPayload,
    StepTransitionMarkedPayload,
    SubtaskCompletedPayload,
    SubtaskDeniedPayload,
    SubtaskSpawnedPayload,
    TaskCancelledPayload,
    TaskCompletedPayload,
    TaskCreatedPayload,
    TaskFailedPayload,
    TaskForkedPayload,
    TaskRewoundPayload,
    TaskSnapshotPayload,
    TaskStartedPayload,
    TaskStatePatchedPayload,
    TaskSuspendedPayload,
    TaskWokenPayload,
    ToolCallApprovalRequestedPayload,
    ToolCallApprovalResolvedPayload,
    ToolCallDeniedPayload,
    ToolCallFinishedPayload,
    ToolCallStartedPayload,
    ToolResultRecordedPayload,
    ToolSchemaRecordedPayload,
    TurnInterruptedPayload,
    UserQuestionAnsweredPayload,
    UserQuestionRequestedPayload,
)
from noeta.protocols.messages import Usage
from noeta.protocols.values import EVENT_PAYLOAD_MAX_BYTES


__all__ = [
    "_PAYLOAD_RESTORERS",
    "_enforce_payload_cap",
    "_restore_payload",
]


def _restore_llm_request_started_payload(d: Any) -> LLMRequestStartedPayload:
    """Restore ``LLMRequestStarted``, tolerating three shapes of ``selection``.

    Absent, an already-typed :class:`MessageSelection` (what
    ``from_canonical_bytes`` hands back for a tagged value), or an untagged
    dict; anything else raises. A required key missing from the dict form is a
    ``KeyError``, so a malformed body fails loud instead of being dropped.
    """
    sel = d.get("selection")
    if sel is None:
        selection: Optional[MessageSelection] = None
    elif isinstance(sel, MessageSelection):
        selection = sel
    elif isinstance(sel, dict):
        selection = MessageSelection(
            strategy=sel["strategy"],
            candidates=sel["candidates"],
            selected=sel["selected"],
            dropped=sel["dropped"],
            limit=sel["limit"],
            # ``.get``: a stored body without these counters must restore at
            # the byte-safe defaults rather than fail the read.
            pruned=sel.get("pruned", 0),
            summarized=sel.get("summarized", 0),
        )
    else:
        raise TypeError(
            f"LLMRequestStarted.selection: unexpected shape {type(sel)!r}"
        )
    return LLMRequestStartedPayload(
        call_id=d["call_id"],
        model=d["model"],
        request_ref=d["request_ref"],
        input_tokens=d.get("input_tokens", 0),
        selection=selection,
    )


def _restore_llm_request_finished_payload(d: Any) -> LLMRequestFinishedPayload:
    """Restore ``LLMRequestFinished``, tolerating three shapes of ``usage``.

    ``Usage`` carries no canonical tag, so the live read path is always the
    untagged-dict branch; unknown keys in it are dropped rather than crashing
    ``Usage(**d)``, since a body carrying a field this reader does not know must
    still fold. A missing ``usage`` restores as an empty ``Usage()``.
    """
    raw = d.get("usage")
    if raw is None:
        usage = Usage()
    elif isinstance(raw, Usage):
        usage = raw
    elif isinstance(raw, dict):
        known = {
            "uncached",
            "cache_read",
            "cache_write",
            "output",
            "reasoning_tokens",
        }
        usage = Usage(**{k: v for k, v in raw.items() if k in known})
    else:
        raise TypeError(
            f"LLMRequestFinished.usage: unexpected shape {type(raw)!r}"
        )
    return LLMRequestFinishedPayload(
        call_id=d["call_id"],
        success=d["success"],
        cost_usd=d.get("cost_usd", 0.0),
        latency_ms=d.get("latency_ms", 0),
        usage=usage,
    )


_PAYLOAD_RESTORERS: dict[str, Callable[[Any], Any]] = {
    # ``restore_dataclass`` rather than ``**d`` wherever a stored body may carry
    # a key this reader does not know: fold and resume must survive it, not die
    # on an unexpected keyword argument.
    "TaskCreated":         lambda d: restore_dataclass(TaskCreatedPayload, d),
    "TaskStarted":         lambda d: TaskStartedPayload(**d),
    "TaskStatePatched":    lambda d: TaskStatePatchedPayload(**d),
    "MessagesAppended":    lambda d: MessagesAppendedPayload(**d),
    "InjectionRequested":  lambda d: restore_dataclass(InjectionRequestedPayload, d),
    "TaskSnapshot":        lambda d: TaskSnapshotPayload(**d),
    "TaskRewound":         lambda d: TaskRewoundPayload(**d),
    "StepAttemptAbandoned": lambda d: StepAttemptAbandonedPayload(**d),
    "TaskForked":          lambda d: TaskForkedPayload(**d),
    "TurnInterrupted":     lambda d: TurnInterruptedPayload(**d),
    "ContextPlanComposed": lambda d: ContextPlanComposedPayload(**d),
    "TaskCompleted":       lambda d: TaskCompletedPayload(**d),
    "TaskFailed":          lambda d: TaskFailedPayload(**d),
    "ToolCallStarted":     lambda d: ToolCallStartedPayload(**d),
    "ToolResultRecorded":  lambda d: ToolResultRecordedPayload(**d),
    "ToolCallFinished":    lambda d: ToolCallFinishedPayload(**d),
    "SubtaskSpawned":      lambda d: restore_dataclass(SubtaskSpawnedPayload, d),
    "StepTransitionMarked": lambda d: StepTransitionMarkedPayload(**d),
    "CompactionRequested": lambda d: CompactionRequestedPayload(**d),
    "Compacted":           lambda d: CompactedPayload(**d),
    "SubtaskCompleted":    lambda d: SubtaskCompletedPayload(**d),
    "SubtaskDenied":       lambda d: restore_dataclass(SubtaskDeniedPayload, d),
    "TaskSuspended":       lambda d: TaskSuspendedPayload(**d),
    "TaskWoken":           lambda d: TaskWokenPayload(**d),
    "ToolCallDenied":      lambda d: ToolCallDeniedPayload(**d),
    "ToolCallApprovalRequested": lambda d: ToolCallApprovalRequestedPayload(**d),
    "ToolCallApprovalResolved":  lambda d: ToolCallApprovalResolvedPayload(**d),
    "UserQuestionRequested": lambda d: UserQuestionRequestedPayload(**d),
    "UserQuestionAnswered": lambda d: UserQuestionAnsweredPayload(**d),
    "LLMRequestStarted":   lambda d: _restore_llm_request_started_payload(d),
    "LLMResponseRecorded": lambda d: LLMResponseRecordedPayload(**d),
    "AssistantThinkingRecorded": lambda d: AssistantThinkingRecordedPayload(**d),
    "LLMRequestFinished":  lambda d: _restore_llm_request_finished_payload(d),
    "LLMRetryScheduled":   lambda d: LLMRetryScheduledPayload(**d),
    "TaskCancelled":       lambda d: TaskCancelledPayload(**d),
    "ModelBound":          lambda d: ModelBoundPayload(**d),
    "AgentBound":          lambda d: restore_dataclass(AgentBoundPayload, d),
    "TaskHostBound":       lambda d: restore_dataclass(TaskHostBoundPayload, d),
    "ConversationClosed":  lambda d: ConversationClosedPayload(**d),
    "ConversationReopened": lambda d: ConversationReopenedPayload(**d),
    "LeaseGranted":        lambda d: LeaseGrantedPayload(**d),
    "ToolSchemaRecorded":  lambda d: ToolSchemaRecordedPayload(**d),
    "SkillContentRecorded": lambda d: SkillContentRecordedPayload(**d),
    "ContextContentRecorded": lambda d: ContextContentRecordedPayload(**d),
    "McpServerSkipped":    lambda d: McpServerSkippedPayload(**d),
    "McpProvenanceRecorded": lambda d: McpProvenanceRecordedPayload(**d),
    "BackgroundShellStarted": lambda d: BackgroundShellStartedPayload(**d),
    "BackgroundShellPolled":  lambda d: BackgroundShellPolledPayload(**d),
    "BackgroundShellExited":  lambda d: BackgroundShellExitedPayload(**d),
    "BackgroundShellKilled":  lambda d: BackgroundShellKilledPayload(**d),
    "BackgroundShellLost":    lambda d: BackgroundShellLostPayload(**d),
    "BackgroundSubagentStarted":   lambda d: restore_dataclass(
        BackgroundSubagentStartedPayload, d
    ),
    "BackgroundSubagentDelivered": lambda d: BackgroundSubagentDeliveredPayload(**d),
}


def _restore_payload(event_type: str, body: Any) -> Any:
    restorer = _PAYLOAD_RESTORERS.get(event_type)
    if restorer is None:
        # An unregistered event type passes through as the canonical dict
        # rather than failing the read; the contract suite is what guarantees
        # every typed payload class actually has an entry.
        return body
    return restorer(body)


def _enforce_payload_cap(task_id: str, event_type: str, body: bytes) -> None:
    if len(body) > EVENT_PAYLOAD_MAX_BYTES:
        raise PayloadTooLarge(
            f"task_id={task_id}, type={event_type}, "
            f"size={len(body)}, cap={EVENT_PAYLOAD_MAX_BYTES} "
            "(large bodies must go through ContentStore)"
        )
