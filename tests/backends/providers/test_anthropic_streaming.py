"""Streaming behavior of :class:`AnthropicProvider`.

Only text / thinking fragments surface as :class:`Delta`; tool arguments and
signatures accumulate silently, because a half-parsed JSON fragment is not
something a host can render. A fragmented stream parses to the same response
as the same message delivered whole. Cancellation is asyncio cancellation and
closes the connection.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from noeta.backends.providers.anthropic import AnthropicProvider
from noeta.core import models
from noeta.core.errors import (
    ContextOverflow,
    Fatal,
    MalformedToolArguments,
    RateLimited,
    StreamBroken,
    TimedOut,
    Transient,
)
from noeta.core.provider import Provider
from noeta.core.types import (
    Delta,
    Image,
    Message,
    ModelRequest,
    Text,
    Thinking,
    ToolUse,
    Usage,
    user,
)
from tests.backends.providers.sse import RaisingStream, sse_bytes, sse_response

BASE_URL = "https://api.anthropic.test"
MESSAGES_ENDPOINT = f"{BASE_URL}/v1/messages"
_TEXT_ONLY_MODEL = "text-only-model"

Frame = tuple[str | None, Any]


@pytest.fixture(autouse=True)
def _text_only_catalog_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        models,
        "_EXTENSIONS",
        {
            **models._EXTENSIONS,
            _TEXT_ONLY_MODEL: models.ModelSpec(
                real_model_id=_TEXT_ONLY_MODEL,
                context_window=128_000,
                max_output_tokens=16_384,
                supports_vision=False,
            ),
        },
    )


def _provider(**overrides: Any) -> AnthropicProvider:
    kwargs: dict[str, Any] = {
        "api_key": "sk-ant-test",
        "base_url": BASE_URL,
        "default_max_tokens": 4096,
    }
    kwargs.update(overrides)
    return AnthropicProvider(**kwargs)


def _req(text: str = "hi", **kw: Any) -> ModelRequest:
    return ModelRequest(model="claude-opus-4-7", messages=[user(text)], **kw)


def _message_start(usage: dict[str, Any] | None = None) -> Frame:
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
                "usage": usage if usage is not None else {"input_tokens": 10, "output_tokens": 1},
            },
        },
    )


def _block_start(index: int, block: dict[str, Any]) -> Frame:
    return ("content_block_start", {"type": "content_block_start", "index": index, "content_block": block})


def _block_delta(index: int, delta: dict[str, Any]) -> Frame:
    return ("content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta})


def _block_stop(index: int) -> Frame:
    return ("content_block_stop", {"type": "content_block_stop", "index": index})


def _message_delta(stop_reason: str, output_tokens: int = 2) -> Frame:
    return (
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        },
    )


_MESSAGE_STOP: Frame = ("message_stop", {"type": "message_stop"})
_PING: Frame = ("ping", {"type": "ping"})


def _tool_use(index: int, call_id: str, partial_json: str | None) -> list[Frame]:
    frames: list[Frame] = [
        _block_start(index, {"type": "tool_use", "id": call_id, "name": "Read", "input": {}})
    ]
    if partial_json is not None:
        frames.append(_block_delta(index, {"type": "input_json_delta", "partial_json": partial_json}))
    frames.append(_block_stop(index))
    return frames


async def _run(
    frames: list[Frame], request: ModelRequest | None = None
) -> tuple[list[Delta], Any]:
    respx.post(MESSAGES_ENDPOINT).mock(return_value=sse_response(sse_bytes(frames)))
    deltas: list[Delta] = []
    response = await _provider().complete(request or _req(), deltas.append)
    return deltas, response


def test_provider_satisfies_the_protocol() -> None:
    provider: Provider = _provider()
    assert provider.family == "anthropic"


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


@respx.mock
async def test_text_stream_emits_ordered_deltas_and_full_response() -> None:
    deltas, response = await _run(
        [
            _message_start(),
            _PING,
            _block_start(0, {"type": "text", "text": ""}),
            _block_delta(0, {"type": "text_delta", "text": "Hel"}),
            _block_delta(0, {"type": "text_delta", "text": "lo!"}),
            _block_stop(0),
            _message_delta("end_turn", 4),
            _MESSAGE_STOP,
        ]
    )
    assert deltas == [Delta(kind="text", text="Hel", index=0), Delta(kind="text", text="lo!", index=0)]
    assert response.stop_reason == "end_turn"
    assert response.message.content == [Text(text="Hello!")]
    # usage: input side from message_start, cumulative output from message_delta
    assert response.usage == Usage(uncached=10, output=4)


@respx.mock
async def test_on_delta_is_optional() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=sse_response(
            sse_bytes(
                [
                    _message_start(),
                    _block_start(0, {"type": "text", "text": ""}),
                    _block_delta(0, {"type": "text_delta", "text": "x"}),
                    _block_stop(0),
                    _message_delta("end_turn"),
                ]
            )
        )
    )
    response = await _provider().complete(_req())
    assert response.message.text == "x"


@respx.mock
async def test_streamed_overflow_stop_reason_raises_context_overflow() -> None:
    with pytest.raises(ContextOverflow):
        await _run([_message_start(), _message_delta("model_context_window_exceeded"), _MESSAGE_STOP])


@respx.mock
async def test_stream_body_and_request_headers() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=sse_response(sse_bytes([_message_start(), _message_delta("end_turn")]))
    )
    await _provider(extra_headers={"x-trace": "c"}).complete(_req(headers={"x-trace": "call"}))
    request = route.calls.last.request
    assert json.loads(request.content)["stream"] is True
    assert request.headers["x-trace"] == "call"
    assert request.headers["x-api-key"] == "sk-ant-test"


# ---------------------------------------------------------------------------
# tool_use (arguments accumulate silently)
# ---------------------------------------------------------------------------


@respx.mock
async def test_tool_use_stream_never_emits_argument_deltas() -> None:
    deltas, response = await _run(
        [
            _message_start(),
            _block_start(0, {"type": "tool_use", "id": "toolu_01", "name": "get_weather", "input": {}}),
            _block_delta(0, {"type": "input_json_delta", "partial_json": '{"city": '}),
            _block_delta(0, {"type": "input_json_delta", "partial_json": '"Paris"}'}),
            _block_stop(0),
            _message_delta("tool_use"),
            _MESSAGE_STOP,
        ]
    )
    assert deltas == []
    assert response.stop_reason == "tool_use"
    assert response.message.content == [ToolUse(id="toolu_01", name="get_weather", input={"city": "Paris"})]


@respx.mock
async def test_tool_use_with_no_argument_fragments_keeps_empty_input() -> None:
    _, response = await _run([_message_start(), *_tool_use(0, "t", None), _message_delta("tool_use")])
    assert response.message.content == [ToolUse(id="t", name="Read", input={})]


@respx.mock
async def test_tool_use_without_content_block_stop_is_still_decoded() -> None:
    _, response = await _run(
        [
            _message_start(),
            _block_start(0, {"type": "tool_use", "id": "t", "name": "Read", "input": {}}),
            _block_delta(0, {"type": "input_json_delta", "partial_json": '{"a": 1}'}),
            _message_delta("tool_use"),
        ]
    )
    assert response.message.content == [ToolUse(id="t", name="Read", input={"a": 1})]


@respx.mock
async def test_max_tokens_drops_the_call_cut_mid_json() -> None:
    _, response = await _run(
        [
            _message_start(),
            *_tool_use(0, "t1", '{"file_path":"/a"}'),
            *_tool_use(1, "t2", '{"file_pa'),
            _message_delta("max_tokens", 50),
            _MESSAGE_STOP,
        ]
    )
    assert response.stop_reason == "max_tokens"
    assert response.message.content == [ToolUse(id="t1", name="Read", input={"file_path": "/a"})]


@respx.mock
async def test_undecodable_input_without_max_tokens_still_raises() -> None:
    with pytest.raises(MalformedToolArguments, match="tool_use input was not valid JSON"):
        await _run([_message_start(), *_tool_use(0, "t1", '{"file_pa'), _message_delta("tool_use")])


@respx.mock
async def test_refusal_interrupting_a_tool_call_ends_the_turn() -> None:
    _, response = await _run(
        [
            _message_start(),
            _block_start(0, {"type": "text", "text": "I can't"}),
            _block_stop(0),
            *_tool_use(1, "t1", '{"file_path":"/a"}'),
            _message_delta("refusal"),
            _MESSAGE_STOP,
        ]
    )
    assert response.stop_reason == "refusal"
    assert response.message.content == [Text(text="I can't")]


@respx.mock
async def test_stream_closed_before_message_delta_is_transient() -> None:
    with pytest.raises(Transient, match="no stop_reason"):
        await _run(
            [
                _message_start(),
                _block_start(0, {"type": "text", "text": ""}),
                _block_delta(0, {"type": "text_delta", "text": "half an ans"}),
            ]
        )


# ---------------------------------------------------------------------------
# thinking + signature
# ---------------------------------------------------------------------------


@respx.mock
async def test_thinking_stream_emits_thinking_deltas_and_silent_signature() -> None:
    deltas, response = await _run(
        [
            _message_start(),
            _block_start(0, {"type": "thinking", "thinking": "", "signature": ""}),
            _block_delta(0, {"type": "thinking_delta", "thinking": "let me "}),
            _block_delta(0, {"type": "thinking_delta", "thinking": "think"}),
            _block_delta(0, {"type": "signature_delta", "signature": "sig-"}),
            _block_delta(0, {"type": "signature_delta", "signature": "abc"}),
            _block_stop(0),
            _block_start(1, {"type": "text", "text": ""}),
            _block_delta(1, {"type": "text_delta", "text": "answer"}),
            _block_stop(1),
            _message_delta("end_turn"),
        ]
    )
    finished = Thinking(text="let me think", signature="sig-abc", family="anthropic")
    assert deltas == [
        Delta(kind="thinking", text="let me ", index=0),
        Delta(kind="thinking", text="think", index=0),
        Delta(kind="block", index=0, block=finished),
        Delta(kind="text", text="answer", index=1),
    ]
    assert response.message.content == [
        Thinking(text="let me think", signature="sig-abc", family="anthropic"),
        Text(text="answer"),
    ]


@respx.mock
async def test_redacted_thinking_block_rides_content_block_start_whole() -> None:
    deltas, response = await _run(
        [
            _message_start(),
            _block_start(0, {"type": "redacted_thinking", "data": "ENCRYPTED"}),
            _block_stop(0),
            _block_start(1, {"type": "text", "text": "ok"}),
            _block_stop(1),
            _message_delta("end_turn"),
        ]
    )
    assert deltas == [Delta(kind="block", index=0, block=Thinking(
        text="", data="ENCRYPTED", family="anthropic"))]
    assert response.message.content == [
        Thinking(text="", data="ENCRYPTED", family="anthropic"),
        Text(text="ok"),
    ]


@respx.mock
async def test_only_a_finished_thinking_block_with_content_is_handed_over_whole() -> None:
    deltas, _ = await _run(
        [
            _message_start(),
            _block_start(0, {"type": "thinking", "thinking": "", "signature": ""}),
            _block_delta(0, {"type": "signature_delta", "signature": ""}),
            _block_stop(0),
            _block_start(1, {"type": "thinking", "thinking": "", "signature": ""}),
            _block_delta(1, {"type": "thinking_delta", "thinking": "still going"}),
            _block_start(2, {"type": "text", "text": ""}),
            _block_delta(2, {"type": "text_delta", "text": "ok"}),
            _block_stop(2),
            _message_delta("end_turn"),
        ]
    )
    # The empty block and the one that never ended are not handed over.
    assert [d.kind for d in deltas] == ["thinking", "text"]


# ---------------------------------------------------------------------------
# Empty text blocks
# ---------------------------------------------------------------------------


@respx.mock
async def test_streamed_empty_text_block_before_a_tool_call_is_dropped() -> None:
    _, response = await _run(
        [
            _message_start(),
            _block_start(0, {"type": "text", "text": ""}),
            _block_stop(0),
            *_tool_use(1, "t", '{"a": 1}'),
            _message_delta("tool_use"),
        ]
    )
    assert response.message.content == [ToolUse(id="t", name="Read", input={"a": 1})]


@respx.mock
async def test_streamed_whitespace_only_text_block_is_dropped_real_text_kept() -> None:
    _, response = await _run(
        [
            _message_start(),
            _block_start(0, {"type": "text", "text": ""}),
            _block_delta(0, {"type": "text_delta", "text": "\n\n"}),
            _block_stop(0),
            _block_start(1, {"type": "text", "text": ""}),
            _block_delta(1, {"type": "text_delta", "text": "real"}),
            _block_stop(1),
            _message_delta("end_turn"),
        ]
    )
    assert response.message.content == [Text(text="real")]


@respx.mock
async def test_streamed_message_of_only_an_empty_text_block_is_empty() -> None:
    _, response = await _run(
        [_message_start(), _block_start(0, {"type": "text", "text": ""}), _block_stop(0), _message_delta("end_turn")]
    )
    assert response.message.content == []
    assert response.stop_reason == "end_turn"


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


@respx.mock
async def test_mid_stream_disconnect_maps_to_transient() -> None:
    head = sse_bytes([_message_start(), _block_start(0, {"type": "text", "text": ""})])
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            stream=RaisingStream([head], httpx.RemoteProtocolError("peer closed")),
            headers={"content-type": "text/event-stream"},
        )
    )
    with pytest.raises(Transient):
        await _provider().complete(_req())


@respx.mock
async def test_connect_error_on_stream_open_maps_to_transient() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(Transient):
        await _provider().complete(_req())


@respx.mock
async def test_http_429_with_retry_after_maps_to_rate_limited() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            429,
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
            headers={"retry-after": "7"},
        )
    )
    with pytest.raises(RateLimited) as ex:
        await _provider().complete(_req())
    assert ex.value.retry_after == 7.0


@respx.mock
async def test_http_400_prompt_too_long_maps_to_overflow() -> None:
    """The status error is raised before any frame is read, and the body is
    still read back for the overflow sniff."""
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "prompt is too long: 250000 tokens"},
            },
        )
    )
    with pytest.raises(ContextOverflow):
        await _provider().complete(_req())


@respx.mock
async def test_http_401_maps_to_fatal() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad"}}
        )
    )
    with pytest.raises(Fatal):
        await _provider().complete(_req())


def _error_event(err_type: str, message: str = "m") -> Frame:
    return ("error", {"type": "error", "error": {"type": err_type, "message": message}})


@respx.mock
@pytest.mark.parametrize("err_type", ["overloaded_error", "api_error"])
async def test_vendor_error_event_retryable_types_map_to_transient(err_type: str) -> None:
    with pytest.raises(Transient) as ex:
        await _run([_message_start(), _error_event(err_type)])
    assert not isinstance(ex.value, RateLimited)


@respx.mock
@pytest.mark.parametrize(("err_type", "again"), [("overloaded_error", 2), ("api_error", 0)])
async def test_a_server_error_mid_stream_breaks_the_stream(err_type: str, again: int) -> None:
    """Claude Code streams again twice after an overload, and goes on in one
    piece at once after any other server error."""
    with pytest.raises(StreamBroken) as ex:
        await _run([_message_start(), _error_event(err_type)])
    assert ex.value.stream_retries == again


@respx.mock
async def test_a_dropped_connection_or_silence_mid_stream_breaks_the_stream() -> None:
    head = sse_bytes([_message_start(), _block_start(0, {"type": "text", "text": ""})])
    for error, again in ((httpx.RemoteProtocolError("peer closed"), None),
                         (httpx.ReadTimeout("silent"), 1)):
        respx.post(MESSAGES_ENDPOINT).mock(return_value=httpx.Response(
            200, stream=RaisingStream([head], error),
            headers={"content-type": "text/event-stream"}))
        with pytest.raises(StreamBroken) as ex:
            await _provider().complete(_req())
        assert ex.value.stream_retries == again
    # Before the response began it is an ordinary transient failure.
    respx.post(MESSAGES_ENDPOINT).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(Transient) as plain:
        await _provider().complete(_req())
    assert not isinstance(plain.value, StreamBroken)


@respx.mock
async def test_a_call_answered_in_one_piece_as_claude_code_falls_back() -> None:
    """``complete_whole``: the same body without streaming, the output
    capped at 64,000 tokens, a 300 s client timeout."""
    whole = {"id": "m", "type": "message", "role": "assistant", "model": "claude-opus-4-7",
             "content": [{"type": "text", "text": "whole"}], "stop_reason": "end_turn",
             "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 3}}
    route = respx.post(MESSAGES_ENDPOINT).mock(return_value=httpx.Response(200, json=whole))
    response = await _provider().complete_whole(_req(max_tokens=128_000))
    assert response.message.content == [Text(text="whole")]
    sent = route.calls.last.request
    body = json.loads(sent.content)
    assert body["stream"] is False and body["max_tokens"] == 64_000
    assert sent.extensions["timeout"]["read"] == 300.0
    small = await _provider().complete_whole(_req(max_tokens=2_000))
    assert small.message.content == [Text(text="whole")]
    assert json.loads(route.calls.last.request.content)["max_tokens"] == 2_000


@respx.mock
async def test_a_call_in_one_piece_that_outlasts_its_limit_times_out() -> None:
    """The limit bounds the whole call, not each read: a server that holds
    the answer back past it is :class:`TimedOut`."""
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    respx.post(MESSAGES_ENDPOINT).mock(side_effect=slow)
    with pytest.raises(TimedOut, match=r"0\.05 s"):
        await _provider(whole_timeout_seconds=0.05).complete_whole(_req())
    respx.post(MESSAGES_ENDPOINT).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(TimedOut):
        await _provider().complete_whole(_req())


@respx.mock
async def test_a_call_in_one_piece_answered_by_a_proxy_page_fails_at_once() -> None:
    """A body that is not a JSON message (a gateway's HTML page) is not
    retried, as on the streaming path."""
    from noeta.core.retry import complete_with_retries

    route = respx.post(MESSAGES_ENDPOINT).mock(return_value=httpx.Response(
        200, text="<html>login</html>", headers={"content-type": "text/html"}))
    with pytest.raises(Fatal, match="text/html"):
        await _provider().complete_whole(_req())
    route.mock(return_value=httpx.Response(200, json=["not", "a", "message"]))
    with pytest.raises(Fatal, match="not a JSON message"):
        await _provider().complete_whole(_req())

    class Broken(AnthropicProvider):
        async def complete(self, request: Any, on_delta: Any = None) -> Any:
            raise StreamBroken("cut")

    route.mock(return_value=httpx.Response(200, text="<html/>"))
    before = route.call_count
    with pytest.raises(Fatal, match="not a JSON message"):
        await complete_with_retries(
            Broken(api_key="k", base_url=BASE_URL, default_max_tokens=4096), _req())
    assert route.call_count == before + 1


@respx.mock
async def test_a_broken_stream_goes_on_in_one_piece_through_the_retry_helper() -> None:
    """The recorded Claude Code sequence: a stream cut by a server error,
    then non-streaming calls (a 504 retried) until one answers."""
    from noeta.core.retry import complete_with_retries

    whole = {"id": "m", "type": "message", "role": "assistant", "model": "claude-opus-4-7",
             "content": [{"type": "text", "text": "summary"}], "stop_reason": "end_turn",
             "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 3}}
    broken = sse_response(sse_bytes([_message_start(), _error_event("api_error")]))
    route = respx.post(MESSAGES_ENDPOINT).mock(side_effect=[
        broken, httpx.Response(504, json={"type": "error", "error": {
            "type": "api_error", "message": "upstream timeout"}}),
        httpx.Response(200, json=whole)])

    async def no_sleep(_: float) -> None:
        return None

    response = await complete_with_retries(_provider(), _req(), sleep=no_sleep)
    assert response.message.content == [Text(text="summary")]
    sent = [json.loads(c.request.content) for c in route.calls]
    assert [b["stream"] for b in sent] == [True, False, False]


@respx.mock
async def test_vendor_error_event_rate_limit_maps_to_rate_limited() -> None:
    with pytest.raises(RateLimited) as ex:
        await _run([_message_start(), _error_event("rate_limit_error")])
    assert ex.value.retry_after is None


@respx.mock
async def test_vendor_error_event_overflow_maps_to_context_overflow() -> None:
    with pytest.raises(ContextOverflow):
        await _run([_message_start(), _error_event("invalid_request_error", "prompt is too long")])


@respx.mock
async def test_vendor_error_event_unknown_type_maps_to_fatal() -> None:
    with pytest.raises(Fatal, match="mystery_error"):
        await _run([_message_start(), _error_event("mystery_error")])


@respx.mock
async def test_vision_guard_applies_before_stream_open() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(return_value=sse_response(b""))
    request = ModelRequest(
        model=_TEXT_ONLY_MODEL,
        messages=[Message(role="user", content=[Image(media_type="image/png", data=b"x")])],
    )
    with pytest.raises(Fatal):
        await _provider().complete(request)
    assert not route.called


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class _HangingStream(httpx.AsyncByteStream):
    """Yields ``head`` then waits forever; records whether it was closed."""

    def __init__(self, head: bytes) -> None:
        self.head = head
        self.closed = False
        self.started = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.head
        self.started.set()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@respx.mock
async def test_cancellation_mid_stream_propagates_and_closes_the_response() -> None:
    stream = _HangingStream(
        sse_bytes(
            [
                _message_start(),
                _block_start(0, {"type": "text", "text": ""}),
                _block_delta(0, {"type": "text_delta", "text": "partial"}),
            ]
        )
    )
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})
    )
    deltas: list[Delta] = []
    task = asyncio.create_task(_provider().complete(_req(), deltas.append))
    await asyncio.wait_for(stream.started.wait(), timeout=5)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
    assert deltas == [Delta(kind="text", text="partial", index=0)]


# ---------------------------------------------------------------------------
# Shape identity and forward compatibility
# ---------------------------------------------------------------------------


@respx.mock
async def test_fragmented_stream_matches_whole_block_delivery() -> None:
    usage = {
        "input_tokens": 100,
        "cache_read_input_tokens": 25,
        "cache_creation_input_tokens": 50,
        "output_tokens": 7,
    }
    whole = [
        _message_start(usage=dict(usage)),
        _block_start(0, {"type": "thinking", "thinking": "hm", "signature": "sig"}),
        _block_stop(0),
        _block_start(1, {"type": "text", "text": "Hello!"}),
        _block_stop(1),
        _block_start(2, {"type": "tool_use", "id": "toolu_01", "name": "get_weather", "input": {"city": "Paris"}}),
        _block_stop(2),
        _message_delta("tool_use", 7),
        _MESSAGE_STOP,
    ]
    fragmented = [
        _message_start(usage={**usage, "output_tokens": 1}),
        _block_start(0, {"type": "thinking", "thinking": "", "signature": ""}),
        _block_delta(0, {"type": "thinking_delta", "thinking": "hm"}),
        _block_delta(0, {"type": "signature_delta", "signature": "sig"}),
        _block_stop(0),
        _block_start(1, {"type": "text", "text": ""}),
        _block_delta(1, {"type": "text_delta", "text": "Hel"}),
        _block_delta(1, {"type": "text_delta", "text": "lo!"}),
        _block_stop(1),
        _block_start(2, {"type": "tool_use", "id": "toolu_01", "name": "get_weather", "input": {}}),
        _block_delta(2, {"type": "input_json_delta", "partial_json": '{"city": "Paris"}'}),
        _block_stop(2),
        _message_delta("tool_use", 7),
        _MESSAGE_STOP,
    ]
    _, a = await _run(whole)
    _, b = await _run(fragmented)
    assert a == b
    assert a.usage == Usage(uncached=100, cache_read=25, cache_write=50, output=7)


@respx.mock
async def test_unknown_event_and_delta_types_are_skipped_silently() -> None:
    deltas, response = await _run(
        [
            _message_start(),
            ("some_future_event", {"type": "some_future_event", "x": 1}),
            _block_start(0, {"type": "text", "text": ""}),
            _block_delta(0, {"type": "text_delta", "text": "ok"}),
            _block_delta(0, {"type": "sparkle_delta", "sparkle": "??"}),
            _block_delta(9, {"type": "text_delta", "text": "orphan"}),
            _block_stop(0),
            _message_delta("end_turn"),
            _MESSAGE_STOP,
        ]
    )
    assert deltas == [Delta(kind="text", text="ok", index=0)]
    assert response.message.content == [Text(text="ok")]
