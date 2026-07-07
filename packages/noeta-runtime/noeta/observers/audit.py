"""``AuditObserver`` — project EventLog envelopes into a sink.

Issue 19. Subscribes to an ``EventLogSubscriber`` and, for each
appended envelope, builds an :class:`AuditRecord` that projects the
full :class:`EventEnvelope` metadata plus a sink-safe
``payload_summary`` and hands it to a caller-supplied
:data:`AuditSink`.

The summary is intentionally **narrow**: every payload class in
``noeta.protocols.events`` must be explicitly classified into either
:data:`_SUMMARY_FIELDS_BY_EVENT` (value-level field allowlist) or
:data:`_TYPE_ONLY_EVENTS` (type + field-name list only). New event
types must consciously land in one of those buckets; the reflection
guard test in :mod:`tests.test_audit_observer` refuses to ship if a
type is missing or duplicated. Adding a new payload type without
classifying it cannot silently fall through to the forward-compat
``_summarize_fallback`` path.

``ContentRef`` values are flattened to ``{hash, size, media_type}``
dicts only — the body is never read from ``ContentStore``.

Thread-safety: subscriber callbacks fire post-COMMIT and outside the
EventLog writer lock; an internal ``threading.Lock`` serialises
``sink(record)`` invocations so applications can pass non-thread-safe
sinks.
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

    Mirrors the full :class:`EventEnvelope` metadata footprint so a
    downstream sink (file, SIEM, OTel exporter) has every field
    needed for dedup, causality reconstruction, and schema-evolution
    diagnosis without re-querying the EventLog. Only
    ``payload_summary`` filters envelope content — see ``_summarize``
    for the explicit allow/deny rules.
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


# Event types whose listed payload fields may be surfaced as values.
# **Explicit allowlist** (issue 19 B4): every field that should appear
# in the audit projection MUST be listed here by name — including
# ContentRef fields. ContentRef values are projected as
# ``{hash, size, media_type}`` only; the body is never read.
# Bans (issue 19 B2 rationale): TaskCreated.goal/inputs,
# TaskCompleted.answer, ToolCallStarted.arguments,
# TaskStatePatched.patch, MessagesAppended.messages body,
# LLMRequest/Response body. Those types either appear in
# ``_TYPE_ONLY_EVENTS`` below, or have their offending field
# deliberately omitted from this allowlist.
_SUMMARY_FIELDS_BY_EVENT: dict[str, tuple[str, ...]] = {
    "TaskStarted":         ("lease_id",),
    "ToolCallStarted":     ("call_id", "tool_name"),
    "ToolResultRecorded":  ("call_id", "success", "summary", "output_ref"),
    "ToolCallFinished":    ("call_id",),
    "ToolCallDenied":      ("call_id", "tool_name", "reason"),
    # Issue A: ``arguments`` is banned from the audit projection (same
    # rationale as ToolCallStarted.arguments) — record only the call's
    # identity. The resolution's governance fields are all safe.
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
    # Issue 06: the bound model + authorizing Principal identity are both
    # governance/identity strings (no user content) — safe to project so
    # the audit trail records every model binding/switch and who sanctioned
    # it.
    "ModelBound":          ("model", "principal_identity", "provider"),
    # the bound Agent name is a governance/identity
    # string (no user content) — safe to project so the audit trail records
    # which Agent identity drove the Task.
    "AgentBound":          ("agent_name",),
    # the server host id is a governance/identity string
    # (no secrets) — safe to project so the audit trail records which host
    # bound the Task.
    "TaskHostBound":       ("host_id",),
    # Issue 08: who closed/reopened the conversation + an optional human
    # note — governance/identity strings (no model output), safe to project
    # so the audit trail records the close/archive lifecycle.
    "ConversationClosed":  ("closed_by", "reason"),
    "ConversationReopened": ("reopened_by", "reason"),
    # Foundation B: the continuation reason + attempt are governance metadata (a
    # fixed Noeta-shape vocabulary, no user content) — safe to project so the
    # audit trail records WHY each step had a non-default next step.
    "StepTransitionMarked": ("reason", "attempt"),
    # per-component content-hash provenance — names,
    # declared versions and hashes are governance/identity strings (no
    # user content), safe to project so the audit trail records which
    # tool schemas / skill contents drove the Task.
    "ToolSchemaRecorded":  ("tool_name", "version", "schema_hash"),
    "SkillContentRecorded": ("skill_name", "version", "content_hash"),
    # generic content-channel provenance — kind/name/version/
    # hash/policy are governance/identity strings (no user content), safe
    # to project so the audit trail records which content drove the Task.
    "ContextContentRecorded": ("kind", "name", "version", "content_hash", "policy"),
    # (3) (D-3): compaction governance metadata — the neutral trigger reason,
    # token estimate, boundary counts, and the summary CONTENT REF (the
    # summary text itself stays behind the ref, never inlined here) — safe to
    # project so the audit trail records each compaction step.
    "CompactionRequested": ("reason", "estimated_tokens"),
    "Compacted":           ("summary_ref", "boundary_count", "replaced_count", "composer_version"),
    "LeaseGranted":        ("lease_id", "worker_id", "expires_at"),
    "LLMRequestStarted":   ("call_id", "model", "request_ref", "selection"),
    "LLMResponseRecorded": ("call_id", "stop_reason", "response_ref"),
    # Slice B: extended-thinking provenance — the keyed call_id, the content
    # ref the blocks live behind (never inlined), and the block count.
    "AssistantThinkingRecorded": ("call_id", "thinking_ref", "block_count"),
    "LLMRequestFinished":  ("call_id", "success", "cost_usd"),
    # a live transient-retry backoff — the retried call_id, attempt
    # counters, chosen delay, error category, and the provider's (truncated)
    # error string are all transport/governance metadata (no user content),
    # safe to project so the audit trail records each rate-limit stall.
    "LLMRetryScheduled":   ("call_id", "attempt", "max_retries", "delay_seconds", "category", "error"),
    # background-shell lifecycle — the job id, launched command,
    # launching task, pid, exit code, byte offset, summary, and the output
    # CONTENT REFS (bytes stay behind the refs, never inlined here) are all
    # governance metadata, safe to project so the audit trail records each
    # background process's start / poll / exit.
    "BackgroundShellStarted": ("job_id", "command", "spawned_by_task_id", "pid", "ref"),
    "BackgroundShellPolled":  ("job_id", "ref", "offset"),
    "BackgroundShellExited":  ("job_id", "exit_code", "final_ref", "summary"),
    "BackgroundShellKilled":  ("job_id", "signal"),
    # issue 06 — the orphan-recovery mark: just the job id (governance
    # metadata, no user content), safe to project so the audit trail records
    # each host-restart orphan reaping.
    "BackgroundShellLost":    ("job_id",),
    # background sub-agent lifecycle (docs/adr/background-subagent.md) — the
    # child subtask id, agent name, goal, terminal status, originating call_id,
    # and the result CONTENT REF (bytes stay behind the ref) are all governance
    # metadata, safe to project so the audit trail / front-end records each
    # background sub-agent's start + delivery.
    "BackgroundSubagentStarted":   ("subtask_id", "agent_name", "goal", "call_id"),
    "BackgroundSubagentDelivered": ("subtask_id", "status", "result_ref", "summary"),
    # an enabled MCP server was skipped at connect time. The
    # alias (a clean name, no url/token) + the typed fault reason string are
    # governance metadata (no user content), safe to project so the audit trail
    # / front-end records which connector failed and why.
    "McpServerSkipped":    ("alias", "reason"),
    # the per-task MCP provenance (enabled aliases + tool subsets,
    # names only, no credentials). Safe governance metadata: project the whole
    # credential-free ``servers`` record so the audit trail can answer "what
    # connectors + which tools did this task get".
    "McpProvenanceRecorded": ("servers",),
    "ContextPlanComposed": ("plan_ref",),
    "MessagesAppended":    ("count", "messages_ref"),
    "TaskSnapshot":        ("state_ref",),
    # conversation rewind baseline: the kept-through seq + the
    # snapshot-shaped state ref are both safe to audit (no user content).
    "TaskRewound":         ("target_seq", "state_ref"),
    # crash-recovery seal: the interrupted attempt's start seq, the
    # snapshot-shaped baseline ref and the machine reason — no user content.
    "StepAttemptAbandoned": ("abandoned_from_seq", "state_ref", "reason"),
}


# Event types where the audit projection is structural only:
# ``{"_type": "<ClassName>", "fields": [<field_names>]}`` — no value
# data. Use for payloads dominated by user content where shape audit
# is the most we can safely emit.
_TYPE_ONLY_EVENTS: frozenset[str] = frozenset(
    {
        "TaskCreated",
        "TaskStatePatched",
        "TaskCompleted",
    }
)


class AuditObserver:
    """Subscribes to an EventLog and projects each envelope to a sink.

    Default sink emits at ``INFO`` via stdlib ``logging`` with a
    structured ``extra={"audit": record}`` payload; tests / Phase 2
    backends inject a custom ``AuditSink`` to capture records inline.
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
        # EventLog ``_notify`` already swallows; defensive
        # catch logs but does not re-raise. Lock guards against
        # concurrent sink invocation from multiple writer threads.
        try:
            with self._lock:
                self._sink(record)
        except Exception:  # noqa: BLE001 — Observer must not break writer
            _log.exception("AuditObserver sink raised")


def _default_logging_sink(record: AuditRecord) -> None:
    """Default sink: structured INFO log via stdlib ``logging``."""
    _log.info(
        "audit %s seq=%d task=%s type=%s",
        record.id,
        record.seq,
        record.task_id,
        record.type,
        extra={"audit": record},
    )


def _summarize(event_type: str, payload: Any) -> dict[str, Any]:
    """Project ``payload`` to a sink-safe dict.

    Routes by event type into one of three modes:

    * In :data:`_SUMMARY_FIELDS_BY_EVENT`: emit just the listed fields;
      ``ContentRef`` values flatten to ``{hash, size, media_type}``.
    * In :data:`_TYPE_ONLY_EVENTS`: emit ``{_type, fields}`` only — no
      value data.
    * Unknown event type (forward-compat): emit ``{_type, fields}``
      where each field is reduced to a value-type name. Values never
      surface.
    """
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
    """Forward-compat fallback for event types that do not appear in
    either classification. We never reach here under the issue 19
    reflection guard, but the implementation is defensive."""
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

    ``ContentRef`` is projected to its three metadata fields only;
    ``MessageSelection`` (MS1) to its fixed five scalar fields; other
    values pass through. Whitelist callers are responsible for not listing
    fields that would carry sensitive bodies (the
    ``_SUMMARY_FIELDS_BY_EVENT`` comments enumerate the bans).
    """
    if isinstance(value, ContentRef):
        return {
            "hash": value.hash,
            "size": value.size,
            "media_type": value.media_type,
        }
    if isinstance(value, MessageSelection):
        # MS1: explicit fixed-field projection — deliberately NOT a broad
        # ``dataclasses.asdict``, so the audit projection never generalizes
        # to "flatten any dataclass" (which could splat a large/sensitive
        # dataclass once allowlisted). All five fields are scalars.
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
