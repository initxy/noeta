"""ToolRuntime (normal mode): records the three-event tool envelope.

Each ``invoke`` MUST produce, in order:
    1. ``ToolCallStarted``  — call_id + tool name + arguments
    2. ``ToolResultRecorded`` — output_ref + summary + artifacts + side_effects
    3. ``ToolCallFinished`` — call_id

The result body lives in ContentStore (per the 4-KB rule); the
EventLog only carries the ContentRef.
"""

from __future__ import annotations

from typing import Any

from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.decisions import ToolCall
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.protocols.tool_args import (
    build_tool_call_started_payload,
    resolve_tool_call_arguments,
)
from noeta.protocols.values import EVENT_PAYLOAD_MAX_BYTES, ContentRef
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


class _RecordingTool:
    name = "echo"
    risk_level = "low"
    input_schema: dict[str, Any] = {"type": "object", "additionalProperties": True}

    def __init__(self, output: Any = "out", artifacts: list[ContentRef] | None = None) -> None:
        self._output = output
        self._artifacts = artifacts or []
        self.received: list[dict[str, Any]] = []

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG002
        self.received.append(arguments)
        return ToolResult(
            success=True,
            output=self._output,
            summary="echoed",
            artifacts=list(self._artifacts),
        )


def _runtime() -> tuple[ToolRuntime, InMemoryEventLog, InMemoryContentStore]:
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    rt = ToolRuntime(event_log=log, content_store=store)
    return rt, log, store


class _RaisingTool:
    name = "boom"
    risk_level = "low"
    input_schema: dict[str, Any] = {"type": "object", "additionalProperties": True}

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG002
        raise RuntimeError("kaboom")


def test_raising_tool_still_records_the_full_trio_as_a_failed_result() -> None:
    """A tool that RAISES must not strand the assistant ``tool_use`` (already
    committed) without a matching ``tool_result`` — that dangles the function
    call and the provider 400s on the next compose→decide. The runtime catches
    the raise and records a failed ToolResultRecorded so the trio still closes.
    """
    rt, log, store = _runtime()
    call = ToolCall(tool_name="boom", arguments={}, call_id="c-boom")

    result = rt.invoke(
        _RaisingTool(), call, task_id="t1", lease_id="lease-1", trace_id="trace-1"
    )

    # The trio is intact and the recorded result is a failure.
    types = [e.type for e in log.read("t1")]
    assert types == ["ToolCallStarted", "ToolResultRecorded", "ToolCallFinished"]
    recorded = log.read("t1")[1]
    assert recorded.payload.success is False
    assert recorded.payload.call_id == "c-boom"
    assert result.success is False
    assert "raised" in result.summary and "kaboom" in result.summary


def test_invoke_writes_three_events_in_order_for_a_single_call() -> None:
    rt, log, _store = _runtime()
    tool = _RecordingTool(output="hi")
    call = ToolCall(tool_name="echo", arguments={"msg": "x"}, call_id="c-1")

    rt.invoke(tool, call, task_id="t1", lease_id="lease-1", trace_id="trace-1")

    types = [e.type for e in log.read("t1")]
    assert types == ["ToolCallStarted", "ToolResultRecorded", "ToolCallFinished"]


def test_tool_call_started_carries_call_id_tool_name_and_arguments() -> None:
    rt, log, _store = _runtime()
    tool = _RecordingTool()
    call = ToolCall(tool_name="echo", arguments={"msg": "hello"}, call_id="c-9")

    rt.invoke(tool, call, task_id="t1", lease_id="lease-1", trace_id="trace-1")

    started = log.read("t1")[0]
    assert started.payload.call_id == "c-9"
    assert started.payload.tool_name == "echo"
    assert started.payload.arguments == {"msg": "hello"}


def test_tool_result_recorded_output_ref_points_to_readable_body() -> None:
    rt, log, store = _runtime()
    tool = _RecordingTool(output={"a": 1, "b": 2})
    call = ToolCall(tool_name="echo", arguments={}, call_id="c-1")

    rt.invoke(tool, call, task_id="t1", lease_id="lease-1", trace_id="trace-1")

    recorded = [e for e in log.read("t1") if e.type == "ToolResultRecorded"][0]
    assert recorded.payload.call_id == "c-1"
    assert isinstance(recorded.payload.output_ref, ContentRef)
    body = store.get(recorded.payload.output_ref)
    assert b"\"a\":1" in body


def test_tool_result_recorded_carries_summary_artifacts_and_side_effects() -> None:
    rt, log, store = _runtime()
    fake_ref = store.put(b"already-there", media_type="text/plain")
    tool = _RecordingTool(output="o", artifacts=[fake_ref])
    call = ToolCall(tool_name="echo", arguments={}, call_id="c-1")

    rt.invoke(tool, call, task_id="t1", lease_id="lease-1", trace_id="trace-1")

    recorded = [e for e in log.read("t1") if e.type == "ToolResultRecorded"][0]
    assert recorded.payload.summary == "echoed"
    assert recorded.payload.artifacts == [fake_ref]
    assert recorded.payload.side_effects == []


def test_invoke_returns_the_tool_result_to_caller() -> None:
    rt, _log, _store = _runtime()
    tool = _RecordingTool(output="hi")
    call = ToolCall(tool_name="echo", arguments={}, call_id="c-1")

    result = rt.invoke(
        tool, call, task_id="t1", lease_id="lease-1", trace_id="trace-1"
    )

    assert result.success is True
    assert result.output == "hi"


# -- oversized arguments → ContentStore offload -------------------


def _big_args() -> dict[str, Any]:
    """Arguments whose canonical form exceeds the 4-KB payload ceiling."""
    return {"path": "f.py", "text": "x" * (EVENT_PAYLOAD_MAX_BYTES + 1000)}


def test_oversized_arguments_are_offloaded_and_do_not_break_the_cap() -> None:
    rt, log, store = _runtime()
    tool = _RecordingTool()
    args = _big_args()
    call = ToolCall(tool_name="echo", arguments=args, call_id="c-big")

    # Before the offload this emit raised PayloadTooLarge mid-step.
    rt.invoke(tool, call, task_id="t1", lease_id="lease-1", trace_id="trace-1")

    started = log.read("t1")[0]
    assert started.type == "ToolCallStarted"
    # Arguments went by reference, not inline, and the event payload itself
    # now sits comfortably under the EventLog's 4-KB ceiling.
    assert started.payload.arguments is None
    assert isinstance(started.payload.arguments_ref, ContentRef)
    assert len(to_canonical_bytes(started.payload)) <= EVENT_PAYLOAD_MAX_BYTES
    # The tool still saw the full, un-truncated arguments.
    assert tool.received == [args]
    # And the recorded arguments round-trip back out of the ContentStore.
    assert resolve_tool_call_arguments(started.payload, store) == args


def test_small_arguments_stay_inline() -> None:
    store = InMemoryContentStore()
    call = ToolCall(tool_name="echo", arguments={"msg": "hi"}, call_id="c-1")

    payload = build_tool_call_started_payload(call, store)

    assert payload.arguments == {"msg": "hi"}
    assert payload.arguments_ref is None
    assert len(store) == 0  # nothing offloaded for a small call
    assert resolve_tool_call_arguments(payload, store) == {"msg": "hi"}


def test_build_is_deterministic() -> None:
    # Rebuilding the payload from the same recorded ToolCall must yield
    # byte-identical payloads (same offloaded ref) — identical args in, identical bytes out.
    store_a = InMemoryContentStore()
    store_b = InMemoryContentStore()
    call = ToolCall(tool_name="echo", arguments=_big_args(), call_id="c-big")

    payload_a = build_tool_call_started_payload(call, store_a)
    payload_b = build_tool_call_started_payload(call, store_b)

    assert payload_a.arguments_ref == payload_b.arguments_ref
    assert to_canonical_bytes(payload_a) == to_canonical_bytes(payload_b)
