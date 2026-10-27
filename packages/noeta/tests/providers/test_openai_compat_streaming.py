"""Streaming behavior of :class:`noeta.providers.openai_compat.OpenAICompatProvider`.

Deltas fire for text / reasoning fragments only (tool-call fragments
accumulate silently, since partial JSON is unrenderable), and the returned
response is what the equivalent batch body parses to. Also the edges of a
stream: truncation by the output cap, cut streams, ``[DONE]`` handling,
index-less parallel calls, and asyncio cancellation. Bodies are nameless
data-only SSE events in the Chat Completions chunk shape.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from noeta.errors import Fatal, MalformedToolArguments, Transient
from noeta.providers.openai_compat import OpenAICompatProvider
from noeta.types import (
    Delta,
    Message,
    ModelRequest,
    ModelResponse,
    Text,
    Thinking,
    ToolUse,
    Usage,
)

from tests.providers.sse import RaisingStream, chat_sse, sse_bytes, sse_response

BASE_URL = "https://example.test/v1"
CHAT_ENDPOINT = f"{BASE_URL}/chat/completions"
SSE_HEADERS = {"content-type": "text/event-stream"}


def _make_provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")


def _request(text: str = "hi") -> ModelRequest:
    return ModelRequest(model="gpt-4o", messages=[Message(role="user", content=[Text(text=text)])])


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
        chunk["choices"] = [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}]
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def _body(*payloads: Any, done: bool = True) -> bytes:
    frames: list[tuple[str | None, Any]] = [(None, p) for p in payloads]
    if done:
        frames.append((None, "[DONE]"))
    return sse_bytes(frames)


def _serve(*payloads: Any, done: bool = True) -> respx.Route:
    return respx.post(CHAT_ENDPOINT).mock(return_value=sse_response(_body(*payloads, done=done)))


async def _stream(
    request: ModelRequest | None = None,
) -> tuple[list[Delta], ModelResponse]:
    deltas: list[Delta] = []
    response = await _make_provider().complete(request or _request(), deltas.append)
    return deltas, response


def _call_fragment(
    index: int | None, call_id: str | None, args: str, name: str | None = "Read"
) -> dict[str, Any]:
    call: dict[str, Any] = {"function": {"arguments": args}}
    if name is not None:
        call["function"]["name"] = name
    if index is not None:
        call["index"] = index
    if call_id is not None:
        call["id"] = call_id
        call["type"] = "function"
    return _chunk(delta={"tool_calls": [call]})


# ---------------------------------------------------------------------------
# Text: ordered deltas + batch-identical final response
# ---------------------------------------------------------------------------


@respx.mock
async def test_text_stream_emits_ordered_deltas_and_matches_batch_parse() -> None:
    usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    _serve(
        _chunk(delta={"role": "assistant", "content": ""}),
        _chunk(delta={"content": "Hel"}),
        _chunk(delta={"content": "lo"}),
        _chunk(finish_reason="stop"),
        _chunk(choices=[], usage=usage),
    )
    deltas, streamed = await _stream()
    # A lone text block takes index 0 (its final position in the content).
    assert deltas == [
        Delta(kind="text", text="Hel", index=0),
        Delta(kind="text", text="lo", index=0),
    ]
    assert streamed.stop_reason == "end_turn"
    assert streamed.message.content == [Text(text="Hello")]
    assert streamed.usage == Usage(uncached=3, output=2)

    # The same response delivered as one whole chunk parses identically.
    respx.post(CHAT_ENDPOINT).mock(
        return_value=sse_response(
            chat_sse(
                {
                    "model": "gpt-4o",
                    "choices": [
                        {"message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}
                    ],
                    "usage": usage,
                }
            )
        )
    )
    batched = await _make_provider().complete(_request())
    assert batched == streamed


@respx.mock
async def test_on_delta_is_optional() -> None:
    _serve(_chunk(delta={"content": "ok"}), _chunk(finish_reason="stop"))
    response = await _make_provider().complete(_request())
    assert response.message.content == [Text(text="ok")]


# ---------------------------------------------------------------------------
# Tool calls: silent accumulation
# ---------------------------------------------------------------------------


@respx.mock
async def test_tool_call_fragments_accumulate_silently_and_decode() -> None:
    _serve(
        _chunk(delta={"role": "assistant", "content": None}),
        _call_fragment(0, "call_0", "", name="echo"),
        _call_fragment(0, None, '{"te', name=None),
        _call_fragment(0, None, 'xt": "hi"}', name=None),
        # Second call by index, arguments arriving whole.
        _call_fragment(1, "call_1", '{"a": 1}', name="add"),
        _chunk(finish_reason="tool_calls"),
    )
    deltas, response = await _stream()
    # Tool-argument fragments are NEVER surfaced as deltas.
    assert deltas == []
    assert response.stop_reason == "tool_use"
    assert response.message.content == [
        ToolUse(id="call_0", name="echo", input={"text": "hi"}),
        ToolUse(id="call_1", name="add", input={"a": 1}),
    ]


@respx.mock
async def test_text_before_tool_calls_keeps_both_in_content() -> None:
    _serve(
        _chunk(delta={"content": "calling the tool first"}),
        _call_fragment(0, "call_a", "{}", name="echo"),
        _chunk(finish_reason="tool_calls"),
    )
    deltas, response = await _stream()
    assert deltas == [Delta(kind="text", text="calling the tool first", index=0)]
    assert response.message.content == [
        Text(text="calling the tool first"),
        ToolUse(id="call_a", name="echo", input={}),
    ]


@respx.mock
async def test_streamed_stop_with_tool_calls_is_tool_use() -> None:
    _serve(_call_fragment(0, "call_1", "{}", name="echo"), _chunk(finish_reason="stop"))
    _, response = await _stream()
    assert response.stop_reason == "tool_use"


@respx.mock
async def test_parallel_calls_without_index_split_on_a_new_id() -> None:
    """Some gateways omit ``index``: a fragment continues the latest call
    unless it carries a different ``id``."""
    _serve(
        _call_fragment(None, "c1", '{"file_path":'),
        _call_fragment(None, None, '"/a"}'),
        _call_fragment(None, "c2", '{"file_path":"/b"}'),
        _chunk(finish_reason="tool_calls"),
    )
    _, response = await _stream()
    assert response.stop_reason == "tool_use"
    assert response.message.content == [
        ToolUse(id="c1", name="Read", input={"file_path": "/a"}),
        ToolUse(id="c2", name="Read", input={"file_path": "/b"}),
    ]


@respx.mock
async def test_length_drops_the_call_cut_mid_json() -> None:
    _serve(
        _call_fragment(0, "c1", '{"file_path":"/a"}'),
        _call_fragment(1, "c2", '{"file_pa'),
        _chunk(finish_reason="length"),
    )
    _, response = await _stream()
    assert response.stop_reason == "max_tokens"
    assert response.message.content == [ToolUse(id="c1", name="Read", input={"file_path": "/a"})]


@respx.mock
async def test_undecodable_arguments_without_length_are_malformed() -> None:
    _serve(_call_fragment(0, "c1", '{"file_pa'), _chunk(finish_reason="tool_calls"))
    with pytest.raises(MalformedToolArguments):
        await _stream()


# ---------------------------------------------------------------------------
# Reasoning fragments
# ---------------------------------------------------------------------------


@respx.mock
async def test_reasoning_content_fragments_emit_thinking_deltas() -> None:
    _serve(
        _chunk(delta={"reasoning_content": "let me "}),
        _chunk(delta={"reasoning_content": "think..."}),
        _chunk(delta={"content": "the answer"}),
        _chunk(finish_reason="stop"),
    )
    deltas, response = await _stream()
    # Thinking streams before text, so the indexes match the final block
    # positions (Thinking 0, Text 1) and stay distinct + ordered.
    assert deltas == [
        Delta(kind="thinking", text="let me ", index=0),
        Delta(kind="thinking", text="think...", index=0),
        Delta(kind="text", text="the answer", index=1),
    ]
    assert response.stop_reason == "end_turn"
    assert response.message.content == [
        Thinking(text="let me think...", family="openai_compat"),
        Text(text="the answer"),
    ]


@respx.mock
async def test_reasoning_field_variant_is_recognised() -> None:
    _serve(
        _chunk(delta={"reasoning": "hmm"}),
        _chunk(delta={"content": "ok"}),
        _chunk(finish_reason="stop"),
    )
    deltas, response = await _stream()
    assert deltas == [
        Delta(kind="thinking", text="hmm", index=0),
        Delta(kind="text", text="ok", index=1),
    ]
    assert response.message.content == [
        Thinking(text="hmm", family="openai_compat"),
        Text(text="ok"),
    ]


@respx.mock
async def test_encrypted_reasoning_accumulates_silently_into_signature() -> None:
    _serve(
        _chunk(delta={"reasoning_content": "step"}),
        _chunk(delta={"encrypted_reasoning": "opaque-token=="}),
        _chunk(delta={"content": "done"}),
        _chunk(finish_reason="stop"),
    )
    deltas, response = await _stream()
    # The signature is an opaque continuation token, never a delta.
    assert [d.text for d in deltas] == ["step", "done"]
    assert response.message.content == [
        Thinking(text="step", signature="opaque-token==", family="openai_compat"),
        Text(text="done"),
    ]


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


@respx.mock
async def test_usage_from_terminal_chunk_is_mapped() -> None:
    _serve(
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
    _, response = await _stream()
    assert response.usage == Usage(uncached=12, output=40, reasoning=30)


@respx.mock
async def test_missing_usage_degrades_to_zero_usage() -> None:
    """Some gateways never send the include_usage chunk: no error."""
    _serve(_chunk(delta={"content": "hi"}), _chunk(finish_reason="stop"))
    _, response = await _stream()
    assert response.stop_reason == "end_turn"
    assert response.usage == Usage()


# ---------------------------------------------------------------------------
# Transport failures mid-stream / cancellation
# ---------------------------------------------------------------------------


@respx.mock
async def test_mid_stream_transport_error_maps_to_transient() -> None:
    head = _body(_chunk(delta={"content": "par"}), done=False)
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            stream=RaisingStream([head], httpx.ReadError("connection dropped mid-stream")),
            headers=SSE_HEADERS,
        )
    )
    deltas: list[Delta] = []
    with pytest.raises(Transient):
        await _make_provider().complete(_request(), deltas.append)
    assert [d.text for d in deltas] == ["par"]


class _HangingStream(httpx.AsyncByteStream):
    """Yields a head, then waits forever; records whether httpx closed it."""

    def __init__(self, head: bytes) -> None:
        self._head = head
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._head
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@respx.mock
async def test_cancellation_mid_stream_propagates_and_closes_the_stream() -> None:
    stream = _HangingStream(_body(_chunk(delta={"content": "Hel"}), done=False))
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, stream=stream, headers=SSE_HEADERS)
    )
    got_delta = asyncio.Event()
    deltas: list[Delta] = []

    def on_delta(delta: Delta) -> None:
        deltas.append(delta)
        got_delta.set()

    task = asyncio.create_task(_make_provider().complete(_request(), on_delta))
    await asyncio.wait_for(got_delta.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Cancellation is not re-bucketed as a transient failure, and the stream
    # context exited (connection released).
    assert [d.text for d in deltas] == ["Hel"]
    assert stream.closed


# ---------------------------------------------------------------------------
# [DONE] handling / malformed chunks / truncated stream
# ---------------------------------------------------------------------------


@respx.mock
async def test_events_after_done_are_ignored() -> None:
    body = _body(_chunk(delta={"content": "hi"}), _chunk(finish_reason="stop")) + _body(
        _chunk(delta={"content": "GHOST"}), done=False
    )
    respx.post(CHAT_ENDPOINT).mock(return_value=sse_response(body))
    deltas, response = await _stream()
    assert [d.text for d in deltas] == ["hi"]
    assert response.message.content == [Text(text="hi")]


@respx.mock
async def test_malformed_and_unknown_chunks_are_skipped() -> None:
    _serve(
        "{not-json",  # malformed JSON → skipped
        json.dumps(["not", "a", "dict"]),  # non-dict root → skipped
        _chunk(choices=[]),  # no choices → no-op
        _chunk(delta={"content": "still "}),
        _chunk(delta={"unknown_field": "?"}),  # unknown delta key → no-op
        _chunk(delta={"content": "fine"}),
        _chunk(finish_reason="stop"),
    )
    deltas, response = await _stream()
    assert [d.text for d in deltas] == ["still ", "fine"]
    assert response.stop_reason == "end_turn"
    assert response.message.content == [Text(text="still fine")]


@respx.mock
async def test_stream_with_finish_reason_but_no_done_still_completes() -> None:
    """A clean-but-unterminated close (finish_reason, no [DONE]) still parses."""
    _serve(_chunk(delta={"content": "whole answer"}), _chunk(finish_reason="stop"), done=False)
    _, response = await _stream()
    assert response.stop_reason == "end_turn"
    assert response.message.content == [Text(text="whole answer")]


@respx.mock
async def test_stream_cut_after_partial_text_is_transient() -> None:
    _serve(_chunk(delta={"content": "half an ans"}), done=False)
    with pytest.raises(Transient, match="without \\[DONE\\] or a finish_reason"):
        await _stream()


@respx.mock
async def test_empty_body_is_transient() -> None:
    respx.post(CHAT_ENDPOINT).mock(return_value=httpx.Response(200, content=b"", headers=SSE_HEADERS))
    with pytest.raises(Transient, match="without \\[DONE\\]"):
        await _stream()


@respx.mock
async def test_non_sse_200_body_is_transient() -> None:
    """A 200 whose body is not an event stream carries no finish_reason — the
    same cut-stream failure as an empty body."""
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, content=b"not-json", headers={"content-type": "text/plain"})
    )
    with pytest.raises(Transient):
        await _stream()


@respx.mock
async def test_done_without_finish_reason_is_fatal() -> None:
    """``[DONE]`` closes the stream cleanly, but no finish_reason ever came:
    there is no stop signal to report."""
    _serve(_chunk(delta={"content": "hi"}))
    with pytest.raises(Fatal, match="finish_reason"):
        await _stream()
