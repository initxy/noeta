"""Streaming test matrix for ``OpenAICompatProvider.complete_streaming``.

Mirrors ``test_provider_openai_compat.py``: all HTTP traffic is mocked via
``respx`` (zero real network calls), here as ``text/event-stream`` bodies in
the Chat Completions chunk shape — nameless data-only SSE events terminated
by a ``data: [DONE]`` sentinel. The contract under test: deltas fire for
text / reasoning fragments only (tool-call fragments accumulate silently),
and the returned ``LLMResponse`` is shape-identical to what ``complete()``
produces for the equivalent batch body.
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
    LLMRequest,
    LLMResponse,
    Message,
    StreamDelta,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    Usage,
)
from noeta.providers.openai_compat import OpenAICompatProvider


BASE_URL = "https://example.test/v1"
CHAT_ENDPOINT = f"{BASE_URL}/chat/completions"

SSE_HEADERS = {"content-type": "text/event-stream"}


def _make_provider(**overrides: Any) -> OpenAICompatProvider:
    kwargs: dict[str, Any] = {
        "base_url": BASE_URL,
        "api_key": "sk-test",
    }
    kwargs.update(overrides)
    return OpenAICompatProvider(**kwargs)


def _basic_request(*, text: str = "hi") -> LLMRequest:
    return LLMRequest(
        model="gpt-4o",
        messages=[Message(role="user", content=[TextBlock(text=text)])],
    )


def _chunk(
    *,
    delta: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
    choices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One Chat Completions stream chunk (``object: chat.completion.chunk``)."""
    chunk: dict[str, Any] = {
        "id": "chatcmpl-stream",
        "object": "chat.completion.chunk",
        "model": "gpt-4o",
    }
    if choices is not None:
        chunk["choices"] = choices
    else:
        chunk["choices"] = [
            {"index": 0, "delta": delta or {}, "finish_reason": finish_reason}
        ]
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def _sse_body(*payloads: Any, done: bool = True) -> bytes:
    """Encode chunks as nameless data-only SSE events (str payloads verbatim)."""
    frames: list[str] = []
    for payload in payloads:
        data = payload if isinstance(payload, str) else json.dumps(payload)
        frames.append(f"data: {data}\n\n")
    if done:
        frames.append("data: [DONE]\n\n")
    return "".join(frames).encode("utf-8")


def _stream_response(*payloads: Any, done: bool = True) -> httpx.Response:
    return httpx.Response(
        200, content=_sse_body(*payloads, done=done), headers=SSE_HEADERS
    )


def _complete_streaming(
    provider: OpenAICompatProvider,
    request: LLMRequest,
    **kwargs: Any,
) -> tuple[list[StreamDelta], LLMResponse]:
    deltas: list[StreamDelta] = []
    response = provider.complete_streaming(request, deltas.append, **kwargs)
    return deltas, response


class _ExplodingStream(httpx.SyncByteStream):
    """Yields a head of valid SSE bytes, then dies mid-stream."""

    def __init__(self, head: bytes) -> None:
        self._head = head

    def __iter__(self) -> Iterator[bytes]:
        yield self._head
        raise httpx.ReadError("connection dropped mid-stream")


# ---------------------------------------------------------------------------
# 1. Text-only stream: ordered text deltas + batch-identical final response
# ---------------------------------------------------------------------------


@respx.mock
def test_text_stream_emits_ordered_deltas_and_matches_batch() -> None:
    usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    stream = _stream_response(
        _chunk(delta={"role": "assistant", "content": ""}),
        _chunk(delta={"content": "Hel"}),
        _chunk(delta={"content": "lo"}),
        _chunk(finish_reason="stop"),
        _chunk(choices=[], usage=usage),
    )
    batch = httpx.Response(
        200,
        json={
            "id": "chatcmpl-batch",
            "object": "chat.completion",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        },
    )
    route = respx.post(CHAT_ENDPOINT).mock(side_effect=[stream, batch])

    provider = _make_provider()
    deltas, streamed = _complete_streaming(provider, _basic_request())
    batched = provider.complete(_basic_request())

    # Ordered text deltas; a lone text block takes index 0 (its final
    # position in the response content).
    assert deltas == [
        StreamDelta(kind="text", text="Hel", index=0),
        StreamDelta(kind="text", text="lo", index=0),
    ]
    # Streamed result is shape-identical to the equivalent batch call
    # (``raw`` is diagnostics-only and legitimately differs).
    assert streamed.stop_reason == batched.stop_reason == "end_turn"
    assert streamed.content == batched.content == [TextBlock(text="Hello")]
    assert streamed.usage == batched.usage == Usage(uncached=3, output=2)

    # The streaming request adds stream + stream_options on top of the
    # batch body, nothing else.
    stream_body = json.loads(route.calls[0].request.content)
    batch_body = json.loads(route.calls[1].request.content)
    assert stream_body.pop("stream") is True
    assert stream_body.pop("stream_options") == {"include_usage": True}
    assert stream_body == batch_body


@respx.mock
def test_request_headers_are_merged_into_the_post() -> None:
    """``request_headers`` are transport-only: merged over the client headers."""
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=_stream_response(
            _chunk(delta={"content": "ok"}), _chunk(finish_reason="stop")
        )
    )

    _complete_streaming(
        _make_provider(),
        _basic_request(),
        request_headers={"X-Session": "abc"},
    )

    request = route.calls[0].request
    assert request.headers["x-session"] == "abc"
    assert request.headers["authorization"] == "Bearer sk-test"


# ---------------------------------------------------------------------------
# 2. Tool-call stream: silent accumulation, batch-identical decode
# ---------------------------------------------------------------------------


@respx.mock
def test_tool_call_fragments_accumulate_silently_and_decode() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=_stream_response(
            _chunk(delta={"role": "assistant", "content": None}),
            _chunk(
                delta={
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_0",
                            "type": "function",
                            "function": {"name": "echo", "arguments": ""},
                        }
                    ]
                }
            ),
            _chunk(
                delta={
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": '{"te'}}
                    ]
                }
            ),
            _chunk(
                delta={
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": 'xt": "hi"}'}}
                    ]
                }
            ),
            # Second call by index, arguments arriving whole.
            _chunk(
                delta={
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "add", "arguments": '{"a": 1}'},
                        }
                    ]
                }
            ),
            _chunk(finish_reason="tool_calls"),
        )
    )

    deltas, response = _complete_streaming(_make_provider(), _basic_request())

    # Tool-argument fragments are NEVER surfaced as deltas.
    assert deltas == []
    assert response.stop_reason == "tool_use"
    assert response.content == [
        ToolUseBlock(call_id="call_0", tool_name="echo", arguments={"text": "hi"}),
        ToolUseBlock(call_id="call_1", tool_name="add", arguments={"a": 1}),
    ]


@respx.mock
def test_text_before_tool_calls_keeps_both_in_content() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=_stream_response(
            _chunk(delta={"content": "calling the tool first"}),
            _chunk(
                delta={
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": "echo", "arguments": "{}"},
                        }
                    ]
                }
            ),
            _chunk(finish_reason="tool_calls"),
        )
    )

    deltas, response = _complete_streaming(_make_provider(), _basic_request())

    assert deltas == [
        StreamDelta(kind="text", text="calling the tool first", index=0)
    ]
    assert response.content == [
        TextBlock(text="calling the tool first"),
        ToolUseBlock(call_id="call_a", tool_name="echo", arguments={}),
    ]


# ---------------------------------------------------------------------------
# 3. Reasoning fragments: thinking deltas + final ThinkingBlock
# ---------------------------------------------------------------------------


@respx.mock
def test_reasoning_content_fragments_emit_thinking_deltas() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=_stream_response(
            _chunk(delta={"reasoning_content": "let me "}),
            _chunk(delta={"reasoning_content": "think..."}),
            _chunk(delta={"content": "the answer"}),
            _chunk(finish_reason="stop"),
        )
    )

    deltas, response = _complete_streaming(_make_provider(), _basic_request())

    # Thinking streams before text, so the indexes match the final block
    # positions (ThinkingBlock 0, TextBlock 1) and stay distinct + ordered.
    assert deltas == [
        StreamDelta(kind="thinking", text="let me ", index=0),
        StreamDelta(kind="thinking", text="think...", index=0),
        StreamDelta(kind="text", text="the answer", index=1),
    ]
    assert response.stop_reason == "end_turn"
    assert response.content == [
        ThinkingBlock(text="let me think...", signature=None),
        TextBlock(text="the answer"),
    ]


@respx.mock
def test_reasoning_field_variant_is_recognised() -> None:
    """Gateways spelling the field ``reasoning`` (not ``reasoning_content``)
    stream thinking too — the same keys ``_extract_thinking`` sniffs."""
    respx.post(CHAT_ENDPOINT).mock(
        return_value=_stream_response(
            _chunk(delta={"reasoning": "hmm"}),
            _chunk(delta={"content": "ok"}),
            _chunk(finish_reason="stop"),
        )
    )

    deltas, response = _complete_streaming(_make_provider(), _basic_request())

    assert deltas == [
        StreamDelta(kind="thinking", text="hmm", index=0),
        StreamDelta(kind="text", text="ok", index=1),
    ]
    assert response.content == [
        ThinkingBlock(text="hmm", signature=None),
        TextBlock(text="ok"),
    ]


@respx.mock
def test_encrypted_reasoning_accumulates_silently_into_signature() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=_stream_response(
            _chunk(delta={"reasoning_content": "step"}),
            _chunk(delta={"encrypted_reasoning": "opaque-token=="}),
            _chunk(delta={"content": "done"}),
            _chunk(finish_reason="stop"),
        )
    )

    deltas, response = _complete_streaming(_make_provider(), _basic_request())

    # The signature is an opaque continuation token, never a delta.
    assert [d.text for d in deltas] == ["step", "done"]
    assert response.content == [
        ThinkingBlock(text="step", signature="opaque-token=="),
        TextBlock(text="done"),
    ]


# ---------------------------------------------------------------------------
# 4. Usage: terminal include_usage chunk mapped; absence degrades to zero
# ---------------------------------------------------------------------------


@respx.mock
def test_usage_from_terminal_chunk_is_mapped() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=_stream_response(
            _chunk(delta={"content": "hi"}),
            _chunk(finish_reason="stop"),
            _chunk(
                choices=[],
                usage={
                    "prompt_tokens": 12,
                    "completion_tokens": 40,
                    "total_tokens": 52,
                    "completion_tokens_details": {"reasoning_tokens": 30},
                },
            ),
        )
    )

    _, response = _complete_streaming(_make_provider(), _basic_request())

    assert response.usage == Usage(uncached=12, output=40, reasoning_tokens=30)
    assert response.usage.visible_output == 10


@respx.mock
def test_missing_usage_degrades_to_zero_usage() -> None:
    """Some OpenAI-compatible gateways never send the include_usage chunk:
    degrade to an empty Usage exactly like batch does for a missing usage
    object — no error."""
    respx.post(CHAT_ENDPOINT).mock(
        return_value=_stream_response(
            _chunk(delta={"content": "hi"}),
            _chunk(finish_reason="stop"),
        )
    )

    _, response = _complete_streaming(_make_provider(), _basic_request())

    assert response.stop_reason == "end_turn"
    assert response.usage == Usage()


# ---------------------------------------------------------------------------
# 5. Error taxonomy: mid-stream transport failures and HTTP errors on open
# ---------------------------------------------------------------------------


@respx.mock
def test_mid_stream_transport_error_maps_to_transient() -> None:
    head = _sse_body(_chunk(delta={"content": "par"}), done=False)
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(
            200, stream=_ExplodingStream(head), headers=SSE_HEADERS
        )
    )

    deltas: list[StreamDelta] = []
    with pytest.raises(TransientError):
        _make_provider().complete_streaming(_basic_request(), deltas.append)


@respx.mock
def test_connect_error_maps_to_transient() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    with pytest.raises(TransientError):
        _make_provider().complete_streaming(_basic_request(), lambda _d: None)


@respx.mock
def test_429_on_open_maps_to_transient_with_retry_after() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(
            429,
            json={"error": "rate_limited"},
            headers={"Retry-After": "3"},
        )
    )

    with pytest.raises(TransientError) as ex:
        _make_provider().complete_streaming(_basic_request(), lambda _d: None)
    assert ex.value.retry_after == 3.0


@respx.mock
def test_400_context_overflow_on_open_maps_to_overflow() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "message": "This model's maximum context length is ...",
                    "type": "invalid_request_error",
                    "code": "context_length_exceeded",
                }
            },
        )
    )

    with pytest.raises(ContextOverflowError):
        _make_provider().complete_streaming(_basic_request(), lambda _d: None)


@respx.mock
def test_401_on_open_maps_to_fatal() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(401, json={"error": "invalid_api_key"})
    )

    with pytest.raises(FatalError):
        _make_provider().complete_streaming(_basic_request(), lambda _d: None)


# ---------------------------------------------------------------------------
# 6. [DONE] handling / malformed chunks skipped / truncated stream
# ---------------------------------------------------------------------------


@respx.mock
def test_events_after_done_are_ignored() -> None:
    body = _sse_body(
        _chunk(delta={"content": "hi"}),
        _chunk(finish_reason="stop"),
    ) + _sse_body(_chunk(delta={"content": "GHOST"}), done=False)
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, content=body, headers=SSE_HEADERS)
    )

    deltas, response = _complete_streaming(_make_provider(), _basic_request())

    assert [d.text for d in deltas] == ["hi"]
    assert response.content == [TextBlock(text="hi")]


@respx.mock
def test_malformed_and_unknown_chunks_are_skipped() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=_stream_response(
            "{not-json",                      # malformed JSON → skipped
            json.dumps(["not", "a", "dict"]),  # non-dict root → skipped
            _chunk(choices=[]),               # no choices → no-op
            _chunk(delta={"content": "still "}),
            _chunk(delta={"unknown_field": "?"}),  # unknown delta key → no-op
            _chunk(delta={"content": "fine"}),
            _chunk(finish_reason="stop"),
        )
    )

    deltas, response = _complete_streaming(_make_provider(), _basic_request())

    assert [d.text for d in deltas] == ["still ", "fine"]
    assert response.stop_reason == "end_turn"
    assert response.content == [TextBlock(text="still fine")]


@respx.mock
def test_stream_ending_without_done_but_with_content_is_tolerated() -> None:
    """A clean-but-unterminated close (content + finish_reason, no [DONE])
    still parses — matching the shared SSE parser's tolerant stance."""
    respx.post(CHAT_ENDPOINT).mock(
        return_value=_stream_response(
            _chunk(delta={"content": "hi"}),
            _chunk(finish_reason="stop"),
            done=False,
        )
    )

    _, response = _complete_streaming(_make_provider(), _basic_request())

    assert response.stop_reason == "end_turn"
    assert response.content == [TextBlock(text="hi")]


@respx.mock
def test_stream_ending_without_done_and_without_content_is_transient() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, content=b"", headers=SSE_HEADERS)
    )

    with pytest.raises(TransientError, match="without \\[DONE\\]"):
        _make_provider().complete_streaming(_basic_request(), lambda _d: None)


@respx.mock
def test_inconsistent_streamed_finish_reason_raises_like_batch() -> None:
    """The rebuilt payload goes through the same ``_parse_response``
    consistency checks as batch: finish_reason=stop with tool_calls raises."""
    respx.post(CHAT_ENDPOINT).mock(
        return_value=_stream_response(
            _chunk(
                delta={
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "echo", "arguments": "{}"},
                        }
                    ]
                }
            ),
            _chunk(finish_reason="stop"),
        )
    )

    with pytest.raises(ValueError, match="inconsistent OpenAI response"):
        _make_provider().complete_streaming(_basic_request(), lambda _d: None)
