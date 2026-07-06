"""OTLP trace export — the documented follow-on ``inner`` adapter for
:class:`noeta.observers.trace_export.TraceExportObserver`.

Ships the EventLog to any OTLP/HTTP collector (Jaeger, the OpenTelemetry
Collector, Langfuse behind a collector, …) as real spans. It consumes the
same :class:`~noeta.observers.audit.AuditRecord` allowlist projection as
the JSONL exporter — no raw goal / tool arguments / message bodies ever
leave the process — and synthesizes spans by pairing start/finish events:

* task span — ``TaskCreated``/``TaskStarted`` → ``TaskCompleted`` /
  ``TaskFailed`` / ``TaskCancelled``; ``TaskSuspended`` / ``TaskWoken``
  become span events, ``AgentBound`` names the span.
* tool span — ``ToolCallStarted`` → ``ToolCallFinished``, keyed on
  ``call_id``; ``ToolResultRecorded`` contributes success.
* llm span — ``LLMRequestStarted`` → ``LLMRequestFinished``, keyed on
  ``call_id``; ``LLMResponseRecorded`` contributes the stop reason.

Subtask spans join their parent's trace: ``SubtaskSpawned`` on the parent
stream links ``subtask_id`` → (parent trace, parent span), and the child's
task span is parented there, so a delegation tree renders as one waterfall.

Identity is deterministic (sha256 of stable keys): the trace id derives
from the envelope ``trace_id`` (falling back to ``correlation_id``, which
defaults to the root ``task_id``), span ids from ``task_id`` / ``call_id``.

Wire format: the OTLP/HTTP **JSON** encoding of
``ExportTraceServiceRequest`` (the proto3 JSON mapping — hex ids, stringed
uint64 nanos), POSTed to the configured ``/v1/traces`` URL. Hand-encoding
the JSON keeps this module free of any OpenTelemetry SDK dependency; the
only non-stdlib need is an HTTP POST, injected (tests) or defaulting to
``httpx`` (already a runtime dependency), imported lazily off the hot path.

Threading: the sink runs on the single :class:`AsyncTraceSink` worker
thread (records arrive serially, per-task in seq order), so the assembler
keeps plain dict state with no lock. Export failures are logged and
dropped — an unreachable collector must never break the run. Spans still
open at ``close()`` (e.g. a task suspended across process shutdown) are
dropped, not exported with a fake end time.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Callable, Mapping, Optional

from noeta.observers.audit import AuditRecord
from noeta.observers.trace_export import TraceExportObserver
from noeta.protocols.event_log import EventLogSubscriber


__all__ = [
    "OtlpHttpPost",
    "OtlpSpanSink",
    "OtlpTraceConfig",
    "make_otlp_trace_observer",
]


_log = logging.getLogger(__name__)

#: ``(url, body, headers) -> None`` — the injectable HTTP transport (tests
#: pass a fake; production leaves it ``None`` to use ``httpx``). Must raise
#: on transport/status failure; the sink logs and drops the batch.
OtlpHttpPost = Callable[[str, bytes, Mapping[str, str]], None]

_DEFAULT_BATCH_MAX = 64
_DEFAULT_FLUSH_INTERVAL_S = 2.0
#: Cap on span events (suspend/wake marks) kept per task span, so a
#: months-long conversation task cannot grow one span without bound.
_MAX_SPAN_EVENTS = 128

# OTLP enum values (proto3 JSON mapping accepts the integers).
_SPAN_KIND_INTERNAL = 1
_STATUS_UNSET = 0
_STATUS_OK = 1
_STATUS_ERROR = 2

_UNKNOWN_TRACE = "trace-unknown"


@dataclass(frozen=True)
class OtlpTraceConfig:
    """OTLP/HTTP trace-export wiring (host-level, never agent identity).

    ``endpoint`` is the **full** traces URL (e.g.
    ``http://localhost:4318/v1/traces``) — no path magic is applied here;
    resolving the OTel-standard env vars into a full URL is the caller's
    (app config layer's) job. ``headers`` ride on every export request
    (auth for hosted collectors); they never enter any recording.
    """

    endpoint: str
    headers: tuple[tuple[str, str], ...] = ()
    service_name: str = "noeta"


@dataclass
class _OpenSpan:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    start_ns: int
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    status_code: int = _STATUS_UNSET
    status_message: Optional[str] = None

    def finish(self, end_ns: int) -> dict[str, Any]:
        """Encode as an OTLP JSON span object."""
        span: dict[str, Any] = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "kind": _SPAN_KIND_INTERNAL,
            "startTimeUnixNano": str(self.start_ns),
            "endTimeUnixNano": str(max(end_ns, self.start_ns)),
            "attributes": [_kv(k, v) for k, v in sorted(self.attributes.items())],
        }
        if self.parent_span_id is not None:
            span["parentSpanId"] = self.parent_span_id
        if self.events:
            span["events"] = self.events
        status: dict[str, Any] = {}
        if self.status_code != _STATUS_UNSET:
            status["code"] = self.status_code
        if self.status_message:
            status["message"] = self.status_message
        if status:
            span["status"] = status
        return span


def _hex_id(key: str, nbytes: int) -> str:
    """Deterministic OTel id: the first ``nbytes`` of sha256, hex-encoded."""
    return sha256(key.encode("utf-8")).hexdigest()[: nbytes * 2]


def _kv(key: str, value: Any) -> dict[str, Any]:
    """Encode one attribute as an OTLP ``KeyValue`` (proto3 JSON mapping)."""
    if isinstance(value, bool):
        typed: dict[str, Any] = {"boolValue": value}
    elif isinstance(value, int):
        typed = {"intValue": str(value)}  # int64 → string per proto3 JSON
    elif isinstance(value, float):
        typed = {"doubleValue": value}
    else:
        typed = {"stringValue": str(value)}
    return {"key": key, "value": typed}


def _ns(occurred_at: float) -> int:
    return int(occurred_at * 1_000_000_000)


class _SpanAssembler:
    """Pair start/finish :class:`AuditRecord`\\ s into finished spans.

    Single-threaded by contract (see module docstring). ``feed`` returns
    the spans the record completed (usually zero or one), already encoded
    as OTLP JSON span objects.
    """

    def __init__(self) -> None:
        # task_id -> (trace_id_hex, parent_span_id_hex | None)
        self._task_trace: dict[str, tuple[str, Optional[str]]] = {}
        # subtask_id -> (trace_id_hex, parent task-span hex) from SubtaskSpawned
        self._pending_parent: dict[str, tuple[str, str]] = {}
        self._open_tasks: dict[str, _OpenSpan] = {}
        # (task_id, call_id) -> open tool/llm span
        self._open_calls: dict[tuple[str, str], _OpenSpan] = {}

    # -- linkage -----------------------------------------------------------

    def _linkage(self, record: AuditRecord) -> tuple[str, Optional[str]]:
        """The (trace id, parent span id) a span of ``record``'s task joins."""
        known = self._task_trace.get(record.task_id)
        if known is not None:
            return known
        pending = self._pending_parent.pop(record.task_id, None)
        if pending is not None:
            linkage: tuple[str, Optional[str]] = pending
        else:
            key = (
                record.trace_id
                if record.trace_id and record.trace_id != _UNKNOWN_TRACE
                else record.correlation_id or record.task_id
            )
            linkage = (_hex_id(f"trace:{key}", 16), None)
        self._task_trace[record.task_id] = linkage
        return linkage

    def _task_span_id(self, task_id: str) -> str:
        return _hex_id(f"span:task:{task_id}", 8)

    # -- feed --------------------------------------------------------------

    def feed(self, record: AuditRecord) -> list[dict[str, Any]]:  # noqa: C901
        handler = _HANDLERS.get(record.type)
        if handler is None:
            return []
        return handler(self, record)

    # task lifecycle

    def _open_task(self, record: AuditRecord) -> list[dict[str, Any]]:
        if record.task_id in self._open_tasks:
            return []
        trace_id, parent = self._linkage(record)
        self._open_tasks[record.task_id] = _OpenSpan(
            name="task",
            trace_id=trace_id,
            span_id=self._task_span_id(record.task_id),
            parent_span_id=parent,
            start_ns=_ns(record.occurred_at),
            attributes={"noeta.task_id": record.task_id},
        )
        return []

    def _close_task(
        self,
        record: AuditRecord,
        *,
        status: int,
        message: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        span = self._open_tasks.pop(record.task_id, None)
        self._task_trace.pop(record.task_id, None)
        if span is None:
            return []
        span.status_code = status
        span.status_message = message
        if extra:
            span.attributes.update(extra)
        return [span.finish(_ns(record.occurred_at))]

    def _on_task_created(self, record: AuditRecord) -> list[dict[str, Any]]:
        return self._open_task(record)

    def _on_task_started(self, record: AuditRecord) -> list[dict[str, Any]]:
        # Normally a no-op (TaskCreated already opened the span); covers a
        # process that attached to an existing stream mid-task.
        return self._open_task(record)

    def _on_agent_bound(self, record: AuditRecord) -> list[dict[str, Any]]:
        span = self._open_tasks.get(record.task_id)
        agent = record.payload_summary.get("agent_name")
        if span is not None and agent:
            span.name = f"task {agent}"
            span.attributes["noeta.agent"] = agent
        return []

    def _on_task_completed(self, record: AuditRecord) -> list[dict[str, Any]]:
        return self._close_task(record, status=_STATUS_OK)

    def _on_task_failed(self, record: AuditRecord) -> list[dict[str, Any]]:
        return self._close_task(
            record,
            status=_STATUS_ERROR,
            message=str(record.payload_summary.get("reason") or ""),
        )

    def _on_task_cancelled(self, record: AuditRecord) -> list[dict[str, Any]]:
        # A cancel is an outcome, not an error: status stays UNSET.
        return self._close_task(record, status=_STATUS_UNSET, extra={"noeta.cancelled": True})

    def _on_task_mark(self, record: AuditRecord) -> list[dict[str, Any]]:
        span = self._open_tasks.get(record.task_id)
        if span is None or len(span.events) >= _MAX_SPAN_EVENTS:
            return []
        event: dict[str, Any] = {
            "timeUnixNano": str(_ns(record.occurred_at)),
            "name": record.type,
        }
        reason = record.payload_summary.get("reason")
        if reason:
            event["attributes"] = [_kv("noeta.reason", reason)]
        span.events.append(event)
        return []

    def _on_subtask_spawned(self, record: AuditRecord) -> list[dict[str, Any]]:
        subtask_id = record.payload_summary.get("subtask_id")
        if not subtask_id:
            return []
        trace_id, _ = self._linkage(record)
        parent_span = self._task_span_id(record.task_id)
        child = self._open_tasks.get(str(subtask_id))
        if child is not None:
            # Child stream got recorded first: fix the linkage retroactively
            # (the child span is still open, nothing exported yet).
            child.trace_id = trace_id
            child.parent_span_id = parent_span
            self._task_trace[str(subtask_id)] = (trace_id, parent_span)
        else:
            self._pending_parent[str(subtask_id)] = (trace_id, parent_span)
        return []

    # call pairs (tool / llm)

    def _open_call(
        self, record: AuditRecord, *, name: str, attributes: dict[str, Any]
    ) -> list[dict[str, Any]]:
        call_id = record.payload_summary.get("call_id")
        if not call_id:
            return []
        trace_id, _ = self._linkage(record)
        parent = (
            self._task_span_id(record.task_id)
            if record.task_id in self._open_tasks
            else None
        )
        attributes = {"noeta.task_id": record.task_id, **attributes}
        self._open_calls[(record.task_id, str(call_id))] = _OpenSpan(
            name=name,
            trace_id=trace_id,
            span_id=_hex_id(f"span:call:{record.task_id}:{call_id}", 8),
            parent_span_id=parent,
            start_ns=_ns(record.occurred_at),
            attributes=attributes,
        )
        return []

    def _annotate_call(
        self, record: AuditRecord, attributes: dict[str, Any], *, failed: bool = False
    ) -> list[dict[str, Any]]:
        call_id = record.payload_summary.get("call_id")
        span = self._open_calls.get((record.task_id, str(call_id)))
        if span is not None:
            span.attributes.update(attributes)
            if failed:
                span.status_code = _STATUS_ERROR
        return []

    def _close_call(self, record: AuditRecord) -> list[dict[str, Any]]:
        call_id = record.payload_summary.get("call_id")
        span = self._open_calls.pop((record.task_id, str(call_id)), None)
        if span is None:
            return []
        return [span.finish(_ns(record.occurred_at))]

    def _on_tool_started(self, record: AuditRecord) -> list[dict[str, Any]]:
        tool = record.payload_summary.get("tool_name") or "tool"
        return self._open_call(
            record, name=f"tool {tool}", attributes={"gen_ai.tool.name": str(tool)}
        )

    def _on_tool_result(self, record: AuditRecord) -> list[dict[str, Any]]:
        success = record.payload_summary.get("success")
        attrs: dict[str, Any] = {}
        if success is not None:
            attrs["noeta.success"] = bool(success)
        return self._annotate_call(record, attrs, failed=success is False)

    def _on_llm_started(self, record: AuditRecord) -> list[dict[str, Any]]:
        model = record.payload_summary.get("model") or "llm"
        return self._open_call(
            record,
            name=f"llm {model}",
            attributes={"gen_ai.request.model": str(model)},
        )

    def _on_llm_response(self, record: AuditRecord) -> list[dict[str, Any]]:
        stop = record.payload_summary.get("stop_reason")
        attrs: dict[str, Any] = {}
        if stop:
            attrs["noeta.stop_reason"] = str(stop)
        return self._annotate_call(record, attrs)

    def _on_llm_finished(self, record: AuditRecord) -> list[dict[str, Any]]:
        success = record.payload_summary.get("success")
        cost = record.payload_summary.get("cost_usd")
        attrs: dict[str, Any] = {}
        if success is not None:
            attrs["noeta.success"] = bool(success)
        if isinstance(cost, (int, float)):
            attrs["noeta.cost_usd"] = float(cost)
        self._annotate_call(record, attrs, failed=success is False)
        return self._close_call(record)


_HANDLERS: dict[str, Callable[[_SpanAssembler, AuditRecord], list[dict[str, Any]]]] = {
    "TaskCreated": _SpanAssembler._on_task_created,
    "TaskStarted": _SpanAssembler._on_task_started,
    "AgentBound": _SpanAssembler._on_agent_bound,
    "TaskCompleted": _SpanAssembler._on_task_completed,
    "TaskFailed": _SpanAssembler._on_task_failed,
    "TaskCancelled": _SpanAssembler._on_task_cancelled,
    "TaskSuspended": _SpanAssembler._on_task_mark,
    "TaskWoken": _SpanAssembler._on_task_mark,
    "SubtaskSpawned": _SpanAssembler._on_subtask_spawned,
    "ToolCallStarted": _SpanAssembler._on_tool_started,
    "ToolResultRecorded": _SpanAssembler._on_tool_result,
    "ToolCallFinished": _SpanAssembler._close_call,
    "LLMRequestStarted": _SpanAssembler._on_llm_started,
    "LLMResponseRecorded": _SpanAssembler._on_llm_response,
    "LLMRequestFinished": _SpanAssembler._on_llm_finished,
}


def _encode_request(
    spans: list[dict[str, Any]], *, service_name: str
) -> bytes:
    """The OTLP/HTTP JSON ``ExportTraceServiceRequest`` for ``spans``."""
    request = {
        "resourceSpans": [
            {
                "resource": {"attributes": [_kv("service.name", service_name)]},
                "scopeSpans": [
                    {
                        "scope": {"name": "noeta.observers.otlp"},
                        "spans": spans,
                    }
                ],
            }
        ]
    }
    return json.dumps(request, separators=(",", ":")).encode("utf-8")


def _default_http_post(url: str, body: bytes, headers: Mapping[str, str]) -> None:
    import httpx  # runtime dependency; imported off the emit hot path

    httpx.post(url, content=body, headers=dict(headers), timeout=10.0).raise_for_status()


class OtlpSpanSink:
    """An ``AuditSink`` + ``close()`` pair: assemble spans, batch, POST.

    Runs entirely on the :class:`AsyncTraceSink` worker thread. Flushes when
    the buffer reaches ``batch_max`` or ``flush_interval_s`` has passed since
    the last flush (checked as records arrive); ``close()`` flushes the rest.
    A failed POST is logged and its batch dropped — never raised.
    """

    def __init__(
        self,
        config: OtlpTraceConfig,
        *,
        http_post: Optional[OtlpHttpPost] = None,
        batch_max: int = _DEFAULT_BATCH_MAX,
        flush_interval_s: float = _DEFAULT_FLUSH_INTERVAL_S,
    ) -> None:
        self._config = config
        self._post = http_post if http_post is not None else _default_http_post
        self._batch_max = batch_max
        self._flush_interval_s = flush_interval_s
        self._assembler = _SpanAssembler()
        self._buffer: list[dict[str, Any]] = []
        self._last_flush = time.monotonic()
        self._headers = {
            "Content-Type": "application/json",
            **dict(config.headers),
        }

    def __call__(self, record: AuditRecord) -> None:
        self._buffer.extend(self._assembler.feed(record))
        if len(self._buffer) >= self._batch_max or (
            self._buffer
            and time.monotonic() - self._last_flush >= self._flush_interval_s
        ):
            self._flush()

    def _flush(self) -> None:
        spans, self._buffer = self._buffer, []
        self._last_flush = time.monotonic()
        if not spans:
            return
        body = _encode_request(spans, service_name=self._config.service_name)
        try:
            self._post(self._config.endpoint, body, self._headers)
        except Exception as exc:  # noqa: BLE001 — an export must never break the run
            _log.warning(
                "otlp trace export: POST to %s failed (%s); dropped %d span(s)",
                self._config.endpoint,
                exc,
                len(spans),
            )

    def close(self) -> None:
        self._flush()


def make_otlp_trace_observer(
    *,
    event_log: EventLogSubscriber,
    config: OtlpTraceConfig,
    http_post: Optional[OtlpHttpPost] = None,
) -> TraceExportObserver:
    """Build an OTLP trace export observer for ``config``.

    Same lifecycle shape as :func:`make_jsonl_trace_observer`: the returned
    :class:`TraceExportObserver` owns subscription + async worker + sink and
    tears them down in order on ``stop()``.
    """
    return TraceExportObserver(
        event_log=event_log,
        inner_sink=OtlpSpanSink(config, http_post=http_post),
    )
