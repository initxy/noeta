"""OpenAI Responses adapter — the SSE stream: deltas (text / reasoning
summary, never tool arguments), the terminal event feeding the one parser,
in-stream error events, truncated streams and cancellation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from noeta.errors import ContextOverflow, Fatal, RateLimited, Transient
from noeta.types import Text, Thinking, ToolUse, Usage

from tests.providers._responses_helpers import (
    ENDPOINT,
    DeltaRecorder,
    basic_request,
    completed,
    make_provider,
    responses_payload,
    sent_body,
    stream_of,
    text_delta,
)
from tests.providers.sse import RaisingStream, sse_bytes


def _message_output(*texts: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": t} for t in texts],
    }


def _final(output: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    payload = responses_payload(texts=None, **kw)
    payload["output"] = output
    return payload


def _mock(frames: list[tuple[str | None, Any]]) -> respx.Route:
    return respx.post(ENDPOINT).mock(return_value=stream_of(frames))


@respx.mock
async def test_text_deltas_in_order_and_final_from_terminal_event() -> None:
    final = _final([_message_output("hello world")], usage={"input_tokens": 12, "output_tokens": 5})
    route = _mock(
        [
            ("response.created", {"type": "response.created", "response": {"status": "in_progress"}}),
            (
                "response.output_item.added",
                {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message"}},
            ),
            text_delta("hello ", 0),
            text_delta("world", 0),
            (
                "response.output_text.done",
                {"type": "response.output_text.done", "output_index": 0, "text": "hello world"},
            ),
            completed(final),
        ]
    )
    recorder = DeltaRecorder()
    response = await make_provider().complete(basic_request(), recorder)

    assert recorder.as_tuples == [("text", "hello ", 0), ("text", "world", 0)]
    assert response.stop_reason == "end_turn"
    assert response.message.content == [Text(text="hello world")]
    assert response.usage == Usage(uncached=12, output=5)
    assert sent_body(route)["stream"] is True


@respx.mock
async def test_no_on_delta_is_fine() -> None:
    _mock([text_delta("x"), completed(_final([_message_output("x")]))])
    response = await make_provider().complete(basic_request())
    assert response.message.text == "x"


@respx.mock
async def test_function_call_argument_deltas_never_emitted() -> None:
    final = _final(
        [
            {
                "type": "function_call",
                "id": "fc_internal_1",
                "call_id": "call_abc",
                "name": "get_weather",
                "arguments": json.dumps({"city": "Beijing"}),
            }
        ]
    )
    _mock(
        [
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"type": "function_call", "name": "get_weather"},
                },
            ),
            (
                "response.function_call_arguments.delta",
                {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"city"'},
            ),
            (
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "delta": ': "Beijing"}',
                },
            ),
            completed(final),
        ]
    )
    recorder = DeltaRecorder()
    response = await make_provider().complete(basic_request(), recorder)
    assert recorder.deltas == []
    assert response.stop_reason == "tool_use"
    assert response.message.content == [
        ToolUse(id="call_abc", name="get_weather", input={"city": "Beijing"})
    ]


@respx.mock
async def test_reasoning_summary_deltas_are_thinking_and_final_keeps_signature() -> None:
    enc = "gAAAA" + "x" * 120
    final = _final(
        [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [
                    {"type": "summary_text", "text": "step one"},
                    {"type": "summary_text", "text": "step two"},
                ],
                "encrypted_content": enc,
            },
            _message_output("final answer"),
        ]
    )

    def summary(fragment: str) -> tuple[str, dict[str, Any]]:
        name = "response.reasoning_summary_text.delta"
        return (name, {"type": name, "item_id": "rs_1", "output_index": 0, "delta": fragment})

    _mock(
        [
            summary("step one"),
            summary("step two"),
            text_delta("final ", 1),
            text_delta("answer", 1),
            completed(final),
        ]
    )
    recorder = DeltaRecorder()
    response = await make_provider().complete(basic_request(), recorder)
    # Interleaved blocks stay apart by output_index.
    assert recorder.as_tuples == [
        ("thinking", "step one", 0),
        ("thinking", "step two", 0),
        ("text", "final ", 1),
        ("text", "answer", 1),
    ]
    assert response.message.content == [
        Thinking(text="step one\nstep two", signature=enc, family="openai_responses"),
        Text(text="final answer"),
    ]


@respx.mock
async def test_malformed_output_index_degrades_to_zero() -> None:
    _mock(
        [
            (
                "response.output_text.delta",
                {"type": "response.output_text.delta", "output_index": True, "delta": "a"},
            ),
            (
                "response.output_text.delta",
                {"type": "response.output_text.delta", "output_index": "3", "delta": "b"},
            ),
            completed(_final([_message_output("ab")])),
        ]
    )
    recorder = DeltaRecorder()
    await make_provider().complete(basic_request(), recorder)
    assert recorder.as_tuples == [("text", "a", 0), ("text", "b", 0)]


@respx.mock
async def test_unknown_events_nameless_frames_and_non_json_data_skipped() -> None:
    _mock(
        [
            ("response.banana.delta", {"type": "response.banana.delta", "delta": "nope"}),
            (None, "[DONE]"),
            # Nameless frame: the JSON type field is the fallback dispatch.
            (None, {"type": "response.output_text.delta", "output_index": 0, "delta": "o"}),
            (None, [1, 2]),
            text_delta("k"),
            ("response.unknown_terminal", {"type": "response.unknown_terminal"}),
            completed(_final([_message_output("ok")])),
        ]
    )
    recorder = DeltaRecorder()
    response = await make_provider().complete(basic_request(), recorder)
    assert recorder.as_tuples == [("text", "o", 0), ("text", "k", 0)]
    assert response.stop_reason == "end_turn"
    assert response.message.content == [Text(text="ok")]


@respx.mock
async def test_response_incomplete_max_output_tokens_maps_to_max_tokens() -> None:
    final = _final(
        [_message_output("partial")], status="incomplete", incomplete_reason="max_output_tokens"
    )
    _mock([text_delta("partial"), ("response.incomplete", {"type": "response.incomplete", "response": final})])
    response = await make_provider().complete(basic_request())
    assert response.stop_reason == "max_tokens"
    assert response.message.content == [Text(text="partial")]


@respx.mock
async def test_response_failed_without_error_payload_is_parsed_as_fatal() -> None:
    final = _final([], status="failed")
    _mock([("response.failed", {"type": "response.failed", "response": final})])
    with pytest.raises(Fatal):
        await make_provider().complete(basic_request())


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@respx.mock
async def test_mid_stream_transport_error_maps_to_transient() -> None:
    prefix = sse_bytes([text_delta("par")])
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            stream=RaisingStream([prefix], httpx.ReadError("connection reset")),
            headers={"content-type": "text/event-stream"},
        )
    )
    recorder = DeltaRecorder()
    with pytest.raises(Transient):
        await make_provider().complete(basic_request(), recorder)
    # Deltas already emitted are lost previews; the retry reissues the call.
    assert recorder.as_tuples == [("text", "par", 0)]


@respx.mock
async def test_stream_without_terminal_event_maps_to_transient() -> None:
    _mock([text_delta("par")])
    with pytest.raises(Transient, match="terminal"):
        await make_provider().complete(basic_request())


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ({"type": "error", "code": "server_error", "message": "boom"}, Transient),
        ({"type": "error", "code": "service_unavailable", "message": "x"}, Transient),
        ({"type": "error", "code": "rate_limit_exceeded", "message": "slow"}, RateLimited),
        ({"type": "error", "code": "context_length_exceeded", "message": "x"}, ContextOverflow),
        (
            {"type": "error", "code": "invalid_request", "message": "maximum context length is 8k"},
            ContextOverflow,
        ),
        ({"type": "error", "code": "invalid_prompt", "message": "rejected"}, Fatal),
        # A codeless frame is a truncated stream the gateway emits under load.
        ({"type": "error", "code": "", "message": ""}, Transient),
    ],
)
@respx.mock
async def test_top_level_error_event_translates_by_code(
    error: dict[str, Any], expected: type[Exception]
) -> None:
    _mock([text_delta("par"), ("error", error)])
    with pytest.raises(expected):
        await make_provider().complete(basic_request())


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ({"code": "invalid_prompt", "message": "rejected"}, Fatal),
        ({"message": "something went wrong"}, Transient),
        ({"code": "server_error", "message": "x"}, Transient),
    ],
)
@respx.mock
async def test_response_failed_with_error_payload_translates(
    error: dict[str, Any], expected: type[Exception]
) -> None:
    failed = _final([], status="failed")
    failed["error"] = error
    _mock([("response.failed", {"type": "response.failed", "response": failed})])
    with pytest.raises(expected):
        await make_provider().complete(basic_request())


@respx.mock
async def test_429_on_stream_open_carries_retry_after() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(429, json={"error": "rate_limited"}, headers={"Retry-After": "7"})
    )
    with pytest.raises(RateLimited) as ex:
        await make_provider().complete(basic_request())
    assert ex.value.retry_after == 7.0


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class _HangingStream(httpx.AsyncByteStream):
    """Yields a first frame, then waits forever; records whether it was closed."""

    def __init__(self, first: bytes) -> None:
        self._first = first
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._first
        self.started.set()
        await asyncio.Event().wait()
        yield b""  # pragma: no cover

    async def aclose(self) -> None:
        self.closed = True


@respx.mock
async def test_cancellation_mid_stream_propagates_and_closes_the_stream() -> None:
    stream = _HangingStream(sse_bytes([text_delta("par")]))
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, stream=stream, headers={"content-type": "text/event-stream"}
        )
    )
    recorder = DeltaRecorder()
    task = asyncio.create_task(make_provider().complete(basic_request(), recorder))
    await asyncio.wait_for(stream.started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorder.as_tuples == [("text", "par", 0)]
    assert stream.closed
