"""OpenAI Responses adapter — inbound side: the stop-reason priority table,
output items → blocks, usage accounting, truncated tool calls, and the error
taxonomy (HTTP statuses, stale reasoning ciphertext, transport failures,
malformed payloads)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from noeta.errors import (
    ContextOverflow,
    Fatal,
    MalformedToolArguments,
    RateLimited,
    Transient,
)
from noeta.types import Message, ModelRequest, Text, Thinking, ToolUse, Usage

from tests.providers._responses_helpers import (
    ENDPOINT,
    basic_request,
    make_provider,
    mock_ok,
    ok_stream,
    responses_payload,
    sent_body,
    user_message,
)
from tests.providers.sse import responses_sse, sse_response


def _function_call(call_id: str, name: str, arguments: str, **extra: Any) -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments, **extra}


async def _complete(payload: dict[str, Any], request: ModelRequest | None = None) -> Any:
    mock_ok(payload)
    return await make_provider().complete(request or basic_request())


# ---------------------------------------------------------------------------
# Text and model
# ---------------------------------------------------------------------------


@respx.mock
async def test_text_round_trip_maps_to_end_turn_text() -> None:
    response = await _complete(responses_payload(texts=["hello"]))
    assert response.stop_reason == "end_turn"
    assert response.message == Message(role="assistant", content=[Text(text="hello")])
    assert response.model == "gpt-5.4"


@respx.mock
async def test_model_falls_back_to_request_model_when_payload_has_none() -> None:
    payload = responses_payload(texts=["x"])
    del payload["model"]
    response = await _complete(payload, basic_request(model="m-req"))
    assert response.model == "m-req"


@respx.mock
async def test_multiple_output_text_segments_each_become_text() -> None:
    response = await _complete(responses_payload(texts=["part one", "part two", "part three"]))
    assert response.message.content == [
        Text(text="part one"),
        Text(text="part two"),
        Text(text="part three"),
    ]


@respx.mock
async def test_empty_output_text_segment_is_dropped() -> None:
    response = await _complete(responses_payload(texts=["", "kept"]))
    assert response.message.content == [Text(text="kept")]


# ---------------------------------------------------------------------------
# Stop-reason priority table
# ---------------------------------------------------------------------------


@respx.mock
async def test_function_call_item_maps_to_tool_use() -> None:
    response = await _complete(
        responses_payload(texts=None, extra_output=[_function_call("call_1", "echo", "{}")])
    )
    assert response.stop_reason == "tool_use"


@respx.mock
async def test_incomplete_max_output_tokens_maps_to_max_tokens() -> None:
    response = await _complete(
        responses_payload(
            texts=["partial"], status="incomplete", incomplete_reason="max_output_tokens"
        )
    )
    assert response.stop_reason == "max_tokens"
    assert response.message.content == [Text(text="partial")]


@respx.mock
async def test_incomplete_max_output_tokens_beats_partial_function_call() -> None:
    response = await _complete(
        responses_payload(
            texts=None,
            status="incomplete",
            incomplete_reason="max_output_tokens",
            extra_output=[_function_call("call_x", "echo", "{}")],
        )
    )
    assert response.stop_reason == "max_tokens"


@respx.mock
async def test_incomplete_content_filter_maps_to_refusal_and_drops_calls() -> None:
    response = await _complete(
        responses_payload(
            texts=["I can't help with that"],
            status="incomplete",
            incomplete_reason="content_filter",
            extra_output=[_function_call("c1", "echo", "{}")],
        )
    )
    assert response.stop_reason == "refusal"
    assert response.message.content == [Text(text="I can't help with that")]


@pytest.mark.parametrize("status", ["failed", "content_filter", "cancelled"])
@respx.mock
async def test_non_completed_status_without_calls_is_fatal(status: str) -> None:
    mock_ok(responses_payload(texts=None, status=status))
    with pytest.raises(Fatal):
        await make_provider().complete(basic_request())


@respx.mock
async def test_incomplete_unknown_reason_is_fatal() -> None:
    mock_ok(responses_payload(texts=["x"], status="incomplete", incomplete_reason="other"))
    with pytest.raises(Fatal):
        await make_provider().complete(basic_request())


# ---------------------------------------------------------------------------
# Function calls
# ---------------------------------------------------------------------------


@respx.mock
async def test_inbound_function_call_paired_by_call_id_not_internal_id() -> None:
    response = await _complete(
        responses_payload(
            texts=None,
            extra_output=[
                _function_call(
                    "call_abc",
                    "get_weather",
                    json.dumps({"city": "Shanghai"}),
                    id="fc_internal_must_not_be_used",
                )
            ],
        )
    )
    assert response.message.content == [
        ToolUse(id="call_abc", name="get_weather", input={"city": "Shanghai"})
    ]


@respx.mock
async def test_inbound_text_and_function_call_both_in_content() -> None:
    response = await _complete(
        responses_payload(
            texts=["thinking out loud"],
            extra_output=[_function_call("c1", "tool_a", json.dumps({"k": "v"}))],
        )
    )
    assert response.stop_reason == "tool_use"
    assert response.message.content == [
        Text(text="thinking out loud"),
        ToolUse(id="c1", name="tool_a", input={"k": "v"}),
    ]


@respx.mock
async def test_inbound_multiple_function_calls_each_paired() -> None:
    response = await _complete(
        responses_payload(
            texts=None,
            extra_output=[
                _function_call("c1", "a", "{}"),
                _function_call("c2", "b", json.dumps({"n": 2})),
            ],
        )
    )
    assert response.message.tool_uses == [
        ToolUse(id="c1", name="a", input={}),
        ToolUse(id="c2", name="b", input={"n": 2}),
    ]


@pytest.mark.parametrize("raw", ["null", "", "   ", None])
@respx.mock
async def test_null_or_empty_arguments_decode_as_empty_object(raw: Any) -> None:
    item = _function_call("c1", "a", "{}")
    item["arguments"] = raw
    response = await _complete(responses_payload(texts=None, extra_output=[item]))
    assert response.message.tool_uses == [ToolUse(id="c1", name="a", input={})]


@pytest.mark.parametrize("raw", ["{not valid json", "[1, 2]", '"str"', "3"])
@respx.mock
async def test_malformed_arguments_on_completed_turn_raise(raw: str) -> None:
    mock_ok(responses_payload(texts=None, extra_output=[_function_call("c1", "broken", raw)]))
    with pytest.raises(MalformedToolArguments, match="function_call arguments"):
        await make_provider().complete(basic_request())


@respx.mock
async def test_output_cap_drops_the_call_cut_mid_json() -> None:
    response = await _complete(
        responses_payload(
            texts=None,
            status="incomplete",
            incomplete_reason="max_output_tokens",
            extra_output=[
                _function_call("c1", "Read", '{"file_path":"/a"}'),
                _function_call("c2", "Read", '{"file_pa'),
            ],
        )
    )
    assert response.stop_reason == "max_tokens"
    assert response.message.content == [ToolUse(id="c1", name="Read", input={"file_path": "/a"})]


# ---------------------------------------------------------------------------
# Reasoning items
# ---------------------------------------------------------------------------


def _reasoning_item(summary_texts: list[str], encrypted_content: str) -> dict[str, Any]:
    return {
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": t} for t in summary_texts],
        "encrypted_content": encrypted_content,
    }


@respx.mock
async def test_inbound_reasoning_maps_to_thinking_with_family() -> None:
    enc = "gAAAA" + "x" * 120
    payload = responses_payload(
        texts=["final answer"],
        extra_output=[_reasoning_item(["step one", "step two"], enc)],
    )
    # The gateway emits the reasoning item ahead of the answer.
    payload["output"] = [payload["output"][1], payload["output"][0]]
    response = await _complete(payload)
    assert response.message.content == [
        Thinking(text="step one\nstep two", signature=enc, family="openai_responses"),
        Text(text="final answer"),
    ]


@respx.mock
async def test_inbound_reasoning_encrypted_content_round_trips_verbatim() -> None:
    enc = "ENC::" + "".join(chr(33 + (i % 90)) for i in range(2048))
    response = await _complete(
        responses_payload(texts=None, extra_output=[_reasoning_item(["thinking"], enc)])
    )
    block = response.message.content[0]
    assert isinstance(block, Thinking)
    assert block.signature == enc


@respx.mock
async def test_inbound_reasoning_empty_summary_yields_empty_text() -> None:
    response = await _complete(
        responses_payload(texts=None, extra_output=[_reasoning_item([], "enc-no-summary")])
    )
    assert response.message.content == [
        Thinking(text="", signature="enc-no-summary", family="openai_responses")
    ]


@respx.mock
async def test_unknown_output_items_are_skipped() -> None:
    response = await _complete(
        responses_payload(texts=["ok"], extra_output=[{"type": "web_search_call"}, "junk"])  # type: ignore[list-item]
    )
    assert response.message.content == [Text(text="ok")]


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


@respx.mock
async def test_usage_maps_cached_and_reasoning_tokens() -> None:
    response = await _complete(
        responses_payload(
            texts=["ok"],
            usage={
                "input_tokens": 100,
                "output_tokens": 40,
                "input_tokens_details": {"cached_tokens": 30},
                "output_tokens_details": {"reasoning_tokens": 15},
            },
        )
    )
    # uncached = input_tokens - cached_tokens = 100 - 30
    assert response.usage == Usage(uncached=70, cache_read=30, cache_write=0, output=40, reasoning=15)
    assert response.usage.input == 100


@respx.mock
async def test_usage_without_details_defaults_to_zero_cache() -> None:
    response = await _complete(
        responses_payload(texts=["ok"], usage={"input_tokens": 12, "output_tokens": 5})
    )
    assert response.usage == Usage(uncached=12, output=5)


@respx.mock
async def test_missing_usage_yields_empty_usage() -> None:
    response = await _complete(responses_payload(texts=["ok"]))
    assert response.usage == Usage()


# ---------------------------------------------------------------------------
# HTTP errors
# ---------------------------------------------------------------------------


def _mock_status(status: int, **kw: Any) -> respx.Route:
    return respx.post(ENDPOINT).mock(return_value=httpx.Response(status, **kw))


@respx.mock
async def test_429_with_retry_after_maps_to_rate_limited() -> None:
    _mock_status(429, json={"error": "rate_limited"}, headers={"Retry-After": "7"})
    with pytest.raises(RateLimited) as ex:
        await make_provider().complete(basic_request())
    assert ex.value.retry_after == 7.0
    assert ex.value.status == 429


@respx.mock
async def test_429_without_retry_after_maps_to_rate_limited_none() -> None:
    _mock_status(429, json={"error": "rate_limited"})
    with pytest.raises(RateLimited) as ex:
        await make_provider().complete(basic_request())
    assert ex.value.retry_after is None


@pytest.mark.parametrize("status", [500, 502, 503])
@respx.mock
async def test_5xx_maps_to_transient(status: int) -> None:
    _mock_status(status, json={"error": "boom"})
    with pytest.raises(Transient):
        await make_provider().complete(basic_request())


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "This model's maximum context length is ...", "code": "context_length_exceeded"}},
        {"error": {"message": "x", "type": "context_length_exceeded"}},
        {"error": {"message": "prompt is too long: 250000 tokens > 200000 maximum"}},
        {"error": {"message": "input length and `max_tokens` exceed context limit"}},
    ],
)
@respx.mock
async def test_400_context_overflow_markers_map_to_overflow(body: dict[str, Any]) -> None:
    _mock_status(400, json=body)
    with pytest.raises(ContextOverflow):
        await make_provider().complete(basic_request())


@respx.mock
async def test_413_maps_to_overflow() -> None:
    _mock_status(413, text="request entity too large")
    with pytest.raises(ContextOverflow):
        await make_provider().complete(basic_request())


@respx.mock
async def test_400_plain_invalid_request_maps_to_fatal() -> None:
    _mock_status(400, json={"error": {"message": "missing field", "type": "invalid_request_error"}})
    with pytest.raises(Fatal):
        await make_provider().complete(basic_request())


@respx.mock
async def test_401_maps_to_fatal() -> None:
    _mock_status(401, json={"error": "invalid_api_key"})
    with pytest.raises(Fatal):
        await make_provider().complete(basic_request())


@respx.mock
async def test_fatal_http_error_carries_the_json_error_message() -> None:
    _mock_status(404, json={"error": {"type": "not_found_error", "message": "model: nope-1 not found"}})
    with pytest.raises(Fatal) as info:
        await make_provider().complete(basic_request())
    assert str(info.value).endswith(": model: nope-1 not found")
    assert "404" in str(info.value)


@respx.mock
async def test_transient_http_error_carries_a_capped_raw_body() -> None:
    _mock_status(503, text="upstream gateway melted " + "x" * 2000)
    with pytest.raises(Transient) as info:
        await make_provider().complete(basic_request())
    text = str(info.value)
    assert "upstream gateway melted" in text
    assert "x" * 450 in text
    assert "x" * 500 not in text


@respx.mock
async def test_http_error_with_empty_body_keeps_the_status_text() -> None:
    _mock_status(401, content=b"")
    with pytest.raises(Fatal) as info:
        await make_provider().complete(basic_request())
    assert "401" in str(info.value)
    assert not str(info.value).endswith(": ")


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("timed out")])
@respx.mock
async def test_transport_failures_map_to_transient(exc: Exception) -> None:
    respx.post(ENDPOINT).mock(side_effect=exc)
    with pytest.raises(Transient):
        await make_provider().complete(basic_request())


# ---------------------------------------------------------------------------
# Stale reasoning ciphertext: one-shot strip-and-retry
# ---------------------------------------------------------------------------


def _request_with_prior_reasoning() -> ModelRequest:
    return basic_request(
        messages=[
            user_message("hi"),
            Message(
                role="assistant",
                content=[
                    Thinking(text="prior cot", signature="gAAAA-stale-cipher"),
                    Text(text="answer"),
                ],
            ),
            user_message("continue"),
        ]
    )


def _stale() -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "error": {
                "message": (
                    "code: invalid_encrypted_content; message: The encrypted content "
                    "gAAA...flvh could not be verified."
                ),
                "type": "invalid_request_error",
                "code": "-4003",
            }
        },
    )


@respx.mock
async def test_invalid_encrypted_content_retries_once_without_reasoning() -> None:
    route = respx.post(ENDPOINT).mock(
        side_effect=[_stale(), ok_stream(responses_payload(texts=["recovered"]))]
    )
    response = await make_provider().complete(_request_with_prior_reasoning())
    assert response.message.content == [Text(text="recovered")]
    assert route.call_count == 2
    first, second = sent_body(route, 0), sent_body(route, 1)
    assert any(item["type"] == "reasoning" for item in first["input"])
    assert not any(item["type"] == "reasoning" for item in second["input"])
    # Request-level knobs stay, so the retried turn still reasons fresh.
    assert second["include"] == ["reasoning.encrypted_content"]
    assert second["stream"] is True


@respx.mock
async def test_invalid_encrypted_content_retried_once_only() -> None:
    route = respx.post(ENDPOINT).mock(side_effect=[_stale(), _stale(), _stale()])
    with pytest.raises(Fatal, match="encrypted content"):
        await make_provider().complete(_request_with_prior_reasoning())
    assert route.call_count == 2


@respx.mock
async def test_invalid_encrypted_content_without_reasoning_stays_fatal() -> None:
    route = _mock_status(
        400, json={"error": {"message": "invalid_encrypted_content", "code": "-4003"}}
    )
    with pytest.raises(Fatal):
        await make_provider().complete(basic_request())
    assert route.call_count == 1


@respx.mock
async def test_invalid_encrypted_content_matched_by_code() -> None:
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": "nope", "code": "invalid_encrypted_content"}}),
            ok_stream(),
        ]
    )
    await make_provider().complete(_request_with_prior_reasoning())
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# Malformed payloads
# ---------------------------------------------------------------------------


@respx.mock
async def test_missing_output_is_fatal() -> None:
    respx.post(ENDPOINT).mock(
        return_value=sse_response(responses_sse({"id": "x", "status": "completed"}))
    )
    with pytest.raises(Fatal, match="output"):
        await make_provider().complete(basic_request())


@respx.mock
async def test_non_sse_body_is_a_truncated_stream() -> None:
    """A 200 whose body carries no terminal event (plain text, a JSON array)
    never finished — retryable like a dropped stream."""
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, content=b"not-json", headers={"content-type": "text/plain"})
    )
    with pytest.raises(Transient):
        await make_provider().complete(basic_request())


@respx.mock
async def test_mock_ok_default_is_plain_completion() -> None:
    route = mock_ok()
    response = await make_provider().complete(basic_request())
    assert route.called and response.message.text == "ok"
