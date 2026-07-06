"""Streaming test matrix for
:meth:`noeta.providers.openai_responses.OpenAIResponsesProvider.complete_streaming`.

Token streaming (Slice 5a): the streamed path POSTs the same body as the
batch path plus ``stream:true``, emits ephemeral ``StreamDelta``s for text /
reasoning-summary fragments, swallows function-call-arguments fragments, and
feeds the terminal ``response.completed`` object through the same batch
parser — so the streamed ``LLMResponse`` is shape-identical to ``complete()``.
All HTTP traffic goes through a ``respx`` mock; the suite makes zero real
network calls.
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
    Message,
    StreamDelta,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    Usage,
)
from noeta.providers.openai_responses import OpenAIResponsesProvider


# Same endpoint convention as the batch test matrix: base_url IS the complete
# responses endpoint (the provider POSTs it verbatim, adding only the
# ?api-version query).
BASE_URL = "https://gateway.test/api/modelhub/online/responses"
ENDPOINT = BASE_URL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(**overrides: Any) -> OpenAIResponsesProvider:
    kwargs: dict[str, Any] = {
        "base_url": BASE_URL,
        "api_key": "sk-test",
    }
    kwargs.update(overrides)
    return OpenAIResponsesProvider(**kwargs)


def _user_message(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)])


def _basic_request(text: str = "hi") -> LLMRequest:
    return LLMRequest(model="gpt-5.4", messages=[_user_message(text)])


def _final_payload(
    *,
    output: list[dict[str, Any]],
    status: str = "completed",
    incomplete_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The complete response object a terminal stream event carries — the
    exact shape the batch path receives as its whole HTTP body."""
    payload: dict[str, Any] = {
        "id": "resp-xyz",
        "object": "response",
        "model": "gpt-5.4",
        "status": status,
        "output": output,
    }
    if incomplete_reason is not None:
        payload["incomplete_details"] = {"reason": incomplete_reason}
    if usage is not None:
        payload["usage"] = usage
    return payload


def _message_output(*texts: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": t} for t in texts],
    }


def _sse(events: list[tuple[str | None, Any]]) -> bytes:
    """Encode ``(event_name, payload)`` pairs as one SSE byte body.

    A ``None`` name emits a nameless, data-only frame (exercises the JSON
    ``type``-field fallback); a ``str`` payload is sent as raw data (exercises
    the non-JSON-frame skip)."""
    frames: list[str] = []
    for name, payload in events:
        lines: list[str] = []
        if name is not None:
            lines.append(f"event: {name}")
        data = payload if isinstance(payload, str) else json.dumps(payload)
        lines.append(f"data: {data}")
        frames.append("\n".join(lines) + "\n\n")
    return "".join(frames).encode("utf-8")


def _stream_response(events: list[tuple[str | None, Any]]) -> httpx.Response:
    return httpx.Response(
        200,
        content=_sse(events),
        headers={"content-type": "text/event-stream"},
    )


def _text_delta(fragment: str, output_index: int = 0) -> tuple[str, dict[str, Any]]:
    return (
        "response.output_text.delta",
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": output_index,
            "content_index": 0,
            "delta": fragment,
        },
    )


def _completed(final: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return (
        "response.completed",
        {"type": "response.completed", "response": final},
    )


class _DeltaRecorder:
    def __init__(self) -> None:
        self.deltas: list[StreamDelta] = []

    def __call__(self, delta: StreamDelta) -> None:
        self.deltas.append(delta)

    @property
    def as_tuples(self) -> list[tuple[str, str, int]]:
        return [(d.kind, d.text, d.index) for d in self.deltas]


class _ExplodingStream(httpx.SyncByteStream):
    """Yields a prefix of a valid SSE stream, then dies mid-flight."""

    def __init__(self, prefix: bytes) -> None:
        self._prefix = prefix

    def __iter__(self) -> Iterator[bytes]:
        yield self._prefix
        raise httpx.ReadError("connection reset mid-stream")


# ---------------------------------------------------------------------------
# 1. Text-only stream: deltas in order + final response identical to batch
# ---------------------------------------------------------------------------


@respx.mock
def test_text_stream_deltas_in_order_and_final_equals_batch() -> None:
    """The KEY reuse pin: the response.completed object goes through the same
    _parse_response as the batch path, so feeding the equivalent payload to
    complete() yields an equal LLMResponse — and the streamed request body is
    the batch body plus stream:true, nothing else."""
    final = _final_payload(
        output=[_message_output("hello world")],
        usage={"input_tokens": 12, "output_tokens": 5},
    )
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            _stream_response(
                [
                    (
                        "response.created",
                        {
                            "type": "response.created",
                            "response": {"id": "resp-xyz", "status": "in_progress"},
                        },
                    ),
                    (
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {"type": "message"},
                        },
                    ),
                    _text_delta("hello ", output_index=0),
                    _text_delta("world", output_index=0),
                    (
                        "response.output_text.done",
                        {
                            "type": "response.output_text.done",
                            "output_index": 0,
                            "text": "hello world",
                        },
                    ),
                    _completed(final),
                ]
            ),
            httpx.Response(200, json=final),
        ]
    )

    provider = _make_provider()
    recorder = _DeltaRecorder()
    streamed = provider.complete_streaming(_basic_request(), recorder)
    batch = provider.complete(_basic_request())

    assert recorder.as_tuples == [
        ("text", "hello ", 0),
        ("text", "world", 0),
    ]
    # Shape-identity with the batch parse of the same response object —
    # stop_reason, content, usage, and raw all compare equal.
    assert streamed == batch
    assert streamed.stop_reason == "end_turn"
    assert streamed.content == [TextBlock(text="hello world")]

    stream_body = json.loads(route.calls[0].request.content)
    batch_body = json.loads(route.calls[1].request.content)
    assert stream_body.pop("stream") is True
    assert "stream" not in batch_body
    assert stream_body == batch_body


@respx.mock
def test_streaming_uses_same_endpoint_params_and_merged_headers() -> None:
    """The transport column is identical to batch: verbatim endpoint POST,
    api-version query, constructor extra_headers merged with per-call
    request_headers (per-call wins)."""
    final = _final_payload(output=[_message_output("ok")])
    route = respx.post(ENDPOINT).mock(
        return_value=_stream_response([_text_delta("ok"), _completed(final)])
    )
    provider = _make_provider(
        api_version="2026-03-01-preview",
        extra_headers={"X-Static": "static", "X-TT-logid": "static-log"},
    )
    provider.complete_streaming(
        _basic_request(),
        _DeltaRecorder(),
        request_headers={"X-TT-logid": "task-abc"},
    )

    req = route.calls[0].request
    assert req.url.params["api-version"] == "2026-03-01-preview"
    assert req.url.path == "/api/modelhub/online/responses"
    assert req.headers["api-key"] == "sk-test"
    assert req.headers["x-static"] == "static"
    assert req.headers["x-tt-logid"] == "task-abc"


# ---------------------------------------------------------------------------
# 2. function_call stream: argument deltas swallowed, final ToolUseBlock
# ---------------------------------------------------------------------------


@respx.mock
def test_function_call_arguments_deltas_never_emitted_final_tool_use() -> None:
    final = _final_payload(
        output=[
            {
                "type": "function_call",
                "id": "fc_internal_1",
                "call_id": "call_abc",
                "name": "get_weather",
                "arguments": json.dumps({"city": "Beijing"}),
            }
        ],
    )
    respx.post(ENDPOINT).mock(
        return_value=_stream_response(
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
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": "fc_internal_1",
                        "output_index": 0,
                        "delta": '{"city"',
                    },
                ),
                (
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": "fc_internal_1",
                        "output_index": 0,
                        "delta": ': "Beijing"}',
                    },
                ),
                (
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": 0,
                        "arguments": json.dumps({"city": "Beijing"}),
                    },
                ),
                _completed(final),
            ]
        )
    )

    recorder = _DeltaRecorder()
    response = _make_provider().complete_streaming(_basic_request(), recorder)

    # Tool-call argument fragments are never surfaced as StreamDeltas.
    assert recorder.deltas == []
    assert response.stop_reason == "tool_use"
    assert response.content == [
        ToolUseBlock(
            call_id="call_abc",
            tool_name="get_weather",
            arguments={"city": "Beijing"},
        )
    ]


# ---------------------------------------------------------------------------
# 3. Reasoning stream: summary deltas → kind="thinking", final ThinkingBlock
# ---------------------------------------------------------------------------


@respx.mock
def test_reasoning_summary_deltas_are_thinking_and_final_block_keeps_signature() -> None:
    enc = "gAAAA" + "x" * 120  # opaque continuation ciphertext, verbatim
    final = _final_payload(
        output=[
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
        ],
    )
    respx.post(ENDPOINT).mock(
        return_value=_stream_response(
            [
                (
                    "response.reasoning_summary_text.delta",
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": "rs_1",
                        "output_index": 0,
                        "summary_index": 0,
                        "delta": "step one",
                    },
                ),
                (
                    "response.reasoning_summary_text.delta",
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": "rs_1",
                        "output_index": 0,
                        "summary_index": 1,
                        "delta": "step two",
                    },
                ),
                _text_delta("final ", output_index=1),
                _text_delta("answer", output_index=1),
                _completed(final),
            ]
        )
    )

    recorder = _DeltaRecorder()
    response = _make_provider().complete_streaming(_basic_request(), recorder)

    # thinking deltas carry the reasoning item's output_index; the text deltas
    # carry the message item's — interleaved blocks stay apart by index.
    assert recorder.as_tuples == [
        ("thinking", "step one", 0),
        ("thinking", "step two", 0),
        ("text", "final ", 1),
        ("text", "answer", 1),
    ]
    assert response.content == [
        ThinkingBlock(text="step one\nstep two", signature=enc),
        TextBlock(text="final answer"),
    ]


# ---------------------------------------------------------------------------
# 4. Usage mapping from response.completed
# ---------------------------------------------------------------------------


@respx.mock
def test_usage_from_terminal_event_maps_like_batch() -> None:
    final = _final_payload(
        output=[_message_output("ok")],
        usage={
            "input_tokens": 100,
            "output_tokens": 40,
            "input_tokens_details": {"cached_tokens": 30},
            "output_tokens_details": {"reasoning_tokens": 15},
        },
    )
    respx.post(ENDPOINT).mock(
        return_value=_stream_response([_text_delta("ok"), _completed(final)])
    )
    response = _make_provider().complete_streaming(
        _basic_request(), _DeltaRecorder()
    )
    # uncached = input_tokens - cached_tokens = 100 - 30 = 70
    assert response.usage == Usage(
        uncached=70,
        cache_read=30,
        cache_write=0,
        output=40,
        reasoning_tokens=15,
    )
    assert response.usage.input == 100
    assert response.usage.visible_output == 25


# ---------------------------------------------------------------------------
# 5. Errors: mid-stream transport, HTTP status on open, in-stream error events
# ---------------------------------------------------------------------------


@respx.mock
def test_mid_stream_transport_error_maps_to_transient() -> None:
    prefix = _sse([_text_delta("par")])
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            stream=_ExplodingStream(prefix),
            headers={"content-type": "text/event-stream"},
        )
    )
    recorder = _DeltaRecorder()
    with pytest.raises(TransientError):
        _make_provider().complete_streaming(_basic_request(), recorder)
    # Deltas already emitted before the disconnect are simply lost previews;
    # the runtime retry loop reissues the whole call.
    assert recorder.as_tuples == [("text", "par", 0)]


@respx.mock
def test_429_on_stream_open_maps_to_transient_with_retry_after() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            429, json={"error": "rate_limited"}, headers={"Retry-After": "7"}
        )
    )
    with pytest.raises(TransientError) as ex:
        _make_provider().complete_streaming(_basic_request(), _DeltaRecorder())
    assert ex.value.retry_after == 7.0


@respx.mock
def test_400_context_length_exceeded_on_open_maps_to_overflow() -> None:
    """HTTP statuses on stream open ride the exact batch taxonomy — the body
    is read before translation so _is_context_overflow sees the JSON."""
    respx.post(ENDPOINT).mock(
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
        _make_provider().complete_streaming(_basic_request(), _DeltaRecorder())


@respx.mock
def test_top_level_error_event_translates_by_code() -> None:
    respx.post(ENDPOINT).mock(
        return_value=_stream_response(
            [
                _text_delta("par"),
                (
                    "error",
                    {"type": "error", "code": "server_error", "message": "boom"},
                ),
            ]
        )
    )
    with pytest.raises(TransientError):
        _make_provider().complete_streaming(_basic_request(), _DeltaRecorder())


@respx.mock
def test_response_failed_with_error_payload_maps_to_fatal() -> None:
    failed = _final_payload(output=[], status="failed")
    failed["error"] = {"code": "invalid_prompt", "message": "rejected"}
    respx.post(ENDPOINT).mock(
        return_value=_stream_response(
            [("response.failed", {"type": "response.failed", "response": failed})]
        )
    )
    with pytest.raises(FatalError):
        _make_provider().complete_streaming(_basic_request(), _DeltaRecorder())


@respx.mock
def test_response_incomplete_max_output_tokens_maps_to_max_tokens() -> None:
    """The terminal response.incomplete object still goes through
    _parse_response, so the batch stop_reason inference applies unchanged."""
    final = _final_payload(
        output=[_message_output("partial")],
        status="incomplete",
        incomplete_reason="max_output_tokens",
    )
    respx.post(ENDPOINT).mock(
        return_value=_stream_response(
            [
                _text_delta("partial"),
                (
                    "response.incomplete",
                    {"type": "response.incomplete", "response": final},
                ),
            ]
        )
    )
    response = _make_provider().complete_streaming(
        _basic_request(), _DeltaRecorder()
    )
    assert response.stop_reason == "max_tokens"
    assert response.content == [TextBlock(text="partial")]


@respx.mock
def test_stream_without_terminal_event_maps_to_transient() -> None:
    """A cleanly closed stream that never delivered a terminal response event
    is a truncated stream — retryable, like a mid-stream disconnect."""
    respx.post(ENDPOINT).mock(
        return_value=_stream_response([_text_delta("par")])
    )
    with pytest.raises(TransientError):
        _make_provider().complete_streaming(_basic_request(), _DeltaRecorder())


# ---------------------------------------------------------------------------
# 6. invalid_encrypted_content 400 → one streamed retry with stripped reasoning
# ---------------------------------------------------------------------------


def _request_with_prior_reasoning() -> LLMRequest:
    """A resumed turn whose history carries a prior-turn ThinkingBlock — its
    ``signature`` echoes as a ``reasoning`` input item with
    ``encrypted_content``."""
    return LLMRequest(
        model="gpt-5.4",
        messages=[
            _user_message("hi"),
            Message(
                role="assistant",
                content=[
                    ThinkingBlock(text="prior cot", signature="gAAAA-stale-cipher"),
                    TextBlock(text="answer"),
                ],
            ),
            _user_message("continue"),
        ],
    )


@respx.mock
def test_invalid_encrypted_content_400_retries_once_streamed() -> None:
    """The stale-ciphertext 400 surfaces before any stream starts; the
    streaming path honors the same one-shot strip-and-retry as batch, and the
    retried request is also streamed."""
    final = _final_payload(output=[_message_output("recovered")])
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "code: invalid_encrypted_content; message: The "
                            "encrypted content gAAA...flvh could not be verified."
                        ),
                        "type": "invalid_request_error",
                        "code": "-4003",
                    }
                },
            ),
            _stream_response([_text_delta("recovered"), _completed(final)]),
        ]
    )

    recorder = _DeltaRecorder()
    response = _make_provider().complete_streaming(
        _request_with_prior_reasoning(), recorder
    )

    assert response.content == [TextBlock(text="recovered")]
    assert recorder.as_tuples == [("text", "recovered", 0)]
    assert len(route.calls) == 2
    first = json.loads(route.calls[0].request.content)
    second = json.loads(route.calls[1].request.content)
    # Both attempts stream; the retry strips only the echoed reasoning items.
    assert first["stream"] is True
    assert second["stream"] is True
    assert any(it.get("type") == "reasoning" for it in first["input"])
    assert not any(it.get("type") == "reasoning" for it in second["input"])


@respx.mock
def test_invalid_encrypted_content_without_reasoning_stays_fatal_streamed() -> None:
    """No echoed reasoning to strip ⇒ no retry — same guard as batch."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "message": "invalid_encrypted_content",
                    "type": "invalid_request_error",
                    "code": "-4003",
                }
            },
        )
    )
    with pytest.raises(FatalError):
        _make_provider().complete_streaming(_basic_request(), _DeltaRecorder())
    assert len(route.calls) == 1


# ---------------------------------------------------------------------------
# 7. Unknown events skipped
# ---------------------------------------------------------------------------


@respx.mock
def test_unknown_events_nameless_frames_and_non_json_data_skipped() -> None:
    final = _final_payload(output=[_message_output("ok")])
    respx.post(ENDPOINT).mock(
        return_value=_stream_response(
            [
                # Unknown event type: skipped silently, even with a delta field.
                (
                    "response.banana.delta",
                    {"type": "response.banana.delta", "delta": "nope"},
                ),
                # Non-JSON data frame (e.g. a gateway's [DONE] sentinel): skipped.
                (None, "[DONE]"),
                # Nameless frame: the JSON type field is the fallback dispatch.
                (
                    None,
                    {
                        "type": "response.output_text.delta",
                        "output_index": 0,
                        "delta": "o",
                    },
                ),
                _text_delta("k"),
                # Unknown terminal-looking event: also skipped.
                ("response.unknown_terminal", {"type": "response.unknown_terminal"}),
                _completed(final),
            ]
        )
    )

    recorder = _DeltaRecorder()
    response = _make_provider().complete_streaming(_basic_request(), recorder)

    assert recorder.as_tuples == [("text", "o", 0), ("text", "k", 0)]
    assert response.stop_reason == "end_turn"
    assert response.content == [TextBlock(text="ok")]
