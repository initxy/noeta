"""Streaming test matrix for :class:`noeta.providers.anthropic.AnthropicProvider`.

Token-streaming Slice 2: ``complete_streaming`` speaks the Anthropic Messages
streaming protocol (SSE) and must stay shape-identical to the batch path —
the exact same request body plus ``stream: true``, the same neutral error
taxonomy, and a final ``LLMResponse`` equal to what ``complete`` parses from
the equivalent non-streaming body. Only text / thinking fragments surface as
:class:`StreamDelta`; tool arguments and signatures accumulate silently.

All HTTP traffic is mocked via ``respx`` (mirroring
``tests/test_provider_anthropic.py``); SSE fixtures are raw byte bodies, and
the mid-stream disconnect case uses a custom ``httpx.SyncByteStream`` that
raises while iterating.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

import httpx
import pytest
import respx

from noeta.protocols.errors import (
    ContextOverflowError,
    FatalError,
    TransientError,
)
from noeta.protocols.messages import (
    ImageBlock,
    LLMRequest,
    Message,
    StreamDelta,
    StreamingProvider,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.values import ContentRef
from noeta.providers.anthropic import AnthropicProvider


BASE_URL = "https://api.anthropic.test"
MESSAGES_ENDPOINT = f"{BASE_URL}/v1/messages"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _make_provider(**overrides: Any) -> AnthropicProvider:
    kwargs: dict[str, Any] = {
        "api_key": "sk-ant-test",
        "base_url": BASE_URL,
        "default_max_tokens": 4096,
    }
    kwargs.update(overrides)
    return AnthropicProvider(**kwargs)


def _basic_request(text: str = "hi") -> LLMRequest:
    return LLMRequest(
        model="claude-opus-4-7",
        messages=[Message(role="user", content=[TextBlock(text=text)])],
    )


def _sse(*events: tuple[str, dict[str, Any]]) -> bytes:
    """Render ``(event_name, payload)`` pairs as a raw SSE byte body."""
    frames = [
        f"event: {name}\ndata: {json.dumps(payload)}\n\n"
        for name, payload in events
    ]
    return "".join(frames).encode("utf-8")


def _sse_response(body: bytes) -> httpx.Response:
    return httpx.Response(
        200, content=body, headers={"content-type": "text/event-stream"}
    )


_DEFAULT_USAGE: dict[str, Any] = {"input_tokens": 10, "output_tokens": 1}


def _message_start(
    usage: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    return (
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_stream",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": usage if usage is not None else dict(_DEFAULT_USAGE),
            },
        },
    )


def _block_start(index: int, block: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return (
        "content_block_start",
        {"type": "content_block_start", "index": index, "content_block": block},
    )


def _block_delta(index: int, delta: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return (
        "content_block_delta",
        {"type": "content_block_delta", "index": index, "delta": delta},
    )


def _block_stop(index: int) -> tuple[str, dict[str, Any]]:
    return ("content_block_stop", {"type": "content_block_stop", "index": index})


def _message_delta(
    stop_reason: str, output_tokens: int
) -> tuple[str, dict[str, Any]]:
    return (
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        },
    )


_MESSAGE_STOP: tuple[str, dict[str, Any]] = ("message_stop", {"type": "message_stop"})
_PING: tuple[str, dict[str, Any]] = ("ping", {"type": "ping"})


class _DeltaRecorder:
    def __init__(self) -> None:
        self.deltas: list[StreamDelta] = []

    def __call__(self, delta: StreamDelta) -> None:
        self.deltas.append(delta)


# ---------------------------------------------------------------------------
# Capability probe
# ---------------------------------------------------------------------------


def test_provider_satisfies_streaming_protocol() -> None:
    assert isinstance(_make_provider(), StreamingProvider)


# ---------------------------------------------------------------------------
# Text-only stream
# ---------------------------------------------------------------------------


@respx.mock
def test_text_stream_emits_ordered_deltas_and_batch_shaped_response() -> None:
    body = _sse(
        _message_start(
            usage={
                "input_tokens": 100,
                "cache_read_input_tokens": 25,
                "cache_creation_input_tokens": 50,
                "output_tokens": 1,
            }
        ),
        _PING,
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "Hel"}),
        _block_delta(0, {"type": "text_delta", "text": "lo!"}),
        _block_stop(0),
        _message_delta("end_turn", output_tokens=7),
        _MESSAGE_STOP,
    )
    route = respx.post(MESSAGES_ENDPOINT).mock(return_value=_sse_response(body))

    sink = _DeltaRecorder()
    response = _make_provider().complete_streaming(_basic_request(), sink)

    assert sink.deltas == [
        StreamDelta(kind="text", text="Hel", index=0),
        StreamDelta(kind="text", text="lo!", index=0),
    ]
    assert response.stop_reason == "end_turn"
    assert response.content == [TextBlock(text="Hello!")]
    # input_tokens is the uncached split; cache read/write ride their own
    # fields; output comes from the terminal message_delta.
    assert response.usage == Usage(
        uncached=100, cache_read=25, cache_write=50, output=7
    )
    assert response.usage.input == 175
    # The outbound body carries the stream flag.
    sent = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert sent["stream"] is True
    # raw stays useful for diagnostics: the reconstructed message dict.
    assert response.raw is not None and response.raw["id"] == "msg_stream"


@respx.mock
def test_stream_request_body_matches_batch_body_plus_stream_flag() -> None:
    batch_body = {
        "id": "msg_batch",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": dict(_DEFAULT_USAGE),
    }
    stream_body = _sse(
        _message_start(),
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "ok"}),
        _block_stop(0),
        _message_delta("end_turn", output_tokens=2),
        _MESSAGE_STOP,
    )
    route = respx.post(MESSAGES_ENDPOINT).mock(
        side_effect=[
            httpx.Response(200, json=batch_body),
            _sse_response(stream_body),
        ]
    )

    provider = _make_provider()
    request = _basic_request()
    provider.complete(request)
    provider.complete_streaming(request, _DeltaRecorder())

    batch_sent = json.loads(route.calls[0].request.content.decode("utf-8"))
    stream_sent = json.loads(route.calls[1].request.content.decode("utf-8"))
    # Same request-body building path — the only difference is the flag.
    assert stream_sent.pop("stream") is True
    assert stream_sent == batch_sent


@respx.mock
def test_streaming_request_headers_merge_over_client_headers() -> None:
    body = _sse(
        _message_start(),
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "ok"}),
        _block_stop(0),
        _message_delta("end_turn", output_tokens=2),
        _MESSAGE_STOP,
    )
    route = respx.post(MESSAGES_ENDPOINT).mock(return_value=_sse_response(body))

    _make_provider().complete_streaming(
        _basic_request(), _DeltaRecorder(), {"x-noeta-task": "task-abc"}
    )

    request = route.calls.last.request
    # Per-request headers attach; the shared client's constructor headers
    # survive alongside them — same merge as complete_with_headers.
    assert request.headers["x-noeta-task"] == "task-abc"
    assert request.headers["x-api-key"] == "sk-ant-test"


# ---------------------------------------------------------------------------
# tool_use stream (arguments accumulate silently)
# ---------------------------------------------------------------------------


@respx.mock
def test_tool_use_stream_never_emits_argument_deltas() -> None:
    body = _sse(
        _message_start(),
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "Checking."}),
        _block_stop(0),
        _block_start(
            1,
            {"type": "tool_use", "id": "toolu_01", "name": "get_weather", "input": {}},
        ),
        _block_delta(1, {"type": "input_json_delta", "partial_json": ""}),
        _block_delta(1, {"type": "input_json_delta", "partial_json": '{"city": "Par'}),
        _block_delta(1, {"type": "input_json_delta", "partial_json": 'is", "unit": "c"}'}),
        _block_stop(1),
        _message_delta("tool_use", output_tokens=15),
        _MESSAGE_STOP,
    )
    respx.post(MESSAGES_ENDPOINT).mock(return_value=_sse_response(body))

    sink = _DeltaRecorder()
    response = _make_provider().complete_streaming(_basic_request(), sink)

    # No delta for tool arguments — only the text fragment surfaced.
    assert sink.deltas == [StreamDelta(kind="text", text="Checking.", index=0)]
    assert response.stop_reason == "tool_use"
    assert response.content == [
        TextBlock(text="Checking."),
        ToolUseBlock(
            call_id="toolu_01",
            tool_name="get_weather",
            arguments={"city": "Paris", "unit": "c"},
        ),
    ]


@respx.mock
def test_tool_use_with_no_argument_fragments_keeps_empty_input() -> None:
    body = _sse(
        _message_start(),
        _block_start(
            0, {"type": "tool_use", "id": "toolu_02", "name": "list_files", "input": {}}
        ),
        _block_stop(0),
        _message_delta("tool_use", output_tokens=4),
        _MESSAGE_STOP,
    )
    respx.post(MESSAGES_ENDPOINT).mock(return_value=_sse_response(body))

    response = _make_provider().complete_streaming(
        _basic_request(), _DeltaRecorder()
    )

    assert response.content == [
        ToolUseBlock(call_id="toolu_02", tool_name="list_files", arguments={})
    ]


# ---------------------------------------------------------------------------
# thinking + signature stream
# ---------------------------------------------------------------------------


@respx.mock
def test_thinking_stream_emits_thinking_deltas_and_silent_signature() -> None:
    body = _sse(
        _message_start(),
        _block_start(0, {"type": "thinking", "thinking": "", "signature": ""}),
        _block_delta(0, {"type": "thinking_delta", "thinking": "step 1"}),
        _block_delta(0, {"type": "thinking_delta", "thinking": " step 2"}),
        _block_delta(0, {"type": "signature_delta", "signature": "sig-abc"}),
        _block_stop(0),
        _block_start(1, {"type": "text", "text": ""}),
        _block_delta(1, {"type": "text_delta", "text": "answer"}),
        _block_stop(1),
        _message_delta("end_turn", output_tokens=9),
        _MESSAGE_STOP,
    )
    respx.post(MESSAGES_ENDPOINT).mock(return_value=_sse_response(body))

    sink = _DeltaRecorder()
    response = _make_provider().complete_streaming(_basic_request(), sink)

    # Thinking fragments surface (kind="thinking"); the signature never does.
    assert sink.deltas == [
        StreamDelta(kind="thinking", text="step 1", index=0),
        StreamDelta(kind="thinking", text=" step 2", index=0),
        StreamDelta(kind="text", text="answer", index=1),
    ]
    assert response.content == [
        ThinkingBlock(text="step 1 step 2", signature="sig-abc"),
        TextBlock(text="answer"),
    ]


@respx.mock
def test_redacted_thinking_block_rides_content_block_start_whole() -> None:
    body = _sse(
        _message_start(),
        _block_start(0, {"type": "redacted_thinking", "data": "opaque-blob"}),
        _block_stop(0),
        _block_start(1, {"type": "text", "text": ""}),
        _block_delta(1, {"type": "text_delta", "text": "done"}),
        _block_stop(1),
        _message_delta("end_turn", output_tokens=3),
        _MESSAGE_STOP,
    )
    respx.post(MESSAGES_ENDPOINT).mock(return_value=_sse_response(body))

    sink = _DeltaRecorder()
    response = _make_provider().complete_streaming(_basic_request(), sink)

    # The opaque blob never surfaces as a delta but round-trips on the block.
    assert sink.deltas == [StreamDelta(kind="text", text="done", index=1)]
    assert response.content == [
        ThinkingBlock(text="", signature=None, data="opaque-blob"),
        TextBlock(text="done"),
    ]


# ---------------------------------------------------------------------------
# stop_reason mapping
# ---------------------------------------------------------------------------


@respx.mock
def test_max_tokens_stop_reason_maps_through() -> None:
    body = _sse(
        _message_start(),
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "trunca"}),
        _block_stop(0),
        _message_delta("max_tokens", output_tokens=4096),
        _MESSAGE_STOP,
    )
    respx.post(MESSAGES_ENDPOINT).mock(return_value=_sse_response(body))

    response = _make_provider().complete_streaming(
        _basic_request(), _DeltaRecorder()
    )

    assert response.stop_reason == "max_tokens"


# ---------------------------------------------------------------------------
# ② error recovery — same neutral taxonomy as the batch path
# ---------------------------------------------------------------------------


class _DisconnectingStream(httpx.SyncByteStream):
    """Yields a valid stream prefix, then dies mid-flight."""

    def __init__(self, prefix: bytes) -> None:
        self._prefix = prefix

    def __iter__(self) -> Iterator[bytes]:
        yield self._prefix
        raise httpx.ReadError("connection reset mid-stream")


@respx.mock
def test_mid_stream_disconnect_maps_to_transient() -> None:
    prefix = _sse(
        _message_start(),
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "par"}),
    )
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, stream=_DisconnectingStream(prefix))
    )

    with pytest.raises(TransientError):
        _make_provider().complete_streaming(_basic_request(), _DeltaRecorder())


@respx.mock
def test_connect_error_on_stream_open_maps_to_transient() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    with pytest.raises(TransientError):
        _make_provider().complete_streaming(_basic_request(), _DeltaRecorder())


@respx.mock
def test_http_429_with_retry_after_maps_to_transient() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            429,
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
            headers={"retry-after": "5"},
        )
    )
    with pytest.raises(TransientError) as ex:
        _make_provider().complete_streaming(_basic_request(), _DeltaRecorder())
    assert ex.value.retry_after == 5.0


@respx.mock
def test_http_400_prompt_too_long_maps_to_overflow() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            400,
            json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "prompt is too long: 250000 tokens > 200000 maximum",
                },
            },
        )
    )
    with pytest.raises(ContextOverflowError):
        _make_provider().complete_streaming(_basic_request(), _DeltaRecorder())


@respx.mock
def test_http_401_maps_to_fatal() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            401,
            json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}},
        )
    )
    with pytest.raises(FatalError):
        _make_provider().complete_streaming(_basic_request(), _DeltaRecorder())


@respx.mock
def test_vendor_error_event_overloaded_maps_to_transient() -> None:
    body = _sse(
        _message_start(),
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "par"}),
        (
            "error",
            {
                "type": "error",
                "error": {"type": "overloaded_error", "message": "Overloaded"},
            },
        ),
    )
    respx.post(MESSAGES_ENDPOINT).mock(return_value=_sse_response(body))

    with pytest.raises(TransientError):
        _make_provider().complete_streaming(_basic_request(), _DeltaRecorder())


@respx.mock
def test_vendor_error_event_unknown_type_maps_to_fatal() -> None:
    body = _sse(
        _message_start(),
        (
            "error",
            {
                "type": "error",
                "error": {"type": "authentication_error", "message": "bad key"},
            },
        ),
    )
    respx.post(MESSAGES_ENDPOINT).mock(return_value=_sse_response(body))

    with pytest.raises(FatalError):
        _make_provider().complete_streaming(_basic_request(), _DeltaRecorder())


@respx.mock
def test_vision_guard_applies_before_stream_open() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=_sse_response(_sse(_message_start(), _MESSAGE_STOP))
    )
    ref = ContentRef(hash="sha256:img", size=3, media_type="image/png")
    request = LLMRequest(
        model="claude-opus-4-7",
        messages=[Message(role="user", content=[ImageBlock(source=ref)])],
    )
    with pytest.raises(FatalError, match="not vision-capable"):
        _make_provider().complete_streaming(request, _DeltaRecorder())
    assert not route.called


# ---------------------------------------------------------------------------
# Shape identity with the batch path
# ---------------------------------------------------------------------------


@respx.mock
def test_streamed_response_matches_batch_parse_of_equivalent_body() -> None:
    """The recording invariant: a streamed exchange parses to the same
    ``LLMResponse`` fields as the batch parse of the equivalent non-streaming
    body. ``raw`` is diagnostics-only (not part of the recording), so the
    invariant covers stop_reason / content / usage."""
    usage = {
        "input_tokens": 100,
        "cache_read_input_tokens": 25,
        "cache_creation_input_tokens": 50,
        "output_tokens": 7,
    }
    batch_body = {
        "id": "msg_stream",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [
            {"type": "thinking", "thinking": "hm", "signature": "sig"},
            {"type": "text", "text": "Hello!"},
            {"type": "tool_use", "id": "toolu_01", "name": "get_weather", "input": {"city": "Paris"}},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": usage,
    }
    stream_body = _sse(
        _message_start(usage={**usage, "output_tokens": 1}),
        _block_start(0, {"type": "thinking", "thinking": "", "signature": ""}),
        _block_delta(0, {"type": "thinking_delta", "thinking": "hm"}),
        _block_delta(0, {"type": "signature_delta", "signature": "sig"}),
        _block_stop(0),
        _block_start(1, {"type": "text", "text": ""}),
        _block_delta(1, {"type": "text_delta", "text": "Hel"}),
        _block_delta(1, {"type": "text_delta", "text": "lo!"}),
        _block_stop(1),
        _block_start(
            2,
            {"type": "tool_use", "id": "toolu_01", "name": "get_weather", "input": {}},
        ),
        _block_delta(2, {"type": "input_json_delta", "partial_json": '{"city": "Paris"}'}),
        _block_stop(2),
        _message_delta("tool_use", output_tokens=7),
        _MESSAGE_STOP,
    )
    respx.post(MESSAGES_ENDPOINT).mock(
        side_effect=[
            httpx.Response(200, json=batch_body),
            _sse_response(stream_body),
        ]
    )

    provider = _make_provider()
    request = _basic_request()
    batch = provider.complete(request)
    streamed = provider.complete_streaming(request, _DeltaRecorder())

    assert streamed.stop_reason == batch.stop_reason
    assert streamed.content == batch.content
    assert streamed.usage == batch.usage


# ---------------------------------------------------------------------------
# Forward compatibility — unknown events / delta types skipped silently
# ---------------------------------------------------------------------------


@respx.mock
def test_unknown_event_and_delta_types_are_skipped_silently() -> None:
    body = _sse(
        _message_start(),
        ("some_future_event", {"type": "some_future_event", "x": 1}),
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "ok"}),
        _block_delta(0, {"type": "sparkle_delta", "sparkle": "??"}),
        _block_stop(0),
        _message_delta("end_turn", output_tokens=2),
        _MESSAGE_STOP,
    )
    respx.post(MESSAGES_ENDPOINT).mock(return_value=_sse_response(body))

    sink = _DeltaRecorder()
    response = _make_provider().complete_streaming(_basic_request(), sink)

    assert sink.deltas == [StreamDelta(kind="text", text="ok", index=0)]
    assert response.stop_reason == "end_turn"
    assert response.content == [TextBlock(text="ok")]
