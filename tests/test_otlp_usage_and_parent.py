"""Audit + OTLP projections carry what a dashboard needs, across hosts.

* ``LLMRequestFinished`` used to reach the audit row / llm span as
  ``call_id`` / ``success`` / ``cost_usd`` only; token usage and latency were
  dropped even though the payload records them.
* A child task's span found its parent only through a process-local dict fed
  by the parent's ``SubtaskSpawned``: a child exported by another host (a
  worker fleet) came out with no ``parentSpanId``. The child's ``TaskCreated``
  names its parent, so the edge is now derived from that alone.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from noeta.client.otlp import OtlpSpanSink, OtlpTraceConfig
from noeta.observers.audit import AuditRecord, _summarize
from noeta.protocols.events import (
    LLMRequestFinishedPayload,
    LLMRequestStartedPayload,
    SubtaskSpawnedPayload,
    TaskCompletedPayload,
    TaskCreatedPayload,
    TaskFailedPayload,
)
from noeta.protocols.messages import Usage
from noeta.protocols.values import ContentRef


_TRACE = "trace-shared"
_REF = ContentRef(hash="a" * 64, size=1, media_type="application/json")
_USAGE = Usage(uncached=100, cache_read=2000, cache_write=30, output=400, reasoning_tokens=50)


def _rec(task_id: str, seq: int, type_: str, payload: Any) -> AuditRecord:
    return AuditRecord(
        id=f"{task_id}-{seq}",
        task_id=task_id,
        seq=seq,
        type=type_,
        schema_version=1,
        occurred_at=1_700_000_000.0 + seq,
        actor="engine",
        trace_id=_TRACE,
        correlation_id=task_id,
        causation_id=None,
        origin="engine",
        payload_summary=_summarize(type_, payload),
    )


class _Collector:
    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> None:  # noqa: ARG002
        request = json.loads(body)
        for rs in request["resourceSpans"]:
            for ss in rs["scopeSpans"]:
                self.spans.extend(ss["spans"])


def _sink() -> tuple[OtlpSpanSink, _Collector]:
    collector = _Collector()
    sink = OtlpSpanSink(
        OtlpTraceConfig(endpoint="http://collector/v1/traces"),
        http_post=collector,
        batch_max=10_000,
        flush_interval_s=3600.0,
    )
    return sink, collector


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kv in span["attributes"]:
        (typed,) = kv["value"].items()
        out[kv["key"]] = typed[1]
    return out


def _parent_records() -> list[AuditRecord]:
    return [
        _rec("parent", 1, "TaskCreated", TaskCreatedPayload(goal="g", policy_name="p")),
        _rec(
            "parent",
            2,
            "SubtaskSpawned",
            SubtaskSpawnedPayload(subtask_id="child", agent_name="worker", goal="sub"),
        ),
        _rec("parent", 9, "TaskCompleted", TaskCompletedPayload(answer="ok")),
    ]


def _child_records() -> list[AuditRecord]:
    return [
        _rec(
            "child",
            1,
            "TaskCreated",
            TaskCreatedPayload(
                goal="sub", policy_name="p", agent_name="worker", parent_task_id="parent"
            ),
        ),
        _rec("child", 2, "TaskCompleted", TaskCompletedPayload(answer="done")),
    ]


def _task_span(spans: list[dict[str, Any]], task_id: str) -> dict[str, Any]:
    (span,) = [
        s for s in spans if s["name"].startswith("task") and _attrs(s)["noeta.task_id"] == task_id
    ]
    return span


# --- C3: parent span across hosts --------------------------------------------


def test_child_exported_on_another_host_links_to_its_parent() -> None:
    host_a, spans_a = _sink()
    for record in _parent_records():
        host_a(record)
    host_a.close()

    host_b, spans_b = _sink()
    for record in _child_records():
        host_b(record)
    host_b.close()

    parent = _task_span(spans_a.spans, "parent")
    child = _task_span(spans_b.spans, "child")
    assert child["traceId"] == parent["traceId"]
    assert child["parentSpanId"] == parent["spanId"]
    assert "parentSpanId" not in parent


def test_same_host_linkage_is_unchanged() -> None:
    sink, collected = _sink()
    parent_created, spawned, parent_done = _parent_records()
    child_created, child_done = _child_records()
    for record in (parent_created, spawned, child_created, child_done, parent_done):
        sink(record)
    sink.close()

    parent = _task_span(collected.spans, "parent")
    child = _task_span(collected.spans, "child")
    assert child["traceId"] == parent["traceId"]
    assert child["parentSpanId"] == parent["spanId"]


# --- C2: usage + latency ------------------------------------------------------


def test_audit_row_keeps_usage_and_latency() -> None:
    summary = _summarize(
        "LLMRequestFinished",
        LLMRequestFinishedPayload(
            call_id="L1", success=True, cost_usd=0.5, latency_ms=812, usage=_USAGE
        ),
    )
    assert summary == {
        "call_id": "L1",
        "success": True,
        "cost_usd": 0.5,
        "latency_ms": 812,
        "usage": {
            "uncached": 100,
            "cache_read": 2000,
            "cache_write": 30,
            "output": 400,
            "reasoning_tokens": 50,
        },
    }


def test_llm_span_carries_usage_and_latency() -> None:
    sink, collected = _sink()
    sink(_rec("t", 1, "TaskCreated", TaskCreatedPayload(goal="g", policy_name="p")))
    sink(
        _rec(
            "t",
            2,
            "LLMRequestStarted",
            LLMRequestStartedPayload(call_id="L1", model="m-1", request_ref=_REF),
        )
    )
    sink(
        _rec(
            "t",
            3,
            "LLMRequestFinished",
            LLMRequestFinishedPayload(
                call_id="L1", success=True, cost_usd=0.5, latency_ms=812, usage=_USAGE
            ),
        )
    )
    sink.close()

    (llm,) = [s for s in collected.spans if s["name"] == "llm m-1"]
    attrs = _attrs(llm)
    assert attrs["noeta.cost_usd"] == 0.5
    assert attrs["noeta.success"] is True
    # int64 → string per the proto3 JSON mapping.
    assert attrs["noeta.latency_ms"] == "812"
    assert attrs["noeta.usage.uncached"] == "100"
    assert attrs["noeta.usage.cache_read"] == "2000"
    assert attrs["noeta.usage.cache_write"] == "30"
    assert attrs["noeta.usage.output"] == "400"
    assert attrs["noeta.usage.reasoning_tokens"] == "50"
    assert attrs["gen_ai.usage.input_tokens"] == "2130"
    assert attrs["gen_ai.usage.output_tokens"] == "400"


# --- C1 projections: failure detail -------------------------------------------


def test_task_created_audit_row_names_the_parent_only() -> None:
    child = _summarize(
        "TaskCreated",
        TaskCreatedPayload(goal="secret", policy_name="p", parent_task_id="parent"),
    )
    assert child["parent_task_id"] == "parent"
    assert "secret" not in json.dumps(child)
    root = _summarize("TaskCreated", TaskCreatedPayload(goal="g", policy_name="p"))
    assert "parent_task_id" not in root


def test_failed_task_span_and_audit_row_carry_detail() -> None:
    payload = TaskFailedPayload(reason="llm_error", detail="HTTP 400: bad image")
    assert _summarize("TaskFailed", payload) == {
        "reason": "llm_error",
        "retryable": False,
        "detail": "HTTP 400: bad image",
    }
    sink, collected = _sink()
    sink(_rec("t", 1, "TaskCreated", TaskCreatedPayload(goal="g", policy_name="p")))
    sink(_rec("t", 2, "TaskFailed", payload))
    sink.close()
    span = _task_span(collected.spans, "t")
    assert span["status"] == {"code": 2, "message": "llm_error"}
    assert _attrs(span)["noeta.fail_detail"] == "HTTP 400: bad image"
