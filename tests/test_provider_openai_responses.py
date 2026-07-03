"""Test matrix for :class:`noeta.providers.openai_responses.OpenAIResponsesProvider`.

Adapter foundation (text part):
a tracer bullet of text in, text out. All HTTP traffic goes through a ``respx`` mock;
the suite makes zero real network calls.

Tools (03), reasoning (04), and images (05) are out of scope here, but the full
stop_reason priority table is covered (including tool_use priority, even though the
text path never triggers it).
"""

from __future__ import annotations

import json
from typing import Any

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
    LLMResponse,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.values import ContentRef
from noeta.providers.openai_responses import OpenAIResponsesProvider


# base_url IS the complete responses endpoint
# (re-probed and corrected 2026-06-12): the provider POSTs directly to that URL, adding only the
# ?api-version query, and does NOT append a /openai/responses path (in practice, appending the path
# fails, whereas POSTing the URL as-is returns 200). So ENDPOINT and BASE_URL are the same URL, and
# the endpoint path already contains .../responses.
BASE_URL = "https://gateway.test/api/modelhub/online/responses"
ENDPOINT = BASE_URL


def _make_provider(**overrides: Any) -> OpenAIResponsesProvider:
    kwargs: dict[str, Any] = {
        "base_url": BASE_URL,
        "api_key": "sk-test",
    }
    kwargs.update(overrides)
    return OpenAIResponsesProvider(**kwargs)


def _user_message(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)])


def _basic_request(
    *,
    model: str = "gpt-5.4",
    text: str = "hi",
    system: Message | None = None,
    messages: list[Message] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> LLMRequest:
    return LLMRequest(
        model=model,
        messages=messages if messages is not None else [_user_message(text)],
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def _responses_payload(
    *,
    texts: list[str] | None = None,
    status: str = "completed",
    incomplete_reason: str | None = None,
    extra_output: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a Responses-style response.

    ``texts`` → one ``message`` item whose ``content[]`` holds that many
    ``output_text`` segments (None → no message item). ``extra_output`` is appended
    to the end of the output array (e.g. inserting a function_call item to test priority).
    """
    output: list[dict[str, Any]] = []
    if texts is not None:
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": t} for t in texts
                ],
            }
        )
    if extra_output:
        output.extend(extra_output)
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


# ---------------------------------------------------------------------------
# 1. Text round trip
# ---------------------------------------------------------------------------


@respx.mock
def test_text_round_trip_maps_to_end_turn_textblock() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["hello"]))
    )

    provider = _make_provider()
    response = provider.complete(_basic_request(text="say hi"))

    assert route.called
    assert isinstance(response, LLMResponse)
    assert response.stop_reason == "end_turn"
    assert response.content == [TextBlock(text="hello")]
    assert response.raw is not None and response.raw["id"] == "resp-xyz"


# ---------------------------------------------------------------------------
# 2. Multiple output_text segments: one TextBlock each
# ---------------------------------------------------------------------------


@respx.mock
def test_multiple_output_text_segments_each_become_textblock() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, json=_responses_payload(texts=["part one", "part two", "part three"])
        )
    )
    response = _make_provider().complete(_basic_request())
    assert response.content == [
        TextBlock(text="part one"),
        TextBlock(text="part two"),
        TextBlock(text="part three"),
    ]


# ---------------------------------------------------------------------------
# 3. system → top-level instructions (flattened text)
# ---------------------------------------------------------------------------


@respx.mock
def test_system_field_flattens_to_top_level_instructions() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    request = _basic_request(
        system=Message(
            role="system",
            content=[TextBlock(text="be terse"), TextBlock(text="answer in en")],
        ),
        messages=[_user_message("hello")],
    )
    _make_provider().complete(request)

    body = json.loads(route.calls[0].request.content)
    assert body["instructions"] == "be terse\nanswer in en"
    # system does not go into the input array.
    assert body["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        }
    ]


# ---------------------------------------------------------------------------
# 4. Outbound input message shape: user→input_text, assistant→output_text
# ---------------------------------------------------------------------------


@respx.mock
def test_user_and_assistant_messages_shape_in_input() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    request = _basic_request(
        messages=[
            _user_message("first ask"),
            Message(role="assistant", content=[TextBlock(text="prior answer")]),
            _user_message("follow up"),
        ]
    )
    _make_provider().complete(request)

    body = json.loads(route.calls[0].request.content)
    assert body["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "first ask"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "prior answer"}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "follow up"}],
        },
    ]


# ---------------------------------------------------------------------------
# 5. store:false always present; model in body
# ---------------------------------------------------------------------------


@respx.mock
def test_store_false_always_present_and_model_in_body() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider().complete(_basic_request(model="gpt-5.4-2026-03-05"))

    body = json.loads(route.calls[0].request.content)
    assert body["store"] is False
    assert body["model"] == "gpt-5.4-2026-03-05"


@respx.mock
def test_same_provider_instance_routes_different_models() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider()
    provider.complete(_basic_request(model="gpt-5.4"))
    provider.complete(_basic_request(model="gpt-5.4-mini"))
    assert route.call_count == 2
    assert json.loads(route.calls[0].request.content)["model"] == "gpt-5.4"
    assert json.loads(route.calls[1].request.content)["model"] == "gpt-5.4-mini"


# ---------------------------------------------------------------------------
# 6. temperature / max_tokens → max_output_tokens
# ---------------------------------------------------------------------------


@respx.mock
def test_temperature_and_max_output_tokens_wired() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider().complete(
        _basic_request(temperature=0.3, max_tokens=512)
    )
    body = json.loads(route.calls[0].request.content)
    assert body["temperature"] == 0.3
    assert body["max_output_tokens"] == 512
    assert "max_tokens" not in body


@respx.mock
def test_default_max_tokens_used_when_request_has_none() -> None:
    # A host-configured default fills in max_output_tokens so a request without
    # its own cap no longer inherits the gateway's (small) default and truncates.
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider(default_max_tokens=8000).complete(_basic_request())
    body = json.loads(route.calls[0].request.content)
    assert body["max_output_tokens"] == 8000


@respx.mock
def test_request_max_tokens_overrides_default() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider(default_max_tokens=8000).complete(_basic_request(max_tokens=512))
    body = json.loads(route.calls[0].request.content)
    assert body["max_output_tokens"] == 512


@respx.mock
def test_optional_fields_omitted_when_unset() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider().complete(_basic_request())
    body = json.loads(route.calls[0].request.content)
    assert "temperature" not in body
    assert "max_output_tokens" not in body
    assert "instructions" not in body


# ---------------------------------------------------------------------------
# 7. api-key header (not Bearer) + extra_headers merge + URL/api-version
# ---------------------------------------------------------------------------


@respx.mock
def test_api_key_header_not_bearer_and_extra_headers_merged() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(
        api_key="sk-secret",
        extra_headers={"X-TT-LOGID": "log-123"},
    )
    provider.complete(_basic_request())

    req = route.calls[0].request
    assert req.headers["api-key"] == "sk-secret"
    assert "authorization" not in req.headers
    assert req.headers["x-tt-logid"] == "log-123"


@respx.mock
def test_request_headers_merge_with_default_headers() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(
        extra_headers={"X-Static": "static", "X-TT-logid": "static-log"},
    )

    provider.complete_with_headers(
        _basic_request(),
        {
            "extra": '{"session_id":"task-abc"}',
            "X-TT-logid": "task-abc",
        },
    )

    req = route.calls[0].request
    assert req.headers["x-static"] == "static"
    assert req.headers["extra"] == '{"session_id":"task-abc"}'
    assert req.headers["x-tt-logid"] == "task-abc"


@respx.mock
def test_api_version_appended_as_query_param() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(api_version="2026-03-01-preview")
    provider.complete(_basic_request())

    req = route.calls[0].request
    assert req.url.params["api-version"] == "2026-03-01-preview"
    # base_url IS the full endpoint: the POST path is the endpoint path itself, with NO extra
    # /openai/responses segment (re-probed).
    assert req.url.path == "/api/modelhub/online/responses"


@respx.mock
def test_endpoint_path_is_base_url_unchanged_no_doubled_segment() -> None:
    """The provider POSTs base_url directly and does NOT append a /openai/responses path.

    Re-probed evidence: in the real gateway, base_url is already the full responses endpoint;
    appending the path fails, whereas POSTing as-is returns 200
    (corrected 2026-06-12). This pins the
    endpoint path to exactly base_url's path, with no duplicated .../responses/openai/responses segment.
    """
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider().complete(_basic_request())

    req = route.calls[0].request
    assert req.url.path == "/api/modelhub/online/responses"
    assert "/openai/responses" not in req.url.path
    assert str(req.url).split("?", 1)[0] == ENDPOINT


@respx.mock
def test_no_api_version_means_no_query_param() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider().complete(_basic_request())
    req = route.calls[0].request
    assert "api-version" not in req.url.params


@respx.mock
def test_trailing_slash_in_base_url_is_normalised() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    OpenAIResponsesProvider(
        base_url=BASE_URL + "/", api_key="sk-test"
    ).complete(_basic_request())
    assert route.called


def test_default_timeout_is_300_seconds() -> None:
    provider = _make_provider()
    # High-effort reasoning routinely takes ~80s, so the default must allow a full 300s (probed).
    assert provider._client.timeout.read == 300.0


# ---------------------------------------------------------------------------
# 8. Full coverage of the stop_reason priority table
# ---------------------------------------------------------------------------


@respx.mock
def test_completed_status_maps_to_end_turn() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, json=_responses_payload(texts=["done"], status="completed")
        )
    )
    response = _make_provider().complete(_basic_request())
    assert response.stop_reason == "end_turn"


@respx.mock
def test_function_call_item_maps_to_tool_use() -> None:
    payload = _responses_payload(
        texts=None,
        status="completed",
        extra_output=[
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "echo",
                "arguments": "{}",
            }
        ],
    )
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
    response = _make_provider().complete(_basic_request())
    assert response.stop_reason == "tool_use"


@respx.mock
def test_incomplete_max_output_tokens_maps_to_max_tokens() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_responses_payload(
                texts=["partial"],
                status="incomplete",
                incomplete_reason="max_output_tokens",
            ),
        )
    )
    response = _make_provider().complete(_basic_request())
    assert response.stop_reason == "max_tokens"


@respx.mock
def test_incomplete_max_output_tokens_beats_partial_function_call() -> None:
    """max_tokens wins over a partial function_call (consistent with Chat's length precedent)."""
    payload = _responses_payload(
        texts=None,
        status="incomplete",
        incomplete_reason="max_output_tokens",
        extra_output=[
            {
                "type": "function_call",
                "call_id": "call_x",
                "name": "echo",
                "arguments": "{}",
            }
        ],
    )
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
    response = _make_provider().complete(_basic_request())
    assert response.stop_reason == "max_tokens"


@respx.mock
def test_failed_status_maps_to_error() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, json=_responses_payload(texts=None, status="failed")
        )
    )
    response = _make_provider().complete(_basic_request())
    assert response.stop_reason == "error"


@respx.mock
def test_content_filter_status_maps_to_error() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, json=_responses_payload(texts=None, status="content_filter")
        )
    )
    response = _make_provider().complete(_basic_request())
    assert response.stop_reason == "error"


@respx.mock
def test_incomplete_non_max_tokens_reason_falls_through_to_error() -> None:
    """incomplete, but reason is not max_output_tokens and there is no function_call →
    not completed → error."""
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_responses_payload(
                texts=["x"],
                status="incomplete",
                incomplete_reason="content_filter",
            ),
        )
    )
    response = _make_provider().complete(_basic_request())
    assert response.stop_reason == "error"


# ---------------------------------------------------------------------------
# 9. usage mapping (including cached_tokens → cache_read, one field beyond Chat)
# ---------------------------------------------------------------------------


@respx.mock
def test_usage_maps_with_cached_tokens_to_cache_read() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_responses_payload(
                texts=["ok"],
                usage={
                    "input_tokens": 100,
                    "output_tokens": 40,
                    "input_tokens_details": {"cached_tokens": 30},
                    "output_tokens_details": {"reasoning_tokens": 15},
                },
            ),
        )
    )
    response = _make_provider().complete(_basic_request())
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


@respx.mock
def test_usage_without_details_defaults_to_zero_cache() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_responses_payload(
                texts=["ok"],
                usage={"input_tokens": 12, "output_tokens": 5},
            ),
        )
    )
    response = _make_provider().complete(_basic_request())
    assert response.usage == Usage(uncached=12, output=5)
    assert response.usage.cache_read == 0


@respx.mock
def test_missing_usage_yields_empty_usage() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    response = _make_provider().complete(_basic_request())
    assert response.usage == Usage()


# ---------------------------------------------------------------------------
# 10. Error classification (429 / 5xx / 400 ctx / other 4xx)
# ---------------------------------------------------------------------------


@respx.mock
def test_429_with_retry_after_maps_to_transient() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            429, json={"error": "rate_limited"}, headers={"Retry-After": "7"}
        )
    )
    with pytest.raises(TransientError) as ex:
        _make_provider().complete(_basic_request())
    assert ex.value.retry_after == 7.0


@respx.mock
def test_429_without_retry_after_maps_to_transient_none() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(429, json={"error": "rate_limited"})
    )
    with pytest.raises(TransientError) as ex:
        _make_provider().complete(_basic_request())
    assert ex.value.retry_after is None


@respx.mock
def test_500_maps_to_transient_error() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    with pytest.raises(TransientError):
        _make_provider().complete(_basic_request())


@respx.mock
def test_503_maps_to_transient_error() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(503, json={"error": "unavailable"})
    )
    with pytest.raises(TransientError):
        _make_provider().complete(_basic_request())


@respx.mock
def test_400_context_length_exceeded_maps_to_overflow() -> None:
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
        _make_provider().complete(_basic_request())


@respx.mock
def test_400_plain_invalid_request_maps_to_fatal() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            400,
            json={"error": {"message": "missing field", "type": "invalid_request_error"}},
        )
    )
    with pytest.raises(FatalError):
        _make_provider().complete(_basic_request())


def _request_with_prior_reasoning() -> LLMRequest:
    """A resumed turn whose history carries a prior-turn ThinkingBlock — its
    ``signature`` echoes as a ``reasoning`` input item with ``encrypted_content``."""
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
def test_400_invalid_encrypted_content_retries_without_reasoning() -> None:
    """Stale cross-turn reasoning ciphertext (gateway 400
    ``invalid_encrypted_content``) self-heals: the echoed ``reasoning`` input
    items are dropped and the request is retried ONCE — turning a fatal resume
    into a normal turn."""
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
            httpx.Response(200, json=_responses_payload(texts=["recovered"])),
        ]
    )

    response = _make_provider().complete(_request_with_prior_reasoning())

    assert response.content == [TextBlock(text="recovered")]
    assert len(route.calls) == 2
    first = json.loads(route.calls[0].request.content)["input"]
    second = json.loads(route.calls[1].request.content)["input"]
    assert any(it.get("type") == "reasoning" for it in first)   # original echoed it
    assert not any(it.get("type") == "reasoning" for it in second)  # retry stripped it


@respx.mock
def test_400_invalid_encrypted_content_without_reasoning_stays_fatal() -> None:
    """No echoed reasoning to strip ⇒ no retry loop — a genuine
    ``invalid_encrypted_content`` with nothing to drop is still fatal, sent once."""
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
        _make_provider().complete(_basic_request())
    assert len(route.calls) == 1


@respx.mock
def test_401_maps_to_fatal_error() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(401, json={"error": "invalid_api_key"})
    )
    with pytest.raises(FatalError):
        _make_provider().complete(_basic_request())


@respx.mock
def test_connect_error_maps_to_transient() -> None:
    respx.post(ENDPOINT).mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    with pytest.raises(TransientError):
        _make_provider().complete(_basic_request())


@respx.mock
def test_timeout_maps_to_transient() -> None:
    respx.post(ENDPOINT).mock(side_effect=httpx.ReadTimeout("timed out"))
    with pytest.raises(TransientError):
        _make_provider().complete(_basic_request())


# ---------------------------------------------------------------------------
# 11. Parse failures / defensive checks
# ---------------------------------------------------------------------------


@respx.mock
def test_non_json_body_raises_value_error() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, content=b"not-json", headers={"content-type": "text/plain"}
        )
    )
    with pytest.raises(ValueError):
        _make_provider().complete(_basic_request())


@respx.mock
def test_missing_output_raises_value_error() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"id": "x", "status": "completed"})
    )
    with pytest.raises(ValueError, match="output"):
        _make_provider().complete(_basic_request())


@respx.mock
def test_json_array_root_raises_value_error() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=["not", "a", "dict"])
    )
    with pytest.raises(ValueError, match="JSON object"):
        _make_provider().complete(_basic_request())


@respx.mock
def test_system_role_in_messages_array_raises_value_error() -> None:
    request = LLMRequest(
        model="gpt-5.4",
        messages=[Message(role="system", content=[TextBlock(text="x")])],
    )
    with pytest.raises(ValueError, match="LLMRequest.system"):
        _make_provider().complete(request)


# ---------------------------------------------------------------------------
# 12. Tool round trip (tool part)
# ---------------------------------------------------------------------------


def _chat_tool(name: str = "get_weather") -> dict[str, Any]:
    """Chat-compatible nested tool shape (params buried in a function:{…} subobject)."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "look up the weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }


@respx.mock
def test_outbound_tool_use_block_becomes_top_level_function_call() -> None:
    """An assistant ToolUseBlock → a top-level function_call item (not nested inside the
    message), with arguments serialized to a JSON string."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    request = _basic_request(
        messages=[
            _user_message("weather?"),
            Message(
                role="assistant",
                content=[
                    TextBlock(text="let me check"),
                    ToolUseBlock(
                        call_id="call_abc",
                        tool_name="get_weather",
                        arguments={"city": "Beijing"},
                    ),
                ],
            ),
        ]
    )
    _make_provider().complete(request)

    body = json.loads(route.calls[0].request.content)
    assert body["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "weather?"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "let me check"}],
        },
        {
            "type": "function_call",
            "call_id": "call_abc",
            "name": "get_weather",
            "arguments": json.dumps({"city": "Beijing"}),
        },
    ]


@respx.mock
def test_outbound_multiple_tool_use_blocks_each_top_level_item() -> None:
    """Multiple ToolUseBlocks in one assistant message → multiple independent function_call items."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    request = _basic_request(
        messages=[
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(call_id="c1", tool_name="a", arguments={}),
                    ToolUseBlock(call_id="c2", tool_name="b", arguments={"x": 1}),
                ],
            ),
        ]
    )
    _make_provider().complete(request)

    body = json.loads(route.calls[0].request.content)
    # A pure tool-call turn (no text) sends no empty message item: just two function_call items.
    assert body["input"] == [
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "a",
            "arguments": json.dumps({}),
        },
        {
            "type": "function_call",
            "call_id": "c2",
            "name": "b",
            "arguments": json.dumps({"x": 1}),
        },
    ]


@respx.mock
def test_outbound_tool_result_block_becomes_function_call_output() -> None:
    """A tool-role ToolResultBlock → a function_call_output item; str output passes through
    as-is, non-str output goes through JSON serialization."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    request = _basic_request(
        messages=[
            Message(
                role="tool",
                content=[
                    ToolResultBlock(
                        call_id="call_abc", output="sunny, 22C", success=True
                    ),
                    ToolResultBlock(
                        call_id="call_def",
                        output={"temp": 22, "sky": "clear"},
                        success=True,
                    ),
                ],
            ),
        ]
    )
    _make_provider().complete(request)

    body = json.loads(route.calls[0].request.content)
    assert body["input"] == [
        {
            "type": "function_call_output",
            "call_id": "call_abc",
            "output": "sunny, 22C",
        },
        {
            "type": "function_call_output",
            "call_id": "call_def",
            "output": json.dumps({"temp": 22, "sky": "clear"}),
        },
    ]


@respx.mock
def test_tools_array_de_nests_from_chat_to_flat_responses_shape() -> None:
    """tools de-nesting: Chat's {type:function,function:{…}} → Responses' flat
    {type:function,name,description,parameters}."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    request = LLMRequest(
        model="gpt-5.4",
        messages=[_user_message("hi")],
        tools=[_chat_tool("get_weather")],
    )
    _make_provider().complete(request)

    body = json.loads(route.calls[0].request.content)
    assert body["tools"] == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "look up the weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]
    # After de-nesting, NO leftover function subobject.
    assert "function" not in body["tools"][0]


@respx.mock
def test_already_flat_tool_is_passed_through_unchanged() -> None:
    """Already in Responses flat shape (no function subobject) → passed through as-is, idempotent."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    flat = {
        "type": "function",
        "name": "ping",
        "description": "ping",
        "parameters": {"type": "object", "properties": {}},
    }
    request = LLMRequest(
        model="gpt-5.4", messages=[_user_message("hi")], tools=[flat]
    )
    _make_provider().complete(request)

    body = json.loads(route.calls[0].request.content)
    assert body["tools"] == [flat]


@respx.mock
def test_no_tools_means_no_tools_key_in_body() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider().complete(_basic_request())
    body = json.loads(route.calls[0].request.content)
    assert "tools" not in body


@respx.mock
def test_tool_choice_passed_through_only_from_metadata() -> None:
    """LLMRequest has no tool_choice field; it is forwarded only when metadata gives it explicitly."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    request = LLMRequest(
        model="gpt-5.4",
        messages=[_user_message("hi")],
        tools=[_chat_tool()],
        metadata={"tool_choice": "required"},
    )
    _make_provider().complete(request)
    body = json.loads(route.calls[0].request.content)
    assert body["tool_choice"] == "required"


@respx.mock
def test_tool_choice_absent_when_not_in_metadata() -> None:
    """When absent, don't fabricate tool_choice (let the gateway use its default auto)."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    request = LLMRequest(
        model="gpt-5.4", messages=[_user_message("hi")], tools=[_chat_tool()]
    )
    _make_provider().complete(request)
    body = json.loads(route.calls[0].request.content)
    assert "tool_choice" not in body


@respx.mock
def test_inbound_function_call_maps_to_tool_use_block() -> None:
    """Inbound function_call item → ToolUseBlock, with arguments restored to a dict via
    json.loads, stop_reason=tool_use."""
    payload = _responses_payload(
        texts=None,
        status="completed",
        extra_output=[
            {
                "type": "function_call",
                "id": "fc_internal_999",
                "call_id": "call_abc",
                "name": "get_weather",
                "arguments": json.dumps({"city": "Shanghai"}),
            }
        ],
    )
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
    response = _make_provider().complete(_basic_request())

    assert response.stop_reason == "tool_use"
    assert response.content == [
        ToolUseBlock(
            call_id="call_abc",
            tool_name="get_weather",
            arguments={"city": "Shanghai"},
        )
    ]


@respx.mock
def test_inbound_call_id_taken_from_call_id_not_internal_id() -> None:
    """The pairing id comes from the call_id field, NOT the internal id (probing shows both coexist)."""
    payload = _responses_payload(
        texts=None,
        status="completed",
        extra_output=[
            {
                "type": "function_call",
                "id": "fc_internal_must_not_be_used",
                "call_id": "call_correct",
                "name": "echo",
                "arguments": "{}",
            }
        ],
    )
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
    response = _make_provider().complete(_basic_request())

    block = response.content[0]
    assert isinstance(block, ToolUseBlock)
    assert block.call_id == "call_correct"


@respx.mock
def test_inbound_text_and_function_call_both_in_content() -> None:
    """message text + function_call together → TextBlock followed by ToolUseBlock."""
    payload = _responses_payload(
        texts=["thinking out loud"],
        status="completed",
        extra_output=[
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "tool_a",
                "arguments": json.dumps({"k": "v"}),
            }
        ],
    )
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
    response = _make_provider().complete(_basic_request())

    assert response.stop_reason == "tool_use"
    assert response.content == [
        TextBlock(text="thinking out loud"),
        ToolUseBlock(call_id="c1", tool_name="tool_a", arguments={"k": "v"}),
    ]


@respx.mock
def test_inbound_invalid_json_arguments_raises_value_error() -> None:
    """Invalid JSON arguments → MalformedToolArgumentsError.

    Still a ``ValueError`` (so this wording/type contract is unchanged), but
    additionally a ``TransientError`` so RuntimeLLMClient retries a truncated
    tool-call stream on its transient budget instead of failing the task fatally.
    """
    from noeta.protocols.errors import MalformedToolArgumentsError, TransientError

    payload = _responses_payload(
        texts=None,
        status="completed",
        extra_output=[
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "broken",
                "arguments": "{not valid json",
            }
        ],
    )
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(ValueError, match="not JSON-decodable") as excinfo:
        _make_provider().complete(_basic_request())
    assert isinstance(excinfo.value, MalformedToolArgumentsError)
    assert isinstance(excinfo.value, TransientError)
    assert excinfo.value.category == "transient"


@respx.mock
def test_inbound_multiple_function_calls_paired_each_by_call_id() -> None:
    """Multiple function_calls → multiple ToolUseBlocks, each paired by its own call_id."""
    payload = _responses_payload(
        texts=None,
        status="completed",
        extra_output=[
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "a",
                "arguments": "{}",
            },
            {
                "type": "function_call",
                "call_id": "c2",
                "name": "b",
                "arguments": json.dumps({"n": 2}),
            },
        ],
    )
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
    response = _make_provider().complete(_basic_request())

    assert response.content == [
        ToolUseBlock(call_id="c1", tool_name="a", arguments={}),
        ToolUseBlock(call_id="c2", tool_name="b", arguments={"n": 2}),
    ]


# ---------------------------------------------------------------------------
# 13. Reasoning chain + request-level binding
# ---------------------------------------------------------------------------


def _reasoning_request(
    *,
    effort: str | None = None,
    thinking: str | None = None,
    output_schema: dict[str, Any] | None = None,
    messages: list[Message] | None = None,
) -> LLMRequest:
    return LLMRequest(
        model="gpt-5.4",
        messages=messages if messages is not None else [_user_message("hard problem")],
        effort=effort,
        thinking=thinking,
        output_schema=output_schema,
    )


# --- Request: when reasoning is in play, carry reasoning{effort,summary:auto} + include + store:false ---


@respx.mock
def test_reasoning_request_block_present_when_effort_in_play() -> None:
    """When mapped effort is non-None → body carries reasoning{effort,summary:auto} +
    include:[reasoning.encrypted_content]; store:false is still always present."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider().complete(_reasoning_request(effort="high"))

    body = json.loads(route.calls[0].request.content)
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["store"] is False


@respx.mock
def test_no_reasoning_block_when_effort_is_none_but_include_stays() -> None:
    """Neither effort nor thinking yields an effort → no reasoning block, but
    include:[reasoning.encrypted_content] is still requested: a reasoning model
    reasons at its server-side default effort even without a reasoning{} block,
    and without the ciphertext the next turn's echo is an empty shell that
    breaks continuation and the prompt-cache prefix."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider().complete(_reasoning_request())

    body = json.loads(route.calls[0].request.content)
    assert "reasoning" not in body
    assert body["include"] == ["reasoning.encrypted_content"]
    # store:false is still always present (independent of reasoning).
    assert body["store"] is False


@respx.mock
def test_no_include_when_continuation_off() -> None:
    """reasoning_continuation="off" → the ciphertext is never echoed back, so
    include:[reasoning.encrypted_content] is not requested either (the escape
    hatch for gateways that reject the include param)."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider(reasoning_continuation="off").complete(
        _reasoning_request(effort="high")
    )

    body = json.loads(route.calls[0].request.content)
    # The explicit effort still maps into the reasoning block; only the
    # continuation include is dropped.
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert "include" not in body


# --- effort mapping: low/medium/high pass through, xhigh/max→high, None unset ---


@respx.mock
@pytest.mark.parametrize(
    ("effort_in", "effort_out"),
    [
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "high"),
        ("max", "high"),
    ],
)
def test_effort_map_passthrough_and_collapse(
    effort_in: str, effort_out: str
) -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider().complete(_reasoning_request(effort=effort_in))

    body = json.loads(route.calls[0].request.content)
    assert body["reasoning"]["effort"] == effort_out


@respx.mock
def test_effort_none_means_no_reasoning_block() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider().complete(_reasoning_request(effort=None))
    body = json.loads(route.calls[0].request.content)
    assert "reasoning" not in body


# --- thinking mapping: disabled→minimal, adaptive/None derive nothing from thinking ---


@respx.mock
def test_thinking_disabled_derives_minimal_effort() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider().complete(_reasoning_request(thinking="disabled"))

    body = json.loads(route.calls[0].request.content)
    assert body["reasoning"]["effort"] == "minimal"
    assert body["include"] == ["reasoning.encrypted_content"]


@respx.mock
@pytest.mark.parametrize("thinking", ["adaptive", None])
def test_thinking_adaptive_or_none_does_not_derive_effort(
    thinking: str | None,
) -> None:
    """adaptive/None derive no effort from thinking; with no effort field, carry no reasoning."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider().complete(_reasoning_request(thinking=thinking))

    body = json.loads(route.calls[0].request.content)
    assert "reasoning" not in body


@respx.mock
def test_thinking_disabled_overrides_explicit_effort() -> None:
    """disabled is an explicit signal to suppress reasoning → collapses to minimal even when effort=high."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider().complete(
        _reasoning_request(effort="high", thinking="disabled")
    )
    body = json.loads(route.calls[0].request.content)
    assert body["reasoning"]["effort"] == "minimal"


@respx.mock
def test_thinking_adaptive_keeps_explicit_effort() -> None:
    """adaptive derives no effort, but an explicit effort still takes effect."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider().complete(
        _reasoning_request(effort="medium", thinking="adaptive")
    )
    body = json.loads(route.calls[0].request.content)
    assert body["reasoning"]["effort"] == "medium"


# --- output_schema → text.format json_schema ---


@respx.mock
def test_output_schema_maps_to_text_format_json_schema() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    _make_provider().complete(_reasoning_request(output_schema=schema))

    body = json.loads(route.calls[0].request.content)
    assert body["text"] == {
        "format": {
            "type": "json_schema",
            "name": "noeta_output",
            "schema": schema,
        }
    }


@respx.mock
def test_no_output_schema_means_no_text_key() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    _make_provider().complete(_reasoning_request())
    body = json.loads(route.calls[0].request.content)
    assert "text" not in body


# --- Inbound: reasoning item → ThinkingBlock, encrypted_content preserved verbatim ---


def _reasoning_item(
    *, summary_texts: list[str], encrypted_content: str
) -> dict[str, Any]:
    return {
        "type": "reasoning",
        "summary": [
            {"type": "summary_text", "text": t} for t in summary_texts
        ],
        "encrypted_content": encrypted_content,
    }


@respx.mock
def test_inbound_reasoning_maps_to_thinking_block() -> None:
    """reasoning item → ThinkingBlock: text is the concatenation of summary segments,
    signature is encrypted_content."""
    enc = "gAAAA" + "x" * 120  # simulate opaque ciphertext (~21.6KB in practice)
    payload = _responses_payload(
        texts=["final answer"],
        status="completed",
        extra_output=[
            _reasoning_item(
                summary_texts=["reasoning step one", "reasoning step two"],
                encrypted_content=enc,
            )
        ],
    )
    # The reasoning item should come before the message (probing shows reasoning precedes the
    # answer), so put it first in the array.
    payload["output"] = [payload["output"][-1], payload["output"][0]]
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
    response = _make_provider().complete(_basic_request())

    assert response.content == [
        ThinkingBlock(text="reasoning step one\nreasoning step two", signature=enc),
        TextBlock(text="final answer"),
    ]


@respx.mock
def test_inbound_reasoning_encrypted_content_round_trips_verbatim() -> None:
    """encrypted_content must round-trip verbatim (the continuation ciphertext can't change a byte)."""
    enc = "ENC::" + "".join(chr(33 + (i % 90)) for i in range(2048))
    payload = _responses_payload(
        texts=None,
        status="completed",
        extra_output=[
            _reasoning_item(summary_texts=["thinking"], encrypted_content=enc)
        ],
    )
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
    response = _make_provider().complete(_basic_request())

    block = response.content[0]
    assert isinstance(block, ThinkingBlock)
    assert block.signature == enc


@respx.mock
def test_inbound_reasoning_empty_summary_yields_empty_text() -> None:
    """Empty summary array → empty text string, signature still preserved."""
    enc = "enc-no-summary"
    payload = _responses_payload(
        texts=None,
        status="completed",
        extra_output=[
            _reasoning_item(summary_texts=[], encrypted_content=enc)
        ],
    )
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
    response = _make_provider().complete(_basic_request())

    assert response.content == [ThinkingBlock(text="", signature=enc)]


# --- Outbound: ThinkingBlock → reasoning item (on by default), signature stuffed back as-is ---


@respx.mock
def test_outbound_thinking_block_becomes_reasoning_item_default_on() -> None:
    """Default reasoning_continuation="responses": an assistant-turn ThinkingBlock →
    {type:reasoning,encrypted_content,summary}, where encrypted_content is the signature
    stuffed back as-is for continuation."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    enc = "sig-verbatim-зашифровано"
    request = _basic_request(
        messages=[
            _user_message("hard problem"),
            Message(
                role="assistant",
                content=[
                    ThinkingBlock(text="let me think", signature=enc),
                    TextBlock(text="my answer"),
                ],
            ),
        ]
    )
    _make_provider().complete(request)

    body = json.loads(route.calls[0].request.content)
    assert body["input"][0] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "hard problem"}],
    }
    # reasoning item sent back, summary segment refilled from block.text, encrypted_content=signature.
    assert body["input"][1] == {
        "type": "reasoning",
        "encrypted_content": enc,
        "summary": [{"type": "summary_text", "text": "let me think"}],
    }
    assert body["input"][2] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "my answer"}],
    }


@respx.mock
def test_outbound_reasoning_item_placed_before_function_call() -> None:
    """The reasoning item comes before this turn's function_call (per the 0033 re-attach: the
    thinking chain is re-attached into the View before tool_use, and the provider serializes in
    appearance order)."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    enc = "thinking-sig"
    request = _basic_request(
        messages=[
            Message(
                role="assistant",
                content=[
                    ThinkingBlock(text="figuring out which tool to call", signature=enc),
                    ToolUseBlock(
                        call_id="call_1", tool_name="lookup", arguments={"q": "x"}
                    ),
                ],
            ),
        ]
    )
    _make_provider().complete(request)

    body = json.loads(route.calls[0].request.content)
    types = [item["type"] for item in body["input"]]
    # No text/image → no empty message item; only reasoning → function_call remain.
    assert types == ["reasoning", "function_call"]
    reasoning_item = body["input"][types.index("reasoning")]
    assert reasoning_item["encrypted_content"] == enc
    assert reasoning_item["summary"] == [
        {"type": "summary_text", "text": "figuring out which tool to call"}
    ]


@respx.mock
def test_outbound_thinking_block_dropped_when_continuation_off() -> None:
    """reasoning_continuation="off" → no reasoning item sent back (symmetric with Chat's default off)."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    request = _basic_request(
        messages=[
            Message(
                role="assistant",
                content=[
                    ThinkingBlock(text="hidden reasoning", signature="sig"),
                    TextBlock(text="answer"),
                ],
            ),
        ]
    )
    _make_provider(reasoning_continuation="off").complete(request)

    body = json.loads(route.calls[0].request.content)
    types = [item["type"] for item in body["input"]]
    assert "reasoning" not in types
    # Text still becomes a message item as usual.
    assert body["input"] == [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "answer"}],
        }
    ]


@respx.mock
def test_outbound_thinking_block_without_signature_not_echoed() -> None:
    """signature is None (no ciphertext) → the ThinkingBlock is NOT echoed as a
    reasoning input item at all: without encrypted_content the item cannot
    restore any reasoning tokens, and sending the empty shell breaks the
    gateway's prompt-cache prefix at that position (observed on subagent
    conversations whose turns never got a cache hit past the static head)."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    request = _basic_request(
        messages=[
            Message(
                role="assistant",
                content=[
                    ThinkingBlock(text="reasoning without ciphertext", signature=None),
                    TextBlock(text="answer"),
                ],
            ),
        ]
    )
    _make_provider().complete(request)

    body = json.loads(route.calls[0].request.content)
    types = [item["type"] for item in body["input"]]
    assert "reasoning" not in types
    # The rest of the assistant turn is unaffected.
    assert {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "answer"}],
    } in body["input"]


# ---------------------------------------------------------------------------
# 14. Images go live: ImageBlock → input_image data URI + image_resolver (issue 05)
# ---------------------------------------------------------------------------
#
# In the ledger, an ImageBlock carries only a small ContentRef handle. At wire-assembly time the
# provider derefs it with the injected narrow resolver (ContentRef→bytes), base64-encodes it, and
# inlines it as {type:input_image,image_url:"data:<media>;base64,<…>"}. The base64 appears ONLY in
# the outgoing wire body and is never written back to the ledger/ContentStore
# (red line). The inline primitive is general:
# it walks ImageBlocks at ANY message position (not just the last user turn), paving the way at zero
# cost for future pull (an image-reading tool returning images).


import base64 as _base64


# Real bytes of a 1x1 transparent PNG (small enough, unambiguous media type), content-addressed into a ContentRef.
_PNG_BYTES = _base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQ"
    "DJxAAAAABJRU5ErkJggg=="
)
_PNG_REF = ContentRef(hash="sha256:png", size=len(_PNG_BYTES), media_type="image/png")
_JPEG_BYTES = b"\xff\xd8\xff\xe0jpeg-bytes-not-real-but-fine"
_JPEG_REF = ContentRef(
    hash="sha256:jpeg", size=len(_JPEG_BYTES), media_type="image/jpeg"
)

# An image-bearing request must target a VISION MODEL, or the vision guard
# raises FatalError before wire assembly.
# gpt-5.4-2026-03-05 is registered in the catalog as a vision + reasoning model, so all image wire
# tests use it (the inline-translation assertions only run after the guard passes).
_VISION_MODEL = "gpt-5.4-2026-03-05"


def _fake_resolver(known: dict[ContentRef, bytes]):
    """Build a narrow resolver: look up bytes by ContentRef (simulating content_store.get).

    Note the provider only receives this Callable; it does NOT hold a ContentStore, keeping it pure
    (red line).
    """

    def _resolve(ref: ContentRef) -> bytes:
        return known[ref]

    return _resolve


def _data_uri(media_type: str, body: bytes) -> str:
    return f"data:{media_type};base64,{_base64.b64encode(body).decode('ascii')}"


@respx.mock
def test_image_block_in_user_message_becomes_input_image_data_uri() -> None:
    """An ImageBlock in a user turn → one input_image segment in that message's content array,
    where image_url is data:<media>;base64,<…>, media_type comes from the ContentRef, and the
    bytes are derefed via the injected resolver and base64-encoded."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(
        image_resolver=_fake_resolver({_PNG_REF: _PNG_BYTES})
    )
    request = _basic_request(
        model=_VISION_MODEL,
        messages=[
            Message(
                role="user",
                content=[
                    TextBlock(text="what is this?"),
                    ImageBlock(source=_PNG_REF),
                ],
            ),
        ],
    )
    provider.complete(request)

    body = json.loads(route.calls[0].request.content)
    assert body["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "what is this?"},
                {
                    "type": "input_image",
                    "image_url": _data_uri("image/png", _PNG_BYTES),
                },
            ],
        }
    ]


@respx.mock
def test_image_data_uri_prefix_and_base64_payload_correct() -> None:
    """Assert both the data URI prefix data:<media>;base64, and the base64 content are correct."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(
        image_resolver=_fake_resolver({_JPEG_REF: _JPEG_BYTES})
    )
    request = _basic_request(
        model=_VISION_MODEL,
        messages=[Message(role="user", content=[ImageBlock(source=_JPEG_REF)])],
    )
    provider.complete(request)

    body = json.loads(route.calls[0].request.content)
    image_seg = body["input"][0]["content"][0]
    assert image_seg["type"] == "input_image"
    url = image_seg["image_url"]
    assert url.startswith("data:image/jpeg;base64,")
    payload_b64 = url.split(",", 1)[1]
    assert _base64.b64decode(payload_b64) == _JPEG_BYTES


@respx.mock
def test_image_block_in_historical_non_last_message_is_inlined() -> None:
    """The inline primitive walks ANY message position: an ImageBlock in history (not the last
    turn) is also inlined, paving the way at zero cost for future pull (a tool returning images)."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(
        image_resolver=_fake_resolver({_PNG_REF: _PNG_BYTES})
    )
    request = _basic_request(
        model=_VISION_MODEL,
        messages=[
            # First user turn in history with an image (not the last message).
            Message(
                role="user",
                content=[TextBlock(text="look at this"), ImageBlock(source=_PNG_REF)],
            ),
            Message(role="assistant", content=[TextBlock(text="got it")]),
            # Last user turn, plain text.
            _user_message("one more question"),
        ],
    )
    provider.complete(request)

    body = json.loads(route.calls[0].request.content)
    # The image in the first historical user turn is inlined.
    assert body["input"][0]["content"] == [
        {"type": "input_text", "text": "look at this"},
        {
            "type": "input_image",
            "image_url": _data_uri("image/png", _PNG_BYTES),
        },
    ]
    # The last plain-text user turn stays a single input_text (bytes unchanged).
    assert body["input"][2] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "one more question"}],
    }


@respx.mock
def test_image_block_in_assistant_message_is_inlined() -> None:
    """An ImageBlock in an assistant turn also goes through the inline primitive (general
    push/pull, not bound to user turns), paving the way for images returned by a future
    image-reading tool down the assistant/tool path."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(
        image_resolver=_fake_resolver({_PNG_REF: _PNG_BYTES})
    )
    request = _basic_request(
        model=_VISION_MODEL,
        messages=[
            Message(
                role="assistant",
                content=[
                    TextBlock(text="here is the image I generated"),
                    ImageBlock(source=_PNG_REF),
                ],
            ),
        ],
    )
    provider.complete(request)

    body = json.loads(route.calls[0].request.content)
    msg_item = body["input"][0]
    assert msg_item["role"] == "assistant"
    assert msg_item["content"] == [
        {"type": "output_text", "text": "here is the image I generated"},
        {
            "type": "input_image",
            "image_url": _data_uri("image/png", _PNG_BYTES),
        },
    ]


@respx.mock
def test_multiple_images_in_one_message_all_inlined_in_order() -> None:
    """Multiple images in one message are all inlined in block order."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(
        image_resolver=_fake_resolver(
            {_PNG_REF: _PNG_BYTES, _JPEG_REF: _JPEG_BYTES}
        )
    )
    request = _basic_request(
        model=_VISION_MODEL,
        messages=[
            Message(
                role="user",
                content=[
                    ImageBlock(source=_PNG_REF),
                    TextBlock(text="compare these two"),
                    ImageBlock(source=_JPEG_REF),
                ],
            ),
        ],
    )
    provider.complete(request)

    body = json.loads(route.calls[0].request.content)
    assert body["input"][0]["content"] == [
        {
            "type": "input_image",
            "image_url": _data_uri("image/png", _PNG_BYTES),
        },
        {"type": "input_text", "text": "compare these two"},
        {
            "type": "input_image",
            "image_url": _data_uri("image/jpeg", _JPEG_BYTES),
        },
    ]


@respx.mock
def test_image_block_without_resolver_raises_clear_error() -> None:
    """Request contains an ImageBlock but image_resolver is None → a clear error (missing config
    must be loud, never silently drop the image)."""
    provider = _make_provider()  # image_resolver defaults to None
    request = _basic_request(
        # Use a vision model so the vision guard passes, letting the request reach wire assembly and
        # trip the missing-resolver error (guard first, missing resolver second; this tests the latter).
        model=_VISION_MODEL,
        messages=[Message(role="user", content=[ImageBlock(source=_PNG_REF)])],
    )
    with pytest.raises(ValueError, match="image_resolver"):
        provider.complete(request)


def test_provider_holds_only_resolver_not_content_store() -> None:
    """The provider is pure (red line): it holds only the narrow resolver Callable, NOT a
    ContentStore / StepContext."""
    resolver = _fake_resolver({_PNG_REF: _PNG_BYTES})
    provider = _make_provider(image_resolver=resolver)
    # It holds exactly the injected resolver; no content store on any other instance attribute.
    assert provider._image_resolver is resolver
    for name, value in vars(provider).items():
        type_name = type(value).__name__
        assert "ContentStore" not in type_name, (
            f"provider must not hold a ContentStore, but {name} is {type_name}"
        )
        assert "StepContext" not in type_name, (
            f"provider must not hold a StepContext, but {name} is {type_name}"
        )


@respx.mock
def test_text_only_message_unchanged_when_resolver_present() -> None:
    """Resolver present but no image in the message → the plain-text path's bytes are unchanged
    (a single input_text; red line: the old serialization is untouched)."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(
        image_resolver=_fake_resolver({_PNG_REF: _PNG_BYTES})
    )
    provider.complete(_basic_request(text="plain text"))

    body = json.loads(route.calls[0].request.content)
    assert body["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "plain text"}],
        }
    ]


@respx.mock
def test_image_bytes_not_written_back_resolver_called_per_request() -> None:
    """Red-line check: base64 appears only once in the wire body, while the account side stays a
    ContentRef (the ImageBlock is not rewritten); the resolver is called to deref on each wire
    assembly, and the ledger never caches base64. Reusing the same request twice and seeing the
    resolver called each time is indirect evidence that the deref is transient and not written back."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    calls: list[ContentRef] = []

    def _counting_resolver(ref: ContentRef) -> bytes:
        calls.append(ref)
        return _PNG_BYTES

    provider = _make_provider(image_resolver=_counting_resolver)
    image_block = ImageBlock(source=_PNG_REF)
    request = _basic_request(
        model=_VISION_MODEL,
        messages=[Message(role="user", content=[image_block])],
    )
    provider.complete(request)
    provider.complete(request)

    # Each of the two requests derefs once (no caching written back to the ledger/ContentStore).
    assert calls == [_PNG_REF, _PNG_REF]
    # The account-side ImageBlock still carries only a ContentRef, not rewritten into a base64 form.
    assert request.messages[0].content[0] == ImageBlock(source=_PNG_REF)
    assert request.messages[0].content[0].source is _PNG_REF


# ---------------------------------------------------------------------------
# 15. Vision capability guard
# ---------------------------------------------------------------------------
#
# A safety net once ImageBlock joined the union type: a request with an image but a non-vision
# target model → FatalError before going on the wire, so we don't blindly send images to a model
# that can't read them. The guard looks up request.model in catalog.CATALOG (after alias
# resolution); a missing model or supports_vision False counts as non-vision. The guard runs BEFORE
# wire assembly: a non-vision model with an image shouldn't emit even one HTTP request.


@respx.mock
def test_image_with_non_vision_model_raises_fatal_before_request() -> None:
    """ImageBlock + non-vision model (gpt-4o, catalog supports_vision False) → FatalError, and
    NOT a single HTTP request is sent (the guard runs before wire assembly / POST)."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(
        image_resolver=_fake_resolver({_PNG_REF: _PNG_BYTES})
    )
    request = _basic_request(
        model="gpt-4o",  # supports_vision=False in the catalog
        messages=[Message(role="user", content=[ImageBlock(source=_PNG_REF)])],
    )
    with pytest.raises(FatalError, match="vision"):
        provider.complete(request)
    # The guard runs before wire assembly / POST, so the gateway is never hit.
    assert not route.called


@respx.mock
def test_image_with_unregistered_model_raises_fatal() -> None:
    """Model not in the catalog (unregistered) → conservatively treated as non-vision; an image means FatalError, no request sent."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(
        image_resolver=_fake_resolver({_PNG_REF: _PNG_BYTES})
    )
    request = _basic_request(
        model="totally-unknown-model",
        messages=[Message(role="user", content=[ImageBlock(source=_PNG_REF)])],
    )
    with pytest.raises(FatalError, match="vision"):
        provider.complete(request)
    assert not route.called


@respx.mock
def test_image_in_historical_message_also_triggers_guard() -> None:
    """The guard walks ImageBlocks at ANY position: an image in a historical turn (not the last)
    with a non-vision model → FatalError just the same."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(
        image_resolver=_fake_resolver({_PNG_REF: _PNG_BYTES})
    )
    request = _basic_request(
        model="gpt-4o",
        messages=[
            Message(role="user", content=[ImageBlock(source=_PNG_REF)]),
            Message(role="assistant", content=[TextBlock(text="got it")]),
            _user_message("another question"),
        ],
    )
    with pytest.raises(FatalError, match="vision"):
        provider.complete(request)
    assert not route.called


@respx.mock
def test_image_with_vision_model_passes_guard_and_sends() -> None:
    """ImageBlock + vision model (gpt-5.4-2026-03-05, catalog supports_vision True) → the guard
    passes, the request assembles and sends normally (image inlined into input_image)."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(
        image_resolver=_fake_resolver({_PNG_REF: _PNG_BYTES})
    )
    request = _basic_request(
        model=_VISION_MODEL,
        messages=[Message(role="user", content=[ImageBlock(source=_PNG_REF)])],
    )
    response = provider.complete(request)

    assert route.called
    assert isinstance(response, LLMResponse)
    body = json.loads(route.calls[0].request.content)
    assert body["input"][0]["content"][0]["type"] == "input_image"


@respx.mock
def test_text_only_request_with_non_vision_model_passes_guard() -> None:
    """The guard checks the catalog only when an image is actually present: a plain-text request +
    non-vision model passes as usual (the old path is unaffected, zero overhead)."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider()
    provider.complete(_basic_request(model="gpt-4o", text="plain text, no image"))
    assert route.called


@respx.mock
def test_text_only_request_with_unregistered_model_passes_guard() -> None:
    """Plain text + unregistered model also passes as usual: the guard doesn't check the catalog
    for plain-text requests (many existing text/tool tests use gpt-5.4, which is outside the
    catalog, and must not be caught by the guard)."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider()
    provider.complete(_basic_request(model="gpt-5.4", text="plain text"))
    assert route.called


# ---------------------------------------------------------------------------
# 16. Tool-result images: ToolResultBlock.images → function_call_output array
# ---------------------------------------------------------------------------
#
# The read tool reading a .png surfaces the image on ToolResultBlock.images. When the bound model is
# vision-capable, the provider deref→base64-inlines each image and turns the function_call_output's
# output from a plain string into a content-part array: [{input_text}, {input_image}, ...] (probed
# against a real gateway: HTTP 200 + the model actually sees the image). A non-vision model (or a
# missing resolver) degrades to the plain string output with a note appended — never crashing. A
# tool-result image rides inside ToolResultBlock.images, NOT a top-level ImageBlock, so it is
# invisible to _guard_vision_capability; the degrade is the dedicated gate.


def _tool_message_with_image(
    *, call_id: str = "call_img", output: str = "read /tmp/pic.png (image, 70 bytes)"
) -> Message:
    return Message(
        role="tool",
        content=[
            ToolResultBlock(
                call_id=call_id,
                output=output,
                success=True,
                images=[ImageBlock(source=_PNG_REF)],
            )
        ],
    )


@respx.mock
def test_tool_result_image_with_vision_model_becomes_input_image_array() -> None:
    """A ToolResultBlock carrying an image + a vision model → the function_call_output's output is a
    content-part ARRAY: an input_text (the rendered string) followed by an input_image data URI,
    whose image_url starts with data:<media>;base64, and whose bytes deref via the resolver."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(image_resolver=_fake_resolver({_PNG_REF: _PNG_BYTES}))
    request = _basic_request(
        model=_VISION_MODEL,
        messages=[_tool_message_with_image()],
    )
    provider.complete(request)

    body = json.loads(route.calls[0].request.content)
    assert body["input"] == [
        {
            "type": "function_call_output",
            "call_id": "call_img",
            "output": [
                {"type": "input_text", "text": "read /tmp/pic.png (image, 70 bytes)"},
                {
                    "type": "input_image",
                    "image_url": _data_uri("image/png", _PNG_BYTES),
                },
            ],
        }
    ]
    image_seg = body["input"][0]["output"][1]
    assert image_seg["image_url"].startswith("data:image/png;base64,")


@respx.mock
def test_tool_result_multiple_images_all_inlined_after_text() -> None:
    """Multiple images on one ToolResultBlock → all inlined as input_image segments, in order,
    following the single leading input_text segment."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(
        image_resolver=_fake_resolver({_PNG_REF: _PNG_BYTES, _JPEG_REF: _JPEG_BYTES})
    )
    request = _basic_request(
        model=_VISION_MODEL,
        messages=[
            Message(
                role="tool",
                content=[
                    ToolResultBlock(
                        call_id="call_two",
                        output="two images",
                        success=True,
                        images=[
                            ImageBlock(source=_PNG_REF),
                            ImageBlock(source=_JPEG_REF),
                        ],
                    )
                ],
            )
        ],
    )
    provider.complete(request)

    body = json.loads(route.calls[0].request.content)
    assert body["input"][0]["output"] == [
        {"type": "input_text", "text": "two images"},
        {"type": "input_image", "image_url": _data_uri("image/png", _PNG_BYTES)},
        {"type": "input_image", "image_url": _data_uri("image/jpeg", _JPEG_BYTES)},
    ]


@respx.mock
def test_tool_result_image_with_non_vision_model_degrades_to_string() -> None:
    """A ToolResultBlock with an image + a non-vision model (gpt-4o, supports_vision False) → the
    output stays a plain STRING with a degrade note appended; no crash, and the image is dropped.
    The vision guard does NOT fire (a tool-result image is not a top-level ImageBlock)."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(image_resolver=_fake_resolver({_PNG_REF: _PNG_BYTES}))
    request = _basic_request(
        model="gpt-4o",  # supports_vision=False in the catalog
        messages=[_tool_message_with_image(output="read /tmp/pic.png")],
    )
    provider.complete(request)

    body = json.loads(route.calls[0].request.content)
    output = body["input"][0]["output"]
    assert isinstance(output, str)
    assert output == "read /tmp/pic.png\n[image omitted: model is not vision-capable]"
    assert route.called


@respx.mock
def test_tool_result_image_without_resolver_degrades_to_string() -> None:
    """Image present + vision model but NO image_resolver configured → degrade to the plain string
    output with the note appended (never crash on a missing resolver)."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider()  # image_resolver defaults to None
    request = _basic_request(
        model=_VISION_MODEL,
        messages=[_tool_message_with_image(output="read /tmp/pic.png")],
    )
    provider.complete(request)

    body = json.loads(route.calls[0].request.content)
    output = body["input"][0]["output"]
    assert isinstance(output, str)
    assert output == "read /tmp/pic.png\n[image omitted: model is not vision-capable]"


@respx.mock
def test_tool_result_without_images_output_unchanged_string() -> None:
    """Regression: a ToolResultBlock with NO images keeps the plain string output, byte-for-byte
    (the text-only tool path is untouched even with a vision model + resolver present)."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_responses_payload(texts=["ok"]))
    )
    provider = _make_provider(image_resolver=_fake_resolver({_PNG_REF: _PNG_BYTES}))
    request = _basic_request(
        model=_VISION_MODEL,
        messages=[
            Message(
                role="tool",
                content=[
                    ToolResultBlock(
                        call_id="call_text", output="sunny, 22C", success=True
                    )
                ],
            )
        ],
    )
    provider.complete(request)

    body = json.loads(route.calls[0].request.content)
    assert body["input"] == [
        {
            "type": "function_call_output",
            "call_id": "call_text",
            "output": "sunny, 22C",
        }
    ]
