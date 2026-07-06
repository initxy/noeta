"""OTLP trace export — span assembly, OTLP/HTTP JSON encoding, sink batching.

Covers: start/finish pairing into spans (task / tool / llm, keyed on
``call_id``), the deterministic id scheme, subtask → parent trace linkage
(both arrival orders), suspend/wake span events, error status mapping, the
``ExportTraceServiceRequest`` JSON shape, sink batching / flush-on-close /
POST-failure swallowing, the end-to-end observer over a real EventLog (with
the audit allowlist inheritance), and the app config resolution (config key
+ OTel-standard env fallbacks).
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from noeta.agent.backend import BackendConfig
from noeta.observers.audit import AuditRecord
from noeta.observers.otlp import (
    OtlpSpanSink,
    OtlpTraceConfig,
    _SpanAssembler,
    make_otlp_trace_observer,
)
from noeta.protocols.events import (
    TaskCompletedPayload,
    TaskCreatedPayload,
    ToolCallFinishedPayload,
    ToolCallStartedPayload,
)
from noeta.storage.memory import InMemoryEventLog


def _record(**over: Any) -> AuditRecord:
    base: dict[str, Any] = dict(
        id="e1", task_id="t1", seq=0, type="TaskStarted", schema_version=1,
        occurred_at=1.0, actor="engine", trace_id="tr", correlation_id="c",
        causation_id=None, origin="engine", payload_summary={},
    )
    base.update(over)
    return AuditRecord(**base)


class _FakePost:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, bytes, Mapping[str, str]]] = []
        self.fail = fail

    def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> None:
        self.calls.append((url, body, headers))
        if self.fail:
            raise RuntimeError("collector down")

    def spans(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for _, body, _ in self.calls:
            req = json.loads(body)
            for rs in req["resourceSpans"]:
                for ss in rs["scopeSpans"]:
                    out.extend(ss["spans"])
        return out


# -- _SpanAssembler: pairing --------------------------------------------------


def test_llm_span_pairs_started_response_finished() -> None:
    a = _SpanAssembler()
    assert a.feed(_record(type="LLMRequestStarted", occurred_at=10.0,
                          payload_summary={"call_id": "c1", "model": "m-1"})) == []
    assert a.feed(_record(type="LLMResponseRecorded",
                          payload_summary={"call_id": "c1", "stop_reason": "end_turn"})) == []
    spans = a.feed(_record(type="LLMRequestFinished", occurred_at=12.5,
                           payload_summary={"call_id": "c1", "success": True,
                                            "cost_usd": 0.25}))
    assert len(spans) == 1
    span = spans[0]
    assert span["name"] == "llm m-1"
    assert span["startTimeUnixNano"] == str(int(10.0 * 1e9))
    assert span["endTimeUnixNano"] == str(int(12.5 * 1e9))
    assert len(span["traceId"]) == 32 and len(span["spanId"]) == 16
    attrs = {kv["key"]: kv["value"] for kv in span["attributes"]}
    assert attrs["gen_ai.request.model"] == {"stringValue": "m-1"}
    assert attrs["noeta.stop_reason"] == {"stringValue": "end_turn"}
    assert attrs["noeta.cost_usd"] == {"doubleValue": 0.25}
    assert attrs["noeta.success"] == {"boolValue": True}
    assert "status" not in span  # success ⇒ UNSET on a call span


def test_tool_span_failure_maps_to_error_status() -> None:
    a = _SpanAssembler()
    a.feed(_record(type="ToolCallStarted",
                   payload_summary={"call_id": "c1", "tool_name": "shell_run"}))
    a.feed(_record(type="ToolResultRecorded",
                   payload_summary={"call_id": "c1", "success": False}))
    spans = a.feed(_record(type="ToolCallFinished", payload_summary={"call_id": "c1"}))
    assert len(spans) == 1
    assert spans[0]["name"] == "tool shell_run"
    assert spans[0]["status"] == {"code": 2}
    # A finish with no matching start emits nothing.
    assert a.feed(_record(type="ToolCallFinished", payload_summary={"call_id": "zz"})) == []


def test_task_span_lifecycle_agent_name_and_marks() -> None:
    a = _SpanAssembler()
    a.feed(_record(type="TaskCreated", occurred_at=1.0))
    a.feed(_record(type="AgentBound", payload_summary={"agent_name": "main"}))
    a.feed(_record(type="TaskSuspended", occurred_at=2.0,
                   payload_summary={"reason": "approval"}))
    a.feed(_record(type="TaskWoken", occurred_at=3.0))
    spans = a.feed(_record(type="TaskCompleted", occurred_at=4.0))
    assert len(spans) == 1
    span = spans[0]
    assert span["name"] == "task main"
    assert span["status"] == {"code": 1}
    assert [e["name"] for e in span["events"]] == ["TaskSuspended", "TaskWoken"]
    assert span["events"][0]["attributes"] == [
        {"key": "noeta.reason", "value": {"stringValue": "approval"}}
    ]
    # Closing again is a no-op (state was released).
    assert a.feed(_record(type="TaskCompleted", occurred_at=5.0)) == []


def test_task_failed_carries_error_status_and_reason() -> None:
    a = _SpanAssembler()
    a.feed(_record(type="TaskCreated"))
    spans = a.feed(_record(type="TaskFailed",
                           payload_summary={"reason": "budget exceeded"}))
    assert spans[0]["status"] == {"code": 2, "message": "budget exceeded"}


def test_call_span_parents_to_open_task_span() -> None:
    a = _SpanAssembler()
    a.feed(_record(type="TaskCreated"))
    a.feed(_record(type="ToolCallStarted",
                   payload_summary={"call_id": "c1", "tool_name": "read"}))
    tool = a.feed(_record(type="ToolCallFinished", payload_summary={"call_id": "c1"}))[0]
    task = a.feed(_record(type="TaskCompleted"))[0]
    assert tool["parentSpanId"] == task["spanId"]
    assert tool["traceId"] == task["traceId"]
    assert "parentSpanId" not in task  # root task has no parent


def test_subtask_joins_parent_trace_spawn_first() -> None:
    a = _SpanAssembler()
    a.feed(_record(type="TaskCreated", task_id="parent"))
    a.feed(_record(type="SubtaskSpawned", task_id="parent",
                   payload_summary={"subtask_id": "child", "agent_name": "explore"}))
    a.feed(_record(type="TaskCreated", task_id="child", trace_id="trace-unknown",
                   correlation_id="child"))
    child = a.feed(_record(type="TaskCompleted", task_id="child"))[0]
    parent = a.feed(_record(type="TaskCompleted", task_id="parent"))[0]
    assert child["traceId"] == parent["traceId"]
    assert child["parentSpanId"] == parent["spanId"]


def test_subtask_joins_parent_trace_child_recorded_first() -> None:
    a = _SpanAssembler()
    a.feed(_record(type="TaskCreated", task_id="parent"))
    # Child stream lands before the parent's SubtaskSpawned record.
    a.feed(_record(type="TaskCreated", task_id="child", trace_id="trace-unknown",
                   correlation_id="child"))
    a.feed(_record(type="SubtaskSpawned", task_id="parent",
                   payload_summary={"subtask_id": "child"}))
    child = a.feed(_record(type="TaskCompleted", task_id="child"))[0]
    parent = a.feed(_record(type="TaskCompleted", task_id="parent"))[0]
    assert child["traceId"] == parent["traceId"]
    assert child["parentSpanId"] == parent["spanId"]


def test_unhandled_and_point_events_emit_nothing() -> None:
    a = _SpanAssembler()
    assert a.feed(_record(type="MessagesAppended")) == []
    assert a.feed(_record(type="ContextPlanComposed")) == []


def test_ids_are_deterministic_across_assemblers() -> None:
    def close(assembler: _SpanAssembler) -> dict[str, Any]:
        assembler.feed(_record(type="TaskCreated"))
        return assembler.feed(_record(type="TaskCompleted"))[0]

    one, two = close(_SpanAssembler()), close(_SpanAssembler())
    assert one["traceId"] == two["traceId"]
    assert one["spanId"] == two["spanId"]


# -- OtlpSpanSink: batching + transport ---------------------------------------


def _llm_pair(sink: OtlpSpanSink, call_id: str) -> None:
    sink(_record(type="LLMRequestStarted",
                 payload_summary={"call_id": call_id, "model": "m"}))
    sink(_record(type="LLMRequestFinished",
                 payload_summary={"call_id": call_id, "success": True}))


def test_sink_flushes_on_close_and_encodes_resource() -> None:
    post = _FakePost()
    sink = OtlpSpanSink(
        OtlpTraceConfig(endpoint="http://c:4318/v1/traces",
                        headers=(("authorization", "Bearer k"),),
                        service_name="svc"),
        http_post=post, batch_max=100, flush_interval_s=3600.0,
    )
    _llm_pair(sink, "c1")
    assert post.calls == []  # below batch_max, interval not elapsed
    sink.close()
    assert len(post.calls) == 1
    url, body, headers = post.calls[0]
    assert url == "http://c:4318/v1/traces"
    assert headers["Content-Type"] == "application/json"
    assert headers["authorization"] == "Bearer k"
    req = json.loads(body)
    resource = req["resourceSpans"][0]["resource"]
    assert resource["attributes"] == [
        {"key": "service.name", "value": {"stringValue": "svc"}}
    ]
    assert len(post.spans()) == 1


def test_sink_flushes_at_batch_max() -> None:
    post = _FakePost()
    sink = OtlpSpanSink(
        OtlpTraceConfig(endpoint="http://c/v1/traces"),
        http_post=post, batch_max=2, flush_interval_s=3600.0,
    )
    _llm_pair(sink, "c1")
    assert post.calls == []
    _llm_pair(sink, "c2")
    assert len(post.calls) == 1 and len(post.spans()) == 2


def test_sink_swallows_post_failure_and_keeps_going() -> None:
    post = _FakePost(fail=True)
    sink = OtlpSpanSink(
        OtlpTraceConfig(endpoint="http://c/v1/traces"),
        http_post=post, batch_max=1,
    )
    _llm_pair(sink, "c1")  # POST raises; swallowed
    _llm_pair(sink, "c2")  # sink still functional
    sink.close()
    assert len(post.calls) == 2  # each failed batch was attempted then dropped


# -- end-to-end: observer over a real EventLog --------------------------------


def test_observer_end_to_end_exports_spans_not_bodies() -> None:
    log = InMemoryEventLog()
    post = _FakePost()
    obs = make_otlp_trace_observer(
        event_log=log,
        config=OtlpTraceConfig(endpoint="http://c:4318/v1/traces"),
        http_post=post,
    )
    log.emit(task_id="t1", type="TaskCreated",
             payload=TaskCreatedPayload(goal="SECRET_GOAL", policy_name="react"),
             trace_id="tr-1")
    log.emit(task_id="t1", type="ToolCallStarted",
             payload=ToolCallStartedPayload(
                 call_id="c1", tool_name="write",
                 arguments={"content": "ARG_SECRET"}))
    log.emit(task_id="t1", type="ToolCallFinished",
             payload=ToolCallFinishedPayload(call_id="c1"))
    log.emit(task_id="t1", type="TaskCompleted",
             payload=TaskCompletedPayload(answer="ANSWER_SECRET"))
    obs.stop()  # graceful drain flushes both completed spans

    spans = post.spans()
    assert sorted(s["name"] for s in spans) == ["task", "tool write"]
    blob = b"".join(body for _, body, _ in post.calls).decode()
    assert "SECRET_GOAL" not in blob     # audit allowlist inherited
    assert "ARG_SECRET" not in blob
    assert "ANSWER_SECRET" not in blob
    obs.stop()  # idempotent


# -- app config resolution -----------------------------------------------------


def test_backend_config_otlp_from_config_file(tmp_path: Any) -> None:
    cfg = tmp_path / "noeta.config.json"
    cfg.write_text(json.dumps({
        "otlp_endpoint": "http://collector:4318/v1/traces",
        "otlp_headers": {"authorization": "Bearer k"},
    }))
    c = BackendConfig.from_env({"NOETA_AGENT_CONFIG": str(cfg)})
    assert c.otlp_endpoint == "http://collector:4318/v1/traces"
    assert dict(c.otlp_headers) == {"authorization": "Bearer k"}


def test_backend_config_otlp_is_opt_in_only() -> None:
    # Ambient OTel-standard endpoint env must NOT silently enable export
    # (k8s operators inject it process-wide for other apps).
    c = BackendConfig.from_env({
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://ambient:4318",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://ambient:4318/v1/traces",
    })
    assert c.otlp_endpoint is None
    # Only the Noeta-specific env var / config key enable it; the standard
    # HEADERS var then rides along, percent-decoded per the spec.
    c = BackendConfig.from_env({
        "NOETA_AGENT_OTLP_ENDPOINT": "http://mine/v1/traces",
        "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Basic%20dXNlcg==,x-org=acme",
    })
    assert c.otlp_endpoint == "http://mine/v1/traces"
    assert dict(c.otlp_headers) == {
        "authorization": "Basic dXNlcg==", "x-org": "acme",
    }
    # Default: off.
    assert BackendConfig.from_env({}).otlp_endpoint is None


def test_backend_config_otlp_headers_coerced_to_str(tmp_path: Any) -> None:
    cfg = tmp_path / "noeta.config.json"
    cfg.write_text(json.dumps({
        "otlp_endpoint": "http://c/v1/traces",
        "otlp_headers": {"x-timeout": 30},
    }))
    c = BackendConfig.from_env({"NOETA_AGENT_CONFIG": str(cfg)})
    assert dict(c.otlp_headers) == {"x-timeout": "30"}


# -- review fixes: segment reopen / background linkage / sink close -----------


def test_task_woken_without_open_span_reopens_segment() -> None:
    # A host restart resumes a suspended task: this process never saw its
    # TaskCreated, only a TaskWoken. The task still gets a (segment) span.
    a = _SpanAssembler()
    a.feed(_record(type="TaskWoken", seq=40, occurred_at=5.0))
    a.feed(_record(type="ToolCallStarted",
                   payload_summary={"call_id": "c1", "tool_name": "read"}))
    tool = a.feed(_record(type="ToolCallFinished", payload_summary={"call_id": "c1"}))[0]
    seg = a.feed(_record(type="TaskCompleted", occurred_at=9.0))[0]
    assert seg["startTimeUnixNano"] == str(int(5.0 * 1e9))
    assert tool["parentSpanId"] == seg["spanId"]
    attrs = {kv["key"]: kv["value"] for kv in seg["attributes"]}
    assert attrs["noeta.resumed"] == {"boolValue": True}
    # The segment id must differ from the primary task span id (which a
    # previous process may already have exported).
    fresh = _SpanAssembler()
    fresh.feed(_record(type="TaskCreated"))
    primary = fresh.feed(_record(type="TaskCompleted"))[0]
    assert seg["spanId"] != primary["spanId"]


def test_cancel_rewind_continue_reopens_segment() -> None:
    a = _SpanAssembler()
    a.feed(_record(type="TaskCreated", occurred_at=1.0))
    cancelled = a.feed(_record(type="TaskCancelled", occurred_at=2.0,
                               payload_summary={"reason": "user"}))
    assert len(cancelled) == 1  # primary span exported at cancel
    a.feed(_record(type="TaskRewound", seq=50, occurred_at=3.0,
                   payload_summary={"target_seq": 10}))
    a.feed(_record(type="ToolCallStarted",
                   payload_summary={"call_id": "c9", "tool_name": "edit"}))
    tool = a.feed(_record(type="ToolCallFinished", payload_summary={"call_id": "c9"}))[0]
    seg = a.feed(_record(type="TaskCompleted", occurred_at=6.0))[0]
    assert seg["status"] == {"code": 1}  # the continuation completes OK
    assert seg["spanId"] != cancelled[0]["spanId"]
    assert seg["traceId"] == cancelled[0]["traceId"]
    assert tool["parentSpanId"] == seg["spanId"]


def test_background_subagent_links_to_parent() -> None:
    a = _SpanAssembler()
    a.feed(_record(type="TaskCreated", task_id="parent"))
    a.feed(_record(type="BackgroundSubagentStarted", task_id="parent",
                   payload_summary={"subtask_id": "bg-child", "agent_name": "explore"}))
    a.feed(_record(type="TaskCreated", task_id="bg-child", trace_id="trace-unknown",
                   correlation_id="bg-child"))
    child = a.feed(_record(type="TaskCompleted", task_id="bg-child"))[0]
    parent = a.feed(_record(type="TaskCompleted", task_id="parent"))[0]
    assert child["traceId"] == parent["traceId"]
    assert child["parentSpanId"] == parent["spanId"]


def test_task_close_sweeps_orphaned_call_spans() -> None:
    a = _SpanAssembler()
    a.feed(_record(type="TaskCreated"))
    a.feed(_record(type="ToolCallStarted",
                   payload_summary={"call_id": "orphan", "tool_name": "x"}))
    a.feed(_record(type="TaskFailed", payload_summary={"reason": "died mid-step"}))
    assert a._open_calls == {}  # no leak
    # A late Finished for the swept call emits nothing.
    assert a.feed(_record(type="ToolCallFinished",
                          payload_summary={"call_id": "orphan"})) == []


def test_sink_call_after_close_is_noop() -> None:
    post = _FakePost()
    sink = OtlpSpanSink(
        OtlpTraceConfig(endpoint="http://c/v1/traces"),
        http_post=post, batch_max=1,
    )
    sink.close()
    _llm_pair(sink, "c1")  # dropped: sink is closed
    sink.close()  # idempotent
    assert post.calls == []
