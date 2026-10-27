"""Wire translation for :class:`noeta.backends.providers.openai_compat.OpenAICompatProvider`.

Every rule that maps the neutral message types onto the Chat Completions wire
— and every failure the adapter must translate into the neutral error
taxonomy — gets its own case, because a silent mistranslation here corrupts
whatever the model sees without ever raising. All HTTP traffic is mocked via
``respx``; the adapter always streams, so each batch-shaped fixture is served
as the chunk stream that rebuilds it (:func:`tests.providers.sse.chat_sse`).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from noeta.backends.providers.openai_compat import OpenAICompatProvider
from noeta.core import models
from noeta.core.errors import (
    ContextOverflow,
    Fatal,
    MalformedToolArguments,
    RateLimited,
    Transient,
)
from noeta.core.types import (
    Effort,
    Message,
    ModelRequest,
    ModelResponse,
    SystemPart,
    Text,
    Thinking,
    ToolSpec,
    ToolUse,
    Usage,
)
from tests.backends.providers.sse import chat_sse, sse_response

BASE_URL = "https://example.test/v1"
CHAT_ENDPOINT = f"{BASE_URL}/chat/completions"


@pytest.fixture(autouse=True)
def _clean_catalog_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "_EXTENSIONS", {})
    monkeypatch.setattr(models, "_EXTENSION_ALIASES", {})
    monkeypatch.setattr(models, "_WARNED", set())


def _make_provider(**overrides: Any) -> OpenAICompatProvider:
    kwargs: dict[str, Any] = {"base_url": BASE_URL, "api_key": "sk-test"}
    kwargs.update(overrides)
    return OpenAICompatProvider(**kwargs)


def _user(text: str, origin: Any = None) -> Message:
    return Message(role="user", content=[Text(text=text)], origin=origin)


def _request(
    *,
    model: str = "gpt-4o",
    text: str = "hi",
    system: list[SystemPart] | None = None,
    messages: list[Message] | None = None,
    tools: list[ToolSpec] | None = None,
    max_tokens: int | None = None,
    output_schema: dict[str, Any] | None = None,
    thinking: bool = True,
    effort: Effort | None = None,
    tool_choice: str | None = None,
    headers: dict[str, str] | None = None,
) -> ModelRequest:
    return ModelRequest(
        model=model,
        messages=messages if messages is not None else [_user(text)],
        tools=tools or [],
        system=system or [],
        max_tokens=max_tokens,
        output_schema=output_schema,
        thinking=thinking,
        effort=effort,
        tool_choice=tool_choice,
        headers=headers or {},
    )


def _chat_response(
    *,
    content: str | None = "ok",
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    reasoning_content: str | None = None,
    reasoning: str | None = None,
    encrypted_reasoning: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    if reasoning is not None:
        message["reasoning"] = reasoning
    if encrypted_reasoning is not None:
        message["encrypted_reasoning"] = encrypted_reasoning
    return {
        "id": "chatcmpl-xyz",
        "object": "chat.completion",
        "model": "gpt-4o",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage
        if usage is not None
        else {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _ok(payload: dict[str, Any] | None = None) -> httpx.Response:
    return sse_response(chat_sse(payload if payload is not None else _chat_response()))


def _mock(payload: dict[str, Any] | None = None) -> respx.Route:
    # A fresh response per call, so one route can serve several requests.
    return respx.post(CHAT_ENDPOINT).mock(side_effect=lambda _request: _ok(payload))


def _sent(route: respx.Route, index: int = -1) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(route.calls[index].request.content)
    return body


async def _complete(payload: dict[str, Any], request: ModelRequest | None = None) -> ModelResponse:
    _mock(payload)
    return await _make_provider().complete(request or _request())


def _call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


# ---------------------------------------------------------------------------
# Plain text and usage
# ---------------------------------------------------------------------------


@respx.mock
async def test_plain_text_response_maps_to_end_turn_text() -> None:
    route = _mock(_chat_response(content="hello"))
    response = await _make_provider().complete(_request(text="say hi"))
    assert route.called
    assert response.stop_reason == "end_turn"
    assert response.message.role == "assistant"
    assert response.message.content == [Text(text="hello")]
    # total_tokens is derivable from the other two, so the mapping ignores it.
    assert response.usage == Usage(uncached=1, output=1)
    assert response.usage.input == 1
    assert response.model == "gpt-4o"


@respx.mock
async def test_reasoning_tokens_from_completion_tokens_details() -> None:
    response = await _complete(
        _chat_response(
            usage={
                "prompt_tokens": 12,
                "completion_tokens": 40,
                "total_tokens": 52,
                "completion_tokens_details": {"reasoning_tokens": 30},
            }
        )
    )
    assert response.usage == Usage(uncached=12, output=40, reasoning=30)


@respx.mock
async def test_usage_maps_cached_tokens_to_cache_read() -> None:
    response = await _complete(
        _chat_response(
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "total_tokens": 140,
                "prompt_tokens_details": {"cached_tokens": 30},
                "completion_tokens_details": {"reasoning_tokens": 15},
            }
        )
    )
    # uncached = prompt_tokens - cached_tokens = 100 - 30 = 70
    assert response.usage == Usage(uncached=70, cache_read=30, output=40, reasoning=15)
    assert response.usage.input == 100


@respx.mock
async def test_empty_usage_yields_empty_usage() -> None:
    response = await _complete(_chat_response(usage={}))
    assert response.usage == Usage()


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


@respx.mock
async def test_single_tool_call_maps_to_tool_use() -> None:
    response = await _complete(
        _chat_response(
            content=None,
            tool_calls=[_call("call_1", "echo", json.dumps({"text": "hi"}))],
            finish_reason="tool_calls",
        )
    )
    assert response.stop_reason == "tool_use"
    assert response.message.content == [ToolUse(id="call_1", name="echo", input={"text": "hi"})]


@respx.mock
async def test_multiple_tool_calls_each_become_their_own_block() -> None:
    response = await _complete(
        _chat_response(
            content=None,
            tool_calls=[_call(f"call_{i}", "echo", json.dumps({"i": i})) for i in range(3)],
            finish_reason="tool_calls",
        )
    )
    assert response.stop_reason == "tool_use"
    assert response.message.content == [
        ToolUse(id=f"call_{i}", name="echo", input={"i": i}) for i in range(3)
    ]


@respx.mock
async def test_text_and_tool_call_both_appear_in_content() -> None:
    response = await _complete(
        _chat_response(
            content="calling the tool first",
            tool_calls=[_call("call_a", "echo", "{}")],
            finish_reason="tool_calls",
        )
    )
    assert response.stop_reason == "tool_use"
    assert response.message.content == [
        Text(text="calling the tool first"),
        ToolUse(id="call_a", name="echo", input={}),
    ]


@respx.mock
async def test_stop_with_tool_calls_is_a_tool_use_stop() -> None:
    """Gemini's OpenAI endpoint and older Ollama report "stop" on a response
    that calls tools; the calls are what the model asked for."""
    response = await _complete(
        _chat_response(
            content=None, tool_calls=[_call("call_1", "echo", "{}")], finish_reason="stop"
        )
    )
    assert response.stop_reason == "tool_use"
    assert response.message.content == [ToolUse(id="call_1", name="echo", input={})]


@pytest.mark.parametrize("raw", ["null", "", "  "])
@respx.mock
async def test_null_or_empty_tool_arguments_decode_as_no_arguments(raw: str) -> None:
    response = await _complete(
        _chat_response(content=None, tool_calls=[_call("c", "echo", raw)], finish_reason="tool_calls")
    )
    assert response.message.content == [ToolUse(id="c", name="echo", input={})]


@pytest.mark.parametrize("raw", ["[1, 2]", '"text"', "3"])
@respx.mock
async def test_non_object_tool_arguments_are_malformed(raw: str) -> None:
    with pytest.raises(MalformedToolArguments, match="must be a JSON object"):
        await _complete(
            _chat_response(content=None, tool_calls=[_call("c", "echo", raw)], finish_reason="tool_calls")
        )


@respx.mock
async def test_malformed_tool_call_arguments_raise_malformed() -> None:
    with pytest.raises(MalformedToolArguments, match="tool_call arguments") as excinfo:
        await _complete(
            _chat_response(
                content=None, tool_calls=[_call("call_x", "echo", "{not-json")], finish_reason="tool_calls"
            )
        )
    # A stream cut mid-call: retried like any transient failure (as in 0.6).
    assert isinstance(excinfo.value, Transient)


@respx.mock
async def test_finish_tool_calls_with_no_calls_is_fatal() -> None:
    with pytest.raises(Fatal, match="inconsistent OpenAI response"):
        await _complete(_chat_response(content="hi", tool_calls=[], finish_reason="tool_calls"))


@respx.mock
async def test_finish_reason_length_maps_to_max_tokens() -> None:
    response = await _complete(_chat_response(content="part", finish_reason="length"))
    assert response.stop_reason == "max_tokens"
    assert response.message.content == [Text(text="part")]


@respx.mock
async def test_content_filter_maps_to_refusal_and_drops_calls() -> None:
    response = await _complete(
        _chat_response(
            content="I can't help with that",
            tool_calls=[_call("c", "echo", "{}")],
            finish_reason="content_filter",
        )
    )
    assert response.stop_reason == "refusal"
    assert response.message.content == [Text(text="I can't help with that")]


@respx.mock
async def test_unknown_finish_reason_is_fatal() -> None:
    with pytest.raises(Fatal, match="unsupported finish_reason"):
        await _complete(_chat_response(finish_reason="weird"))


# ---------------------------------------------------------------------------
# HTTP errors
# ---------------------------------------------------------------------------


@respx.mock
async def test_401_translates_to_fatal() -> None:
    respx.post(CHAT_ENDPOINT).mock(return_value=httpx.Response(401, json={"error": "invalid_api_key"}))
    with pytest.raises(Fatal) as ei:
        await _make_provider().complete(_request())
    assert ei.value.status == 401


@respx.mock
async def test_429_with_retry_after_maps_to_rate_limited() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(429, json={"error": "rate_limited"}, headers={"Retry-After": "3"})
    )
    with pytest.raises(RateLimited) as ex:
        await _make_provider().complete(_request())
    assert ex.value.retry_after == 3.0


@respx.mock
async def test_429_without_retry_after_has_no_hint() -> None:
    respx.post(CHAT_ENDPOINT).mock(return_value=httpx.Response(429, json={"error": "rate_limited"}))
    with pytest.raises(RateLimited) as ex:
        await _make_provider().complete(_request())
    assert ex.value.retry_after is None


@pytest.mark.parametrize("status", [500, 503])
@respx.mock
async def test_5xx_maps_to_transient(status: int) -> None:
    respx.post(CHAT_ENDPOINT).mock(return_value=httpx.Response(status, json={"error": "boom"}))
    with pytest.raises(Transient):
        await _make_provider().complete(_request())


@respx.mock
async def test_400_context_length_exceeded_maps_to_overflow() -> None:
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
    with pytest.raises(ContextOverflow):
        await _make_provider().complete(_request())


@respx.mock
async def test_413_maps_to_overflow() -> None:
    respx.post(CHAT_ENDPOINT).mock(return_value=httpx.Response(413, text="Request Entity Too Large"))
    with pytest.raises(ContextOverflow):
        await _make_provider().complete(_request())


@respx.mock
async def test_400_plain_invalid_request_maps_to_fatal() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "missing field", "type": "invalid_request_error"}}
        )
    )
    with pytest.raises(Fatal, match="missing field"):
        await _make_provider().complete(_request())


@respx.mock
async def test_fatal_http_error_carries_the_json_error_message() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(
            404, json={"error": {"type": "not_found_error", "message": "model: nope-1 not found"}}
        )
    )
    with pytest.raises(Fatal) as info:
        await _make_provider().complete(_request())
    assert str(info.value).endswith(": model: nope-1 not found")
    assert "404" in str(info.value)


@respx.mock
async def test_transient_http_error_carries_a_capped_raw_body() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(503, text="upstream gateway melted " + "x" * 2000)
    )
    with pytest.raises(Transient) as info:
        await _make_provider().complete(_request())
    assert "upstream gateway melted" in str(info.value)
    assert "x" * 450 in str(info.value)
    assert "x" * 500 not in str(info.value)


@respx.mock
async def test_connect_error_maps_to_transient() -> None:
    respx.post(CHAT_ENDPOINT).mock(side_effect=httpx.ConnectError("connection refused"))
    with pytest.raises(Transient):
        await _make_provider().complete(_request())


@respx.mock
async def test_timeout_maps_to_transient() -> None:
    respx.post(CHAT_ENDPOINT).mock(side_effect=httpx.ReadTimeout("timed out"))
    with pytest.raises(Transient):
        await _make_provider().complete(_request())


def test_default_timeout_is_300_seconds() -> None:
    # The scalar measures silence between SSE events: a reasoning model can
    # think for minutes before its first token.
    provider = _make_provider()
    assert provider._client.timeout.read == 300.0
    assert provider._client.timeout.connect == 300.0


def test_explicit_timeout_is_respected() -> None:
    provider = _make_provider(timeout_seconds=12.5)
    assert provider._client.timeout.read == 12.5


def test_missing_api_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAICompatProvider(BASE_URL)
    # An empty key counts as absent.
    with pytest.raises(ValueError, match="API key"):
        OpenAICompatProvider(BASE_URL, api_key="")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert OpenAICompatProvider(BASE_URL)._client.headers["authorization"] == "Bearer sk-env"


# ---------------------------------------------------------------------------
# Reasoning fields → Thinking
# ---------------------------------------------------------------------------


@respx.mock
async def test_reasoning_content_becomes_thinking_before_text() -> None:
    response = await _complete(
        _chat_response(content="here is the answer", reasoning_content="let me think first...")
    )
    assert response.message.content == [
        Thinking(text="let me think first...", family="openai_compat"),
        Text(text="here is the answer"),
    ]


@respx.mock
async def test_reasoning_with_encrypted_signature_populates_thinking() -> None:
    response = await _complete(
        _chat_response(
            content=None,
            tool_calls=[_call("call_z", "echo", "{}")],
            finish_reason="tool_calls",
            reasoning="thinking out loud",
            encrypted_reasoning="abc==",
        )
    )
    assert response.message.content == [
        Thinking(text="thinking out loud", signature="abc==", family="openai_compat"),
        ToolUse(id="call_z", name="echo", input={}),
    ]


@respx.mock
async def test_encrypted_reasoning_only_yields_signature_thinking() -> None:
    response = await _complete(_chat_response(content="answer", encrypted_reasoning="opaque-token=="))
    assert response.message.content == [
        Thinking(text="", signature="opaque-token==", family="openai_compat"),
        Text(text="answer"),
    ]


@respx.mock
async def test_no_reasoning_fields_produces_no_thinking() -> None:
    response = await _complete(_chat_response(content="plain"))
    assert all(not isinstance(b, Thinking) for b in response.message.content)
