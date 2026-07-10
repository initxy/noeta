"""Test matrix for :class:`noeta.providers.openai_compat.OpenAICompatProvider`.

Each OpenAICompatProvider translation rule gets a dedicated case. All HTTP
traffic is mocked via ``respx``, so the suite makes zero real network calls.
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
from noeta.providers.openai_compat import OpenAICompatProvider


BASE_URL = "https://example.test/v1"
CHAT_ENDPOINT = f"{BASE_URL}/chat/completions"


def _make_provider(**overrides: Any) -> OpenAICompatProvider:
    kwargs: dict[str, Any] = {
        "base_url": BASE_URL,
        "api_key": "sk-test",
    }
    kwargs.update(overrides)
    return OpenAICompatProvider(**kwargs)


def _user_message(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)])


def _basic_request(
    *,
    model: str = "gpt-4o",
    text: str = "hi",
    system: Message | None = None,
    messages: list[Message] | None = None,
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    output_schema: dict[str, Any] | None = None,
    thinking: str | None = None,
    effort: str | None = None,
) -> LLMRequest:
    return LLMRequest(
        model=model,
        messages=messages if messages is not None else [_user_message(text)],
        tools=tools or [],
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        output_schema=output_schema,
        thinking=thinking,
        effort=effort,
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
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage
        or {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


# ---------------------------------------------------------------------------
# 1. Plain text response (finish_reason=stop)
# ---------------------------------------------------------------------------


@respx.mock
def test_plain_text_response_maps_to_end_turn_textblock() -> None:
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_chat_response(content="hello"))
    )

    provider = _make_provider()
    response = provider.complete(_basic_request(text="say hi"))

    assert route.called
    assert isinstance(response, LLMResponse)
    assert response.stop_reason == "end_turn"
    assert response.content == [TextBlock(text="hello")]
    # total_tokens is a redundant provider field — dropped, not pinned.
    assert response.usage == Usage(uncached=1, output=1)
    assert response.usage.input == 1
    assert response.raw is not None and response.raw["id"] == "chatcmpl-xyz"


@respx.mock
def test_reasoning_tokens_from_completion_tokens_details() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_chat_response(
                usage={
                    "prompt_tokens": 12,
                    "completion_tokens": 40,
                    "total_tokens": 52,
                    "completion_tokens_details": {"reasoning_tokens": 30},
                }
            ),
        )
    )
    provider = _make_provider()
    response = provider.complete(_basic_request(text="think"))
    assert response.usage == Usage(uncached=12, output=40, reasoning_tokens=30)
    # Hidden chain-of-thought does not count against the visible answer.
    assert response.usage.visible_output == 10


@respx.mock
def test_usage_maps_with_cached_tokens_to_cache_read() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_chat_response(
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 40,
                    "total_tokens": 140,
                    "prompt_tokens_details": {"cached_tokens": 30},
                    "completion_tokens_details": {"reasoning_tokens": 15},
                }
            ),
        )
    )
    provider = _make_provider()
    response = provider.complete(_basic_request(text="cached"))
    # uncached = prompt_tokens - cached_tokens = 100 - 30 = 70
    assert response.usage == Usage(
        uncached=70,
        cache_read=30,
        cache_write=0,
        output=40,
        reasoning_tokens=15,
    )
    assert response.usage.input == 100


@respx.mock
def test_empty_usage_yields_empty_usage() -> None:
    # Build the body directly: the _chat_response helper's ``usage or {...}``
    # would substitute the default for a falsy ``{}``, so we bypass it to
    # genuinely exercise the empty-usage path.
    body = _chat_response()
    body["usage"] = {}
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=body)
    )
    provider = _make_provider()
    response = provider.complete(_basic_request(text="hi"))
    assert response.usage == Usage()


# ---------------------------------------------------------------------------
# 2. Single tool_call → ToolUseBlock + stop_reason=tool_use
# ---------------------------------------------------------------------------


@respx.mock
def test_single_tool_call_maps_to_tool_use_block() -> None:
    payload = _chat_response(
        content=None,
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "echo",
                    "arguments": json.dumps({"text": "hi"}),
                },
            }
        ],
        finish_reason="tool_calls",
    )
    respx.post(CHAT_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    response = _make_provider().complete(_basic_request())

    assert response.stop_reason == "tool_use"
    assert response.content == [
        ToolUseBlock(call_id="call_1", tool_name="echo", arguments={"text": "hi"})
    ]


# ---------------------------------------------------------------------------
# 3. Multiple tool_calls
# ---------------------------------------------------------------------------


@respx.mock
def test_multiple_tool_calls_each_become_their_own_block() -> None:
    payload = _chat_response(
        content=None,
        tool_calls=[
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": "echo", "arguments": json.dumps({"i": i})},
            }
            for i in range(3)
        ],
        finish_reason="tool_calls",
    )
    respx.post(CHAT_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    response = _make_provider().complete(_basic_request())

    assert response.stop_reason == "tool_use"
    assert len(response.content) == 3
    for i, block in enumerate(response.content):
        assert isinstance(block, ToolUseBlock)
        assert block.call_id == f"call_{i}"
        assert block.tool_name == "echo"
        assert block.arguments == {"i": i}


# ---------------------------------------------------------------------------
# 4. text + tool_call mixed
# ---------------------------------------------------------------------------


@respx.mock
def test_text_and_tool_call_both_appear_in_content() -> None:
    payload = _chat_response(
        content="calling the tool first",
        tool_calls=[
            {
                "id": "call_a",
                "type": "function",
                "function": {"name": "echo", "arguments": "{}"},
            }
        ],
        finish_reason="tool_calls",
    )
    respx.post(CHAT_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    response = _make_provider().complete(_basic_request())

    assert response.stop_reason == "tool_use"
    assert response.content == [
        TextBlock(text="calling the tool first"),
        ToolUseBlock(call_id="call_a", tool_name="echo", arguments={}),
    ]


# ---------------------------------------------------------------------------
# 5. inconsistent state: finish_reason=stop with tool_calls
# ---------------------------------------------------------------------------


@respx.mock
def test_inconsistent_stop_with_tool_calls_raises_value_error() -> None:
    payload = _chat_response(
        content=None,
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "echo", "arguments": "{}"},
            }
        ],
        finish_reason="stop",
    )
    respx.post(CHAT_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    with pytest.raises(ValueError, match="inconsistent OpenAI response"):
        _make_provider().complete(_basic_request())


# ---------------------------------------------------------------------------
# 6. inconsistent state: finish_reason=tool_calls with empty tool_calls
# ---------------------------------------------------------------------------


@respx.mock
def test_inconsistent_tool_calls_with_empty_array_raises_value_error() -> None:
    payload = _chat_response(
        content="hi",
        tool_calls=[],
        finish_reason="tool_calls",
    )
    respx.post(CHAT_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    with pytest.raises(ValueError, match="inconsistent OpenAI response"):
        _make_provider().complete(_basic_request())


# ---------------------------------------------------------------------------
# 7. max_tokens truncation
# ---------------------------------------------------------------------------


@respx.mock
def test_finish_reason_length_maps_to_max_tokens() -> None:
    payload = _chat_response(content="part", finish_reason="length")
    respx.post(CHAT_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    response = _make_provider().complete(_basic_request())

    assert response.stop_reason == "max_tokens"
    assert response.content == [TextBlock(text="part")]


# ---------------------------------------------------------------------------
# 8. 401 → HTTPStatusError
# ---------------------------------------------------------------------------


@respx.mock
def test_401_translates_to_fatal_error() -> None:
    """② error recovery: a 401 is a non-retryable client error → FatalError.
    The adapter no longer leaks ``httpx.HTTPStatusError`` past its boundary;
    the runtime only ever sees the neutral class."""
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(401, json={"error": "invalid_api_key"})
    )

    with pytest.raises(FatalError):
        _make_provider().complete(_basic_request())


# ---------------------------------------------------------------------------
# 9. 429 → TransientError (with / without Retry-After)
# ---------------------------------------------------------------------------


@respx.mock
def test_429_with_retry_after_header_maps_to_transient() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(
            429,
            json={"error": "rate_limited"},
            headers={"Retry-After": "3"},
        )
    )

    with pytest.raises(TransientError) as ex:
        _make_provider().complete(_basic_request())
    assert ex.value.retry_after == 3.0


@respx.mock
def test_429_without_retry_after_maps_to_transient_none() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(429, json={"error": "rate_limited"})
    )

    with pytest.raises(TransientError) as ex:
        _make_provider().complete(_basic_request())
    assert ex.value.retry_after is None


# ---------------------------------------------------------------------------
# 10. 5xx → TransientError
# ---------------------------------------------------------------------------


@respx.mock
def test_500_maps_to_transient_error() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    with pytest.raises(TransientError):
        _make_provider().complete(_basic_request())


@respx.mock
def test_503_maps_to_transient_error() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(503, json={"error": "unavailable"})
    )

    with pytest.raises(TransientError):
        _make_provider().complete(_basic_request())


# ---------------------------------------------------------------------------
# 10b. 400 context_length_exceeded → ContextOverflowError
# ---------------------------------------------------------------------------


@respx.mock
def test_400_context_length_exceeded_maps_to_overflow() -> None:
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
        _make_provider().complete(_basic_request())


@respx.mock
def test_400_plain_invalid_request_maps_to_fatal() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "message": "missing field",
                    "type": "invalid_request_error",
                }
            },
        )
    )

    with pytest.raises(FatalError):
        _make_provider().complete(_basic_request())


# ---------------------------------------------------------------------------
# 11. Network error → TransientError
# ---------------------------------------------------------------------------


@respx.mock
def test_connect_error_maps_to_transient() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    with pytest.raises(TransientError):
        _make_provider().complete(_basic_request())


@respx.mock
def test_timeout_maps_to_transient() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    with pytest.raises(TransientError):
        _make_provider().complete(_basic_request())


# ---------------------------------------------------------------------------
# 12. Parse failure → ValueError
# ---------------------------------------------------------------------------


@respx.mock
def test_non_json_body_raises_value_error() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(
            200, content=b"not-json", headers={"content-type": "text/plain"}
        )
    )

    with pytest.raises(ValueError):
        _make_provider().complete(_basic_request())


@respx.mock
def test_missing_choices_raises_value_error() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"id": "x", "usage": {}})
    )

    with pytest.raises(ValueError, match="choices"):
        _make_provider().complete(_basic_request())


# ---------------------------------------------------------------------------
# 13. Reasoning model: reasoning_content present → ThinkingBlock first
# ---------------------------------------------------------------------------


@respx.mock
def test_reasoning_content_becomes_thinking_block_before_text() -> None:
    payload = _chat_response(
        content="here is the answer",
        reasoning_content="let me think first...",
    )
    respx.post(CHAT_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    response = _make_provider().complete(_basic_request())

    assert response.content == [
        ThinkingBlock(text="let me think first...", signature=None),
        TextBlock(text="here is the answer"),
    ]


# ---------------------------------------------------------------------------
# 14. Reasoning model: reasoning + encrypted_reasoning round-trip signature
# ---------------------------------------------------------------------------


@respx.mock
def test_reasoning_with_encrypted_signature_populates_thinking_block() -> None:
    payload = _chat_response(
        content=None,
        tool_calls=[
            {
                "id": "call_z",
                "type": "function",
                "function": {"name": "echo", "arguments": "{}"},
            }
        ],
        finish_reason="tool_calls",
        reasoning="thinking out loud",
        encrypted_reasoning="abc==",
    )
    respx.post(CHAT_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    response = _make_provider().complete(_basic_request())

    assert response.content == [
        ThinkingBlock(text="thinking out loud", signature="abc=="),
        ToolUseBlock(call_id="call_z", tool_name="echo", arguments={}),
    ]


# ---------------------------------------------------------------------------
# 15. No reasoning fields → no ThinkingBlock
# ---------------------------------------------------------------------------


@respx.mock
def test_no_reasoning_fields_produces_no_thinking_block() -> None:
    payload = _chat_response(content="plain")
    respx.post(CHAT_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    response = _make_provider().complete(_basic_request())

    assert all(not isinstance(b, ThinkingBlock) for b in response.content)


# ---------------------------------------------------------------------------
# 16. LLMRequest.system translation
# ---------------------------------------------------------------------------


@respx.mock
def test_system_field_is_prepended_to_outbound_messages() -> None:
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_chat_response())
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
    assert body["messages"][0] == {
        "role": "system",
        "content": "be terse\nanswer in en",
    }
    assert body["messages"][1] == {"role": "user", "content": "hello"}


# ---------------------------------------------------------------------------
# 17. system role inside messages → defensive ValueError
# ---------------------------------------------------------------------------


@respx.mock
def test_system_role_in_messages_array_raises_value_error() -> None:
    request = LLMRequest(
        model="gpt-4o",
        messages=[Message(role="system", content=[TextBlock(text="x")])],
    )

    with pytest.raises(ValueError, match="LLMRequest.system"):
        _make_provider().complete(request)


# ---------------------------------------------------------------------------
# 18. Same provider instance handles multiple models
# ---------------------------------------------------------------------------


@respx.mock
def test_same_provider_instance_routes_different_models() -> None:
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_chat_response())
    )

    provider = _make_provider()
    provider.complete(_basic_request(model="gpt-4o"))
    provider.complete(_basic_request(model="gpt-4o-mini"))

    assert route.call_count == 2
    body_a = json.loads(route.calls[0].request.content)
    body_b = json.loads(route.calls[1].request.content)
    assert body_a["model"] == "gpt-4o"
    assert body_b["model"] == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# 19. Outbound assistant ThinkingBlock round-trips to reasoning_content
# ---------------------------------------------------------------------------


@respx.mock
def test_outbound_thinking_block_round_trips_into_reasoning_fields() -> None:
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_chat_response())
    )

    assistant = Message(
        role="assistant",
        content=[
            ThinkingBlock(text="step1\n", signature="sig-1"),
            ThinkingBlock(text="step2", signature="sig-2"),
            TextBlock(text="so the answer is"),
            ToolUseBlock(
                call_id="call_x",
                tool_name="echo",
                arguments={"text": "hi"},
            ),
        ],
    )
    tool_msg = Message(
        role="tool",
        content=[
            ToolResultBlock(call_id="call_x", output="ok", success=True),
            ToolResultBlock(
                call_id="call_y", output="bad", success=False, error="boom"
            ),
        ],
    )
    provider_tool_schemas = [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "parameters": {"type": "object", "additionalProperties": True},
            },
        }
    ]
    request = LLMRequest(
        model="gpt-4o",
        messages=[_user_message("hi"), assistant, tool_msg],
        tools=provider_tool_schemas,
        temperature=0.2,
        max_tokens=512,
    )
    # ``chat`` mode opts the gateway into reasoning round-trip; the default
    # ``off`` is exercised by the next test.
    _make_provider(reasoning_continuation="chat").complete(request)

    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "gpt-4o"
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 512
    assert body["tools"] == provider_tool_schemas

    msgs = body["messages"]
    # Order: user, assistant, tool(call_x), tool(call_y)
    assert msgs[0] == {"role": "user", "content": "hi"}

    asst = msgs[1]
    assert asst["role"] == "assistant"
    assert asst["content"] == "so the answer is"
    assert asst["reasoning_content"] == "step1\n\nstep2"
    assert asst["encrypted_reasoning"] == "sig-2"
    assert asst["tool_calls"] == [
        {
            "id": "call_x",
            "type": "function",
            "function": {
                "name": "echo",
                "arguments": json.dumps({"text": "hi"}),
            },
        }
    ]

    assert msgs[2] == {"role": "tool", "tool_call_id": "call_x", "content": "ok"}
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_y", "content": "bad"}


@respx.mock
def test_default_reasoning_continuation_off_drops_outbound_reasoning() -> None:
    """Default ``reasoning_continuation="off"`` must NOT echo an assistant
    ThinkingBlock onto the wire: native OpenAI hides reasoning and
    DeepSeek-style gateways reject an echoed ``reasoning_content`` (HTTP 400).
    The ContextComposer carries thinking forward neutrally; this adapter is
    the gate that keeps it off OpenAI's wire unless explicitly opted in."""
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_chat_response())
    )

    assistant = Message(
        role="assistant",
        content=[
            ThinkingBlock(text="step1", signature="sig-1"),
            TextBlock(text="answer"),
        ],
    )
    request = LLMRequest(
        model="gpt-4o",
        messages=[_user_message("hi"), assistant],
    )
    # No override → default "off".
    _make_provider().complete(request)

    asst = json.loads(route.calls[0].request.content)["messages"][1]
    assert asst["role"] == "assistant"
    assert asst["content"] == "answer"
    assert "reasoning_content" not in asst
    assert "encrypted_reasoning" not in asst


# ---------------------------------------------------------------------------
# 20. Auth + extra headers wiring
# ---------------------------------------------------------------------------


@respx.mock
def test_authorization_and_extra_headers_sent() -> None:
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_chat_response())
    )

    provider = _make_provider(
        api_key="sk-secret",
        extra_headers={"X-Proxy": "vendor"},
    )
    provider.complete(_basic_request())

    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer sk-secret"
    assert req.headers["x-proxy"] == "vendor"


# ---------------------------------------------------------------------------
# 21. Non-dict JSON root raises ValueError
# ---------------------------------------------------------------------------


@respx.mock
def test_json_array_root_raises_value_error() -> None:
    respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=["not", "a", "dict"])
    )

    with pytest.raises(ValueError, match="JSON object"):
        _make_provider().complete(_basic_request())


# ---------------------------------------------------------------------------
# 22. Bad tool_call arguments JSON raises MalformedToolArgumentsError
# ---------------------------------------------------------------------------


@respx.mock
def test_malformed_tool_call_arguments_raises_value_error() -> None:
    # Still a ValueError (wording/type contract unchanged), additionally a
    # transient error so a truncated tool-call stream is retried by the runtime
    # rather than failing the task fatally.
    from noeta.protocols.errors import MalformedToolArgumentsError, TransientError

    payload = _chat_response(
        content=None,
        tool_calls=[
            {
                "id": "call_x",
                "type": "function",
                "function": {"name": "echo", "arguments": "{not-json"},
            }
        ],
        finish_reason="tool_calls",
    )
    respx.post(CHAT_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    with pytest.raises(ValueError, match="tool_call arguments") as excinfo:
        _make_provider().complete(_basic_request())
    assert isinstance(excinfo.value, MalformedToolArgumentsError)
    assert isinstance(excinfo.value, TransientError)
    assert excinfo.value.category == "transient"


# ---------------------------------------------------------------------------
# 23. tool_result with non-string output is JSON-encoded
# ---------------------------------------------------------------------------


@respx.mock
def test_tool_result_with_dict_output_is_json_encoded() -> None:
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_chat_response())
    )

    request = LLMRequest(
        model="gpt-4o",
        messages=[
            _user_message("hi"),
            Message(
                role="tool",
                content=[
                    ToolResultBlock(
                        call_id="call_x",
                        output={"score": 42},
                        success=True,
                    )
                ],
            ),
        ],
    )
    _make_provider().complete(request)

    body = json.loads(route.calls[0].request.content)
    assert body["messages"][1] == {
        "role": "tool",
        "tool_call_id": "call_x",
        "content": json.dumps({"score": 42}),
    }


# ---------------------------------------------------------------------------
# 24. Signature-only thinking field still yields a ThinkingBlock
# ---------------------------------------------------------------------------


@respx.mock
def test_encrypted_reasoning_only_yields_signature_thinking_block() -> None:
    payload = _chat_response(
        content="answer", encrypted_reasoning="opaque-token=="
    )
    respx.post(CHAT_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    response = _make_provider().complete(_basic_request())

    assert response.content == [
        ThinkingBlock(text="", signature="opaque-token=="),
        TextBlock(text="answer"),
    ]


# ---------------------------------------------------------------------------
# 25. Base URL with trailing slash is normalised
# ---------------------------------------------------------------------------


@respx.mock
def test_trailing_slash_in_base_url_is_normalised() -> None:
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_chat_response())
    )

    provider = OpenAICompatProvider(
        base_url=BASE_URL + "/",
        api_key="sk-test",
    )
    provider.complete(_basic_request())

    assert route.called


# ---------------------------------------------------------------------------
# origin rendering: injected turns
# render as system-role wire messages
# ---------------------------------------------------------------------------


def _origin_wire_messages(messages: list[Message]) -> list[dict[str, Any]]:
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_chat_response())
    )
    _make_provider().complete(_basic_request(messages=messages))
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    return body["messages"]


@respx.mock
def test_origin_system_renders_as_system_role_wire_message() -> None:
    injected = Message(
        role="user",
        content=[TextBlock(text="host says hi")],
        origin="system",
    )
    wire = _origin_wire_messages([_user_message("real human words"), injected])
    assert wire == [
        {"role": "user", "content": "real human words"},
        {"role": "system", "content": "host says hi"},
    ]


@respx.mock
def test_origin_memory_renders_as_system_role_wire_message() -> None:
    recalled = Message(
        role="user",
        content=[TextBlock(text="recalled note")],
        origin="memory",
    )
    wire = _origin_wire_messages([recalled, _user_message("the actual ask")])
    assert wire == [
        {"role": "system", "content": "recalled note"},
        {"role": "user", "content": "the actual ask"},
    ]


@respx.mock
def test_origin_human_renders_as_plain_user_wire_message() -> None:
    """origin=human: the role is the natural author, so rendering matches the default."""
    explicit = Message(
        role="user", content=[TextBlock(text="hello")], origin="human"
    )
    wire = _origin_wire_messages([explicit])
    assert wire == [{"role": "user", "content": "hello"}]


# -- output_schema / thinking / effort (OpenAI compat wire mapping) ------------


@respx.mock
def test_output_schema_wired_to_response_format_json_schema() -> None:
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_chat_response())
    )
    schema = {"type": "object", "properties": {"v": {"type": "boolean"}}}
    provider = _make_provider()
    provider.complete(_basic_request(output_schema=schema))
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "noeta_output", "schema": schema},
    }


@respx.mock
def test_effort_wired_to_reasoning_effort_with_bucket_collapse() -> None:
    """low/medium/high pass through unchanged; xhigh/max collapse to high."""
    provider = _make_provider()
    for noeta_effort, wire_effort in (
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "high"),
        ("max", "high"),
    ):
        route = respx.post(CHAT_ENDPOINT).mock(
            return_value=httpx.Response(200, json=_chat_response())
        )
        provider.complete(_basic_request(effort=noeta_effort))
        body = json.loads(route.calls.last.request.content.decode("utf-8"))
        assert body["reasoning_effort"] == wire_effort


@respx.mock
def test_thinking_silently_ignored_on_openai_compat() -> None:
    """OpenAI compat has no thinking parameter: don't raise, don't write it into the body."""
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_chat_response())
    )
    provider = _make_provider()
    provider.complete(_basic_request(thinking="adaptive"))
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert "thinking" not in body


@respx.mock
def test_three_fields_none_omitted_from_body() -> None:
    """All three fields None: no related key appears in the body — existing behavior untouched."""
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_chat_response())
    )
    provider = _make_provider()
    provider.complete(_basic_request(text="hi"))
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert "response_format" not in body
    assert "reasoning_effort" not in body
    assert "thinking" not in body


# ---------------------------------------------------------------------------
# ImageBlock defense
# ---------------------------------------------------------------------------
#
# This Chat Completions adapter does not support image input. Mis-routing an
# image task to it must raise **explicitly**, never silently drop the image
# (_flatten_text_blocks only picks TextBlock, so the image would be swallowed).
# The error is raised while building the wire body; no HTTP request is sent.

_IMG_REF = ContentRef(hash="sha256:img", size=3, media_type="image/png")


@respx.mock
def test_image_block_in_user_message_raises_explicit_error() -> None:
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_chat_response())
    )
    provider = _make_provider()
    request = _basic_request(
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="look at this"), ImageBlock(source=_IMG_REF)],
            )
        ]
    )
    with pytest.raises(ValueError, match="does not support image"):
        provider.complete(request)
    assert not route.called


@respx.mock
def test_image_block_in_assistant_message_raises_explicit_error() -> None:
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_chat_response())
    )
    provider = _make_provider()
    request = _basic_request(
        messages=[
            Message(role="assistant", content=[ImageBlock(source=_IMG_REF)])
        ]
    )
    with pytest.raises(ValueError, match="does not support image"):
        provider.complete(request)
    assert not route.called


@respx.mock
def test_image_block_in_tool_message_raises_explicit_error() -> None:
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_chat_response())
    )
    provider = _make_provider()
    request = _basic_request(
        messages=[Message(role="tool", content=[ImageBlock(source=_IMG_REF)])]
    )
    with pytest.raises(ValueError, match="does not support image"):
        provider.complete(request)
    assert not route.called


@respx.mock
def test_image_block_in_host_injected_user_turn_raises_explicit_error() -> None:
    """A host-injected turn (origin=system/memory) carrying an image must also raise — the injection path must not bypass the defense."""
    route = respx.post(CHAT_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_chat_response())
    )
    provider = _make_provider()
    request = _basic_request(
        messages=[
            Message(
                role="user",
                origin="system",
                content=[ImageBlock(source=_IMG_REF)],
            )
        ]
    )
    with pytest.raises(ValueError, match="does not support image"):
        provider.complete(request)
    assert not route.called
