"""Typed-payload restore table shared by the persistent EventLog adapters.

Every SQL-backed EventLog stores the envelope payload as canonical bytes
and must rebuild the typed payload dataclass on read.
``from_canonical_bytes`` restores nested ``__canonical_tag__``-bearing
values (``ContentRef``, ``Message``, ``WakeCondition``, ``SubtaskResult``,
``ContextPlan``, ``ViewSegment``) automatically; this table covers the
outer payload classes that do *not* carry a tag and would otherwise read
back as plain dicts.

Extracted from ``noeta.storage.sqlite.eventlog`` when the Postgres
adapter landed, so the two backends read from the single table instead
of drifting apart. A test in the contract suite reflects
``noeta.protocols.events`` and fails CI the moment a new ``*Payload``
class lands without a matching entry here.
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
    """Restore ``LLMRequestStarted`` tolerating MS1's optional ``selection``.

    Three deterministic shapes for ``selection``:
      * absent / ``None`` → ``None`` (pre-MS1 old-shape payload);
      * an already-typed :class:`MessageSelection` (the normal read
        path: ``from_canonical_bytes`` rehydrates the tagged value before
        this restorer runs) → kept as-is;
      * a plain (untagged) dict — old-ish / handwritten / fixture bodies →
        rebuilt from the fixed five fields; a missing required key is a
        ``KeyError`` (fail loud — a malformed body is not silently dropped).
    Any other shape fails loud.
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
            # ③ (D-3f): additive prune/summarize counters — ``.get`` so a
            # pre-③ dict body restores with the byte-safe defaults.
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
    """Restore ``LLMRequestFinished`` tolerating the optional ``usage`` added in foundation phase A.

    Three deterministic shapes for ``usage`` (mirrors the selection
    three-state restorer above):
      * absent / ``None`` → empty ``Usage()`` (old-shape payload from
        before foundation phase A — the dataclass default also covers this,
        but we are explicit so the intent survives refactors);
      * an already-typed :class:`Usage` (the normal read path:
        ``from_canonical_bytes`` does NOT rehydrate ``Usage`` because it
        carries no tag, so in practice this is the dict branch — kept for
        symmetry / defensiveness) → kept as-is;
      * a plain (untagged) dict — the stored canonical body → rebuilt
        from its stored fields. Unknown keys (e.g. a legacy bare-dict
        ``input_tokens`` / ``total_tokens`` from a hand-written fixture)
        are dropped rather than crashing ``Usage(**d)``.
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
    "TaskCreated":         lambda d: TaskCreatedPayload(**d),
    "TaskStarted":         lambda d: TaskStartedPayload(**d),
    "TaskStatePatched":    lambda d: TaskStatePatchedPayload(**d),
    "MessagesAppended":    lambda d: MessagesAppendedPayload(**d),
    "TaskSnapshot":        lambda d: TaskSnapshotPayload(**d),
    "TaskRewound":         lambda d: TaskRewoundPayload(**d),
    "StepAttemptAbandoned": lambda d: StepAttemptAbandonedPayload(**d),
    "ContextPlanComposed": lambda d: ContextPlanComposedPayload(**d),
    "TaskCompleted":       lambda d: TaskCompletedPayload(**d),
    "TaskFailed":          lambda d: TaskFailedPayload(**d),
    "ToolCallStarted":     lambda d: ToolCallStartedPayload(**d),
    "ToolResultRecorded":  lambda d: ToolResultRecordedPayload(**d),
    "ToolCallFinished":    lambda d: ToolCallFinishedPayload(**d),
    "SubtaskSpawned":      lambda d: SubtaskSpawnedPayload(**d),
    "StepTransitionMarked": lambda d: StepTransitionMarkedPayload(**d),
    "CompactionRequested": lambda d: CompactionRequestedPayload(**d),
    "Compacted":           lambda d: CompactedPayload(**d),
    "SubtaskCompleted":    lambda d: SubtaskCompletedPayload(**d),
    "SubtaskDenied":       lambda d: SubtaskDeniedPayload(**d),
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
    # ``restore_dataclass`` (not ``**d``) so an old recording that still
    # carries the retired verify-era ``*_fingerprint`` keys folds/resumes
    # instead of crashing on an unexpected keyword (R1 tolerance).
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
    "BackgroundSubagentStarted":   lambda d: BackgroundSubagentStartedPayload(**d),
    "BackgroundSubagentDelivered": lambda d: BackgroundSubagentDeliveredPayload(**d),
}


def _restore_payload(event_type: str, body: Any) -> Any:
    restorer = _PAYLOAD_RESTORERS.get(event_type)
    if restorer is None:
        # Forward-compatibility: an event type we don't yet know about
        # passes through as the canonical dict. New typed payload
        # classes must register here; the contract suite enforces it.
        return body
    return restorer(body)


def _enforce_payload_cap(task_id: str, event_type: str, body: bytes) -> None:
    if len(body) > EVENT_PAYLOAD_MAX_BYTES:
        raise PayloadTooLarge(
            f"task_id={task_id}, type={event_type}, "
            f"size={len(body)}, cap={EVENT_PAYLOAD_MAX_BYTES} "
            "(large bodies must go through ContentStore)"
        )
