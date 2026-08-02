"""``AuditObserver`` — project EventLog envelopes into a caller-supplied sink.

Every payload class in ``noeta.protocols.events`` must be classified into
either :data:`_SUMMARY_FIELDS_BY_EVENT` (value-level field allowlist) or
:data:`_TYPE_ONLY_EVENTS` (type + field names only), so an unclassified payload
cannot quietly leak user content through the forward-compatible
``_summarize_fallback`` path; the reflection guard in
:mod:`tests.test_audit_observer` fails when a type is missing from both or
present in both. Content is only ever referenced, never inlined: ``ContentRef``
values are flattened to ``{hash, size, media_type}`` and the body is never read
from the ContentStore. An internal lock serialises ``sink(record)`` calls,
because callbacks fire post-COMMIT outside the EventLog writer lock and
applications may pass a sink that is not thread-safe.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, fields
from typing import Any, Callable, Optional

from noeta.protocols.event_log import EventLogSubscriber, subscribe_with_stop
from noeta.protocols.events import EventEnvelope, MessageSelection
from noeta.protocols.values import ContentRef


__all__ = ["AuditObserver", "AuditRecord", "AuditSink"]


_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """Sink-facing projection of a single EventLog envelope.

    Mirrors the whole :class:`EventEnvelope` metadata footprint so a downstream
    sink (file, SIEM, OTel exporter) can dedup, reconstruct causality, and
    diagnose schema drift without querying the EventLog back. Only
    ``payload_summary`` is filtered.
    """

    id: str
    task_id: str
    seq: int
    type: str
    schema_version: int
    occurred_at: float
    actor: str
    trace_id: str
    correlation_id: str
    causation_id: Optional[str]
    origin: str
    payload_summary: dict[str, Any]


AuditSink = Callable[[AuditRecord], None]


# Event types whose listed payload fields may be surfaced as values. A field
# reaches the audit projection only by appearing here **by name**; everything
# else is dropped, so omission is how a field is banned. The fields kept out on
# purpose all carry user or model content: TaskCreated.goal/inputs,
# TaskCompleted.answer, TaskStatePatched.patch (those three types are
# type-only below), ToolCallStarted.arguments,
# ToolCallApprovalRequested.arguments, the MessagesAppended message bodies, and
# the LLM request/response bodies. A ContentRef listed here projects to
# ``{hash, size, media_type}``, never the body behind it.
_SUMMARY_FIELDS_BY_EVENT: dict[str, tuple[str, ...]] = {
    "TaskStarted":         ("lease_id",),
    "ToolCallStarted":     ("call_id", "tool_name"),
    "ToolResultRecorded":  ("call_id", "success", "summary", "output_ref"),
    "ToolCallFinished":    ("call_id",),
    "ToolCallDenied":      ("call_id", "tool_name", "reason"),
    "ToolCallApprovalRequested": ("call_id", "tool_name"),
    "ToolCallApprovalResolved":  (
        "call_id", "tool_name", "approved", "reason", "resolver",
    ),
    "UserQuestionRequested": (
        "question_id", "call_id", "questions_ref", "question_count", "reason",
    ),
    "UserQuestionAnswered": (
        "question_id", "call_id", "answers_ref", "answer_count", "answered_by",
    ),
    "SubtaskSpawned":      ("subtask_id", "agent_name"),
    "SubtaskCompleted":    ("subtask_id",),
    "SubtaskDenied":       ("agent_name", "reason"),
    "TaskSuspended":       ("reason",),
    "TaskWoken":           (),
    "TaskFailed":          ("reason", "retryable"),
    "TaskCancelled":       ("reason", "cascade"),
    "ModelBound":          ("model", "principal_identity", "provider"),
    "AgentBound":          ("agent_name",),
    "TaskHostBound":       ("host_id",),
    "ConversationClosed":  ("closed_by", "reason"),
    "ConversationReopened": ("reopened_by", "reason"),
    "TurnInterrupted":     ("interrupted_by", "reason"),
    "StepTransitionMarked": ("reason", "attempt"),
    "ToolSchemaRecorded":  ("tool_name", "version", "schema_hash"),
    "SkillContentRecorded": ("skill_name", "version", "content_hash"),
    "ContextContentRecorded": ("kind", "name", "version", "content_hash", "policy"),
    "CompactionRequested": ("reason", "estimated_tokens"),
    "Compacted":           ("summary_ref", "boundary_count", "replaced_count", "composer_version"),
    "LeaseGranted":        ("lease_id", "worker_id", "expires_at"),
    "LLMRequestStarted":   ("call_id", "model", "request_ref", "selection"),
    "LLMResponseRecorded": ("call_id", "stop_reason", "response_ref"),
    "AssistantThinkingRecorded": ("call_id", "thinking_ref", "block_count"),
    "LLMRequestFinished":  ("call_id", "success", "cost_usd"),
    "LLMRetryScheduled":   ("call_id", "attempt", "max_retries", "delay_seconds", "category", "error"),
    "BackgroundShellStarted": ("job_id", "command", "spawned_by_task_id", "pid", "ref"),
    "BackgroundShellPolled":  ("job_id", "ref", "offset"),
    "BackgroundShellExited":  ("job_id", "exit_code", "final_ref", "summary"),
    "BackgroundShellKilled":  ("job_id", "signal"),
    "BackgroundShellLost":    ("job_id",),
    "BackgroundSubagentStarted":   ("subtask_id", "agent_name", "goal", "call_id"),
    "BackgroundSubagentDelivered": ("subtask_id", "status", "result_ref", "summary"),
    # ``alias`` is a bare connector name and ``servers`` is the credential-free
    # provenance record — neither carries a url or a token.
    "McpServerSkipped":    ("alias", "reason"),
    "McpProvenanceRecorded": ("servers",),
    "ContextPlanComposed": ("plan_ref",),
    "MessagesAppended":    ("count", "messages_ref"),
    "InjectionRequested":  ("injection_id", "count", "messages_ref"),
    "TaskSnapshot":        ("state_ref",),
    "TaskRewound":         ("target_seq", "state_ref"),
    "StepAttemptAbandoned": ("abandoned_from_seq", "state_ref", "reason"),
    "TaskForked":          ("source_task_id", "source_seq", "state_ref"),
}


# Payloads dominated by user content, where a structural projection
# (``{"_type": ..., "fields": [...]}``) is the most that can safely be emitted.
_TYPE_ONLY_EVENTS: frozenset[str] = frozenset(
    {
        "TaskCreated",
        "TaskStatePatched",
        "TaskCompleted",
    }
)


class AuditObserver:
    """Subscribes to an EventLog and projects each envelope to a sink.

    The default sink logs at ``INFO`` with a structured
    ``extra={"audit": record}``; a caller wanting the records elsewhere injects
    its own :data:`AuditSink`.
    """

    name = "audit"

    def __init__(
        self,
        *,
        event_log: EventLogSubscriber,
        sink: Optional[AuditSink] = None,
    ) -> None:
        self._sink = sink if sink is not None else _default_logging_sink
        self._lock = threading.Lock()
        self._handle = subscribe_with_stop(event_log, self._on_event)

    def stop(self) -> None:
        self._handle.stop()

    def _on_event(self, env: EventEnvelope) -> None:
        record = AuditRecord(
            id=env.id,
            task_id=env.task_id,
            seq=env.seq,
            type=env.type,
            schema_version=env.schema_version,
            occurred_at=env.occurred_at,
            actor=env.actor,
            trace_id=env.trace_id,
            correlation_id=env.correlation_id,
            causation_id=env.causation_id,
            origin=env.origin,
            payload_summary=_summarize(env.type, env.payload),
        )
        try:
            with self._lock:
                self._sink(record)
        except Exception:  # noqa: BLE001 — Observer must not break writer
            _log.exception("AuditObserver sink raised")


def _default_logging_sink(record: AuditRecord) -> None:
    _log.info(
        "audit %s seq=%d task=%s type=%s",
        record.id,
        record.seq,
        record.task_id,
        record.type,
        extra={"audit": record},
    )


def _summarize(event_type: str, payload: Any) -> dict[str, Any]:
    """Project ``payload`` to a sink-safe dict; unclassified types lose all
    values and degrade to shape."""
    if event_type in _SUMMARY_FIELDS_BY_EVENT:
        return _summarize_whitelisted(event_type, payload)
    if event_type in _TYPE_ONLY_EVENTS:
        return _summarize_type_only(payload)
    return _summarize_fallback(payload)


def _summarize_whitelisted(event_type: str, payload: Any) -> dict[str, Any]:
    allowed = _SUMMARY_FIELDS_BY_EVENT[event_type]
    out: dict[str, Any] = {}
    for name in allowed:
        if not hasattr(payload, name):
            continue
        value = getattr(payload, name)
        out[name] = _flatten_value(value)
    return out


def _summarize_type_only(payload: Any) -> dict[str, Any]:
    type_name = type(payload).__name__
    field_names = _field_names(payload)
    return {"_type": type_name, "fields": list(field_names)}


def _summarize_fallback(payload: Any) -> dict[str, Any]:
    """Unreachable while the classification guard holds; kept so that an
    unclassified payload degrades to field names and type names rather than
    leaking values."""
    type_name = type(payload).__name__
    if isinstance(payload, dict):
        return {
            "_type": "dict",
            "fields": [{k: type(v).__name__} for k, v in payload.items()],
        }
    if hasattr(payload, "__dataclass_fields__"):
        return {
            "_type": type_name,
            "fields": [
                {f.name: type(getattr(payload, f.name)).__name__}
                for f in fields(payload)
            ],
        }
    return {"_type": type_name, "fields": []}


def _flatten_value(value: Any) -> Any:
    """Reduce a payload field value to a sink-safe representation.

    Only ``ContentRef`` and ``MessageSelection`` are narrowed; every other value
    passes through as-is, so keeping a body out of the projection is the
    allowlist's job, not this function's.
    """
    if isinstance(value, ContentRef):
        return {
            "hash": value.hash,
            "size": value.size,
            "media_type": value.media_type,
        }
    if isinstance(value, MessageSelection):
        # Fixed five scalar fields rather than ``dataclasses.asdict``: a generic
        # "flatten any dataclass" rule would splat whatever large or sensitive
        # dataclass someone allowlists next.
        return {
            "strategy": value.strategy,
            "candidates": value.candidates,
            "selected": value.selected,
            "dropped": value.dropped,
            "limit": value.limit,
        }
    return value


def _field_names(payload: Any) -> tuple[str, ...]:
    if hasattr(payload, "__dataclass_fields__"):
        return tuple(f.name for f in fields(payload))
    if isinstance(payload, dict):
        return tuple(payload.keys())
    return ()
