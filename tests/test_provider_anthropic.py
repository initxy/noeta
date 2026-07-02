"""Test matrix for :class:`noeta.providers.anthropic.AnthropicProvider`.

Every adapter rule gets a dedicated case. All HTTP traffic is
mocked via ``respx`` so the suite performs zero real network calls. Tests
deliberately mirror the shape of ``tests/test_provider_openai_compat.py``
but focus on Anthropic-specific wire-shape constraints (assistant content
order, tool_result placement, max_tokens fail-fast, thinking adapter-unit
only).
"""

from __future__ import annotations

import base64
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


def _user(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)])


def _basic_request(
    *,
    model: str = "claude-opus-4-7",
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
        messages=messages if messages is not None else [_user(text)],
        tools=tools or [],
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        output_schema=output_schema,
        thinking=thinking,
        effort=effort,
    )


def _anthropic_response(
    *,
    content: list[dict[str, Any]] | None = None,
    stop_reason: str | None = "end_turn",
    usage: dict[str, Any] | None = None,
    response_type: str = "message",
    role: str = "assistant",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "msg_test",
        "type": response_type,
        "role": role,
        "model": "claude-opus-4-7",
        "content": (
            content
            if content is not None
            else [{"type": "text", "text": "ok"}]
        ),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage
        or {
            "input_tokens": 10,
            "output_tokens": 5,
        },
    }
    return body


# ---------------------------------------------------------------------------
# Plain text round-trip
# ---------------------------------------------------------------------------


@respx.mock
def test_plain_text_response_maps_to_end_turn_textblock() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200, json=_anthropic_response(content=[{"type": "text", "text": "hello"}])
        )
    )

    provider = _make_provider()
    response = provider.complete(_basic_request(text="say hi"))

    assert route.called
    assert isinstance(response, LLMResponse)
    assert response.stop_reason == "end_turn"
    assert response.content == [TextBlock(text="hello")]
    assert response.usage == Usage(uncached=10, output=5)
    assert response.usage.input == 10
    assert response.raw is not None and response.raw["id"] == "msg_test"


# ---------------------------------------------------------------------------
# Headers / endpoint (Q8)
# ---------------------------------------------------------------------------


@respx.mock
def test_request_uses_x_api_key_and_anthropic_version_headers() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )

    provider = _make_provider(anthropic_version="2026-01-01")
    provider.complete(_basic_request())

    request = route.calls.last.request
    assert request.headers["x-api-key"] == "sk-ant-test"
    assert request.headers["anthropic-version"] == "2026-01-01"
    assert request.headers["content-type"].startswith("application/json")
    # NOT a Bearer header (Anthropic uses x-api-key)
    assert "authorization" not in {k.lower() for k in request.headers.keys()}


@respx.mock
def test_extra_headers_are_forwarded() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )

    provider = _make_provider(extra_headers={"anthropic-beta": "extended-cache-2026"})
    provider.complete(_basic_request())

    request = route.calls.last.request
    assert request.headers["anthropic-beta"] == "extended-cache-2026"


# ---------------------------------------------------------------------------
# HeaderAwareProvider capability (per-request header injection)
# ---------------------------------------------------------------------------


def test_provider_satisfies_header_aware_protocol() -> None:
    from noeta.protocols.messages import HeaderAwareProvider

    assert isinstance(_make_provider(), HeaderAwareProvider)


@respx.mock
def test_complete_with_headers_merges_over_client_headers() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )

    provider = _make_provider(anthropic_version="2026-01-01")
    provider.complete_with_headers(
        _basic_request(), {"x-noeta-task": "task-abc", "x-request-id": "req-1"}
    )

    request = route.calls.last.request
    # Per-request headers are attached...
    assert request.headers["x-noeta-task"] == "task-abc"
    assert request.headers["x-request-id"] == "req-1"
    # ...and the shared client's constructor headers survive alongside them.
    assert request.headers["x-api-key"] == "sk-ant-test"
    assert request.headers["anthropic-version"] == "2026-01-01"


@respx.mock
def test_complete_with_headers_none_matches_plain_complete() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )

    provider = _make_provider()
    # ``complete`` delegates to ``complete_with_headers(request, None)``; the
    # None path adds no extra headers over the shared client.
    response = provider.complete_with_headers(_basic_request(), None)

    request = route.calls.last.request
    assert request.headers["x-api-key"] == "sk-ant-test"
    assert isinstance(response, LLMResponse)


# ---------------------------------------------------------------------------
# max_tokens fail-fast (B4)
# ---------------------------------------------------------------------------


@respx.mock
def test_max_tokens_fail_fast_when_neither_request_nor_default() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = AnthropicProvider(
        api_key="k", base_url=BASE_URL, default_max_tokens=None
    )
    with pytest.raises(ValueError, match="Anthropic requires max_tokens"):
        provider.complete(_basic_request(max_tokens=None))


@respx.mock
def test_max_tokens_request_wins_over_default() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider(default_max_tokens=2048)
    provider.complete(_basic_request(max_tokens=512))

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body["max_tokens"] == 512


@respx.mock
def test_max_tokens_uses_constructor_default_when_request_missing() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider(default_max_tokens=2048)
    provider.complete(_basic_request(max_tokens=None))

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body["max_tokens"] == 2048


@respx.mock
def test_max_tokens_request_only_works_without_default() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = AnthropicProvider(api_key="k", base_url=BASE_URL)
    provider.complete(_basic_request(max_tokens=999))

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body["max_tokens"] == 999


# ---------------------------------------------------------------------------
# system field separation (G2)
# ---------------------------------------------------------------------------


@respx.mock
def test_system_field_lifted_to_top_level() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    sys = Message(
        role="system",
        content=[TextBlock(text="line1"), TextBlock(text="line2")],
    )
    provider.complete(_basic_request(system=sys))

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    # #4: system is lifted into block form to carry a cache_control breakpoint.
    assert body["system"] == [
        {
            "type": "text",
            "text": "line1\nline2",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    # system not inside the messages array
    assert all(msg["role"] != "system" for msg in body["messages"])


@respx.mock
def test_system_in_messages_array_raises() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    bad = Message(role="system", content=[TextBlock(text="oops")])
    with pytest.raises(ValueError, match="system must use LLMRequest.system"):
        provider.complete(_basic_request(messages=[bad, _user("hi")]))


@respx.mock
def test_system_none_omits_top_level_system_field() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    provider.complete(_basic_request(system=None))

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert "system" not in body


# ---------------------------------------------------------------------------
# Assistant content deterministic regrouping (B2)
# ---------------------------------------------------------------------------


@respx.mock
def test_assistant_content_regrouped_thinking_text_tool_use() -> None:
    """rev2 B2: adapter regroups assistant content blocks
    deterministically as ThinkingBlock* → TextBlock* → ToolUseBlock*."""
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    # Deliberately mis-ordered input:
    assistant = Message(
        role="assistant",
        content=[
            ToolUseBlock(call_id="t1", tool_name="echo", arguments={"x": 1}),
            TextBlock(text="reasoning result"),
            ThinkingBlock(text="thinking...", signature="sig-1"),
        ],
    )
    provider.complete(
        _basic_request(messages=[_user("q"), assistant, _user("then")])
    )

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assistant_msg = body["messages"][1]
    types = [b["type"] for b in assistant_msg["content"]]
    assert types == ["thinking", "text", "tool_use"]


@respx.mock
def test_assistant_content_stable_sort_within_group() -> None:
    """B2: same-type blocks preserve caller order; only inter-type
    regroup is applied. Two TextBlocks in input order A, B stay A, B
    in output even if a tool_use sits between them on input."""
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    assistant = Message(
        role="assistant",
        content=[
            TextBlock(text="A"),
            ToolUseBlock(call_id="t1", tool_name="echo", arguments={}),
            TextBlock(text="B"),
        ],
    )
    provider.complete(_basic_request(messages=[_user("q"), assistant]))

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assistant_content = body["messages"][1]["content"]
    text_blocks_in_order = [b["text"] for b in assistant_content if b["type"] == "text"]
    assert text_blocks_in_order == ["A", "B"]


# ---------------------------------------------------------------------------
# tool_use / tool_result round-trip (G3)
# ---------------------------------------------------------------------------


@respx.mock
def test_tool_use_block_outbound_translation() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    assistant = Message(
        role="assistant",
        content=[
            ToolUseBlock(
                call_id="toolu_001", tool_name="lookup", arguments={"q": "x"}
            )
        ],
    )
    provider.complete(_basic_request(messages=[_user("ask"), assistant]))

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    block = body["messages"][1]["content"][0]
    # #4: this is the last block of the last message, so it carries the
    # ephemeral cache_control breakpoint.
    assert block == {
        "type": "tool_use",
        "id": "toolu_001",
        "name": "lookup",
        "input": {"q": "x"},
        "cache_control": {"type": "ephemeral"},
    }


@respx.mock
def test_role_tool_message_becomes_user_with_tool_result_only() -> None:
    """B3: role='tool' folds into one user message whose content is
    exclusively tool_result blocks in input order."""
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    tool_msg = Message(
        role="tool",
        content=[
            ToolResultBlock(
                call_id="toolu_001",
                output="forty-two",
                success=True,
                error=None,
            ),
            ToolResultBlock(
                call_id="toolu_002",
                output={"k": "v"},
                success=True,
                error=None,
            ),
        ],
    )
    provider.complete(_basic_request(messages=[_user("hi"), tool_msg]))

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    tool_user = body["messages"][1]
    assert tool_user["role"] == "user"
    assert len(tool_user["content"]) == 2
    assert all(b["type"] == "tool_result" for b in tool_user["content"])
    assert tool_user["content"][0]["tool_use_id"] == "toolu_001"
    assert tool_user["content"][0]["content"] == "forty-two"
    assert tool_user["content"][0]["is_error"] is False
    assert tool_user["content"][1]["tool_use_id"] == "toolu_002"
    assert tool_user["content"][1]["content"] == '{"k": "v"}'


@respx.mock
def test_role_user_with_tool_result_block_raises() -> None:
    """B3 placement defense: ToolResultBlock is forbidden inside
    role='user' Message; caller must use role='tool'."""
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    bad_user = Message(
        role="user",
        content=[
            TextBlock(text="hello"),
            ToolResultBlock(
                call_id="x", output="y", success=True, error=None
            ),
        ],
    )
    with pytest.raises(ValueError, match="ToolResultBlock not allowed in role='user'"):
        provider.complete(_basic_request(messages=[bad_user]))


@respx.mock
def test_role_tool_with_non_tool_result_block_raises() -> None:
    """B3 placement defense: role='tool' Message must contain only
    ToolResultBlock; anything else raises."""
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    bad_tool = Message(
        role="tool",
        content=[
            TextBlock(text="oops"),
        ],
    )
    with pytest.raises(
        ValueError, match="role='tool' message may only contain ToolResultBlock"
    ):
        provider.complete(_basic_request(messages=[bad_tool]))


@respx.mock
def test_tool_result_error_prefixed_to_content() -> None:
    """Q10: ToolResultBlock.error str is prefixed to content to keep
    Noeta's two-field success/error split visible in Anthropic's
    one-field tool_result body."""
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    tool_msg = Message(
        role="tool",
        content=[
            ToolResultBlock(
                call_id="t",
                output={"x": 1},
                success=False,
                error="boom",
            )
        ],
    )
    provider.complete(_basic_request(messages=[_user("q"), tool_msg]))

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    tr = body["messages"][1]["content"][0]
    assert tr["content"].startswith("[error] boom\n")
    assert tr["is_error"] is True


@respx.mock
def test_tool_use_block_inbound_translation() -> None:
    """Inbound: tool_use response block → ToolUseBlock with same ID/name/input."""
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_anthropic_response(
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_xyz",
                        "name": "lookup",
                        "input": {"q": "x"},
                    }
                ],
                stop_reason="tool_use",
            ),
        )
    )
    provider = _make_provider()
    response = provider.complete(_basic_request())
    assert response.stop_reason == "tool_use"
    assert response.content == [
        ToolUseBlock(call_id="toolu_xyz", tool_name="lookup", arguments={"q": "x"})
    ]


# ---------------------------------------------------------------------------
# Thinking adapter-unit translation (G4 / B1)
# ---------------------------------------------------------------------------


@respx.mock
def test_thinking_block_inbound_translation() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_anthropic_response(
                content=[
                    {
                        "type": "thinking",
                        "thinking": "deliberation...",
                        "signature": "opaque-sig-1",
                    },
                    {"type": "text", "text": "answer"},
                ],
            ),
        )
    )
    provider = _make_provider()
    response = provider.complete(_basic_request())
    assert response.content == [
        ThinkingBlock(text="deliberation...", signature="opaque-sig-1"),
        TextBlock(text="answer"),
    ]


@respx.mock
def test_redacted_thinking_block_inbound_translation() -> None:
    """A ``redacted_thinking`` block (encrypted reasoning) is preserved as a
    ``ThinkingBlock`` carrying the opaque ``data`` blob — NOT silently dropped
    (dropping it strands a tool-use turn whose reasoning the API expects back)."""
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_anthropic_response(
                content=[
                    {"type": "redacted_thinking", "data": "ENCRYPTED=="},
                    {"type": "text", "text": "answer"},
                ],
            ),
        )
    )
    provider = _make_provider()
    response = provider.complete(_basic_request())
    assert response.content == [
        ThinkingBlock(text="", signature=None, data="ENCRYPTED=="),
        TextBlock(text="answer"),
    ]


@respx.mock
def test_redacted_thinking_block_without_data_is_dropped() -> None:
    """A ``redacted_thinking`` entry whose ``data`` is missing/non-str is
    DROPPED, not kept as ``ThinkingBlock(text="", data=None)`` — keeping it
    would re-emit an empty ``{"type":"thinking","thinking":""}`` block outbound,
    which the API 400s. Only the real text block survives."""
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_anthropic_response(
                content=[
                    {"type": "redacted_thinking"},            # no 'data'
                    {"type": "redacted_thinking", "data": 123},  # non-str
                    {"type": "text", "text": "answer"},
                ],
            ),
        )
    )
    provider = _make_provider()
    response = provider.complete(_basic_request())
    assert response.content == [TextBlock(text="answer")]


@respx.mock
def test_redacted_thinking_block_outbound_translation() -> None:
    """A ``ThinkingBlock`` carrying ``data`` re-emits the opaque blob under the
    ``redacted_thinking`` wire type verbatim — never as an (invalid) empty
    ``thinking`` block."""
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    assistant = Message(
        role="assistant",
        content=[ThinkingBlock(text="", data="ENCRYPTED==")],
    )
    provider.complete(_basic_request(messages=[_user("q"), assistant]))

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assistant_block = body["messages"][1]["content"][0]
    assert assistant_block == {
        "type": "redacted_thinking",
        "data": "ENCRYPTED==",
        "cache_control": {"type": "ephemeral"},
    }


@respx.mock
def test_thinking_block_outbound_translation_with_signature() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    assistant = Message(
        role="assistant",
        content=[ThinkingBlock(text="reasoning", signature="abc")],
    )
    provider.complete(_basic_request(messages=[_user("q"), assistant]))

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assistant_block = body["messages"][1]["content"][0]
    # #4: last block of the last message carries the cache_control breakpoint.
    assert assistant_block == {
        "type": "thinking",
        "thinking": "reasoning",
        "signature": "abc",
        "cache_control": {"type": "ephemeral"},
    }


@respx.mock
def test_thinking_block_outbound_without_signature_omits_field() -> None:
    """B1 / Q5: signature=None is propagated as missing field (not
    written as null) so Anthropic returns its own reject — adapter
    never silently drops the thinking block."""
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    assistant = Message(
        role="assistant",
        content=[ThinkingBlock(text="reasoning")],
    )
    provider.complete(_basic_request(messages=[_user("q"), assistant]))

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assistant_block = body["messages"][1]["content"][0]
    # #4: last block of the last message carries the cache_control breakpoint;
    # signature is still absent (None propagates as a missing field).
    assert assistant_block == {
        "type": "thinking",
        "thinking": "reasoning",
        "cache_control": {"type": "ephemeral"},
    }
    assert "signature" not in assistant_block


# ---------------------------------------------------------------------------
# tools schema unpack (NB3)
# ---------------------------------------------------------------------------


@respx.mock
def test_provider_tool_schemas_openai_shape_unpacks_to_anthropic_shape() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    provider.complete(
        _basic_request(
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "description": "echo something",
                        "parameters": {
                            "type": "object",
                            "properties": {"x": {"type": "integer"}},
                        },
                    },
                }
            ]
        )
    )
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    # #4: the last tool carries an ephemeral cache_control breakpoint.
    assert body["tools"] == [
        {
            "name": "echo",
            "description": "echo something",
            "input_schema": {
                "type": "object",
                "properties": {"x": {"type": "integer"}},
            },
            "cache_control": {"type": "ephemeral"},
        }
    ]


@respx.mock
def test_provider_tool_schemas_missing_description_defaults_to_empty() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    provider.complete(
        _basic_request(
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "parameters": {"type": "object"},
                    },
                }
            ]
        )
    )
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body["tools"][0]["description"] == ""


@respx.mock
def test_tools_empty_omits_tools_field() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    provider.complete(_basic_request(tools=[]))
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert "tools" not in body


@respx.mock
def test_tools_missing_function_key_raises() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    with pytest.raises(ValueError, match="missing 'function' dict"):
        provider.complete(_basic_request(tools=[{"type": "function"}]))


@respx.mock
def test_tools_missing_parameters_raises() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    with pytest.raises(ValueError, match="missing/invalid 'parameters'"):
        provider.complete(
            _basic_request(
                tools=[
                    {
                        "type": "function",
                        "function": {"name": "echo"},
                    }
                ]
            )
        )


@respx.mock
def test_tools_invalid_parameters_type_raises() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    with pytest.raises(ValueError, match="missing/invalid 'parameters'"):
        provider.complete(
            _basic_request(
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "parameters": "not-a-dict",
                        },
                    }
                ]
            )
        )


@respx.mock
def test_tools_empty_name_raises() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    with pytest.raises(ValueError, match="missing/invalid 'name'"):
        provider.complete(
            _basic_request(
                tools=[
                    {
                        "type": "function",
                        "function": {"name": "", "parameters": {}},
                    }
                ]
            )
        )


# ---------------------------------------------------------------------------
# stop_reason mapping (G5 / Q4)
# ---------------------------------------------------------------------------


@respx.mock
def test_stop_reason_end_turn_maps_directly() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response(stop_reason="end_turn"))
    )
    provider = _make_provider()
    response = provider.complete(_basic_request())
    assert response.stop_reason == "end_turn"


@respx.mock
def test_stop_reason_max_tokens_maps_directly() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_anthropic_response(
                content=[{"type": "text", "text": "trunc"}], stop_reason="max_tokens"
            ),
        )
    )
    provider = _make_provider()
    response = provider.complete(_basic_request())
    assert response.stop_reason == "max_tokens"


@respx.mock
def test_stop_sequence_maps_to_end_turn() -> None:
    """Q4: Anthropic stop_sequence → Noeta end_turn (Noeta has no
    stop_sequence enum)."""
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200, json=_anthropic_response(stop_reason="stop_sequence")
        )
    )
    provider = _make_provider()
    response = provider.complete(_basic_request())
    assert response.stop_reason == "end_turn"


@respx.mock
def test_stop_reason_refusal_maps_to_end_turn() -> None:
    """A safety-classifier ``refusal`` is a completed HTTP-200 turn; it must
    map to ``end_turn`` (the refusal text surfaces as the assistant's finished
    answer) rather than ``error`` (which would fail the task non-retryably and
    discard the refusal). ``claude-opus-4-8`` can return this."""
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_anthropic_response(
                content=[{"type": "text", "text": "I can't help with that."}],
                stop_reason="refusal",
            ),
        )
    )
    provider = _make_provider()
    response = provider.complete(_basic_request())
    assert response.stop_reason == "end_turn"
    assert response.content == [TextBlock(text="I can't help with that.")]


@respx.mock
def test_unknown_stop_reason_maps_to_error_without_raising() -> None:
    """Architect NB2: unknown / future stop_reason maps to
    LLMResponse.stop_reason='error' without raising (mirrors
    OpenAICompatProvider behaviour)."""
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200, json=_anthropic_response(stop_reason="weird-future-value")
        )
    )
    provider = _make_provider()
    response = provider.complete(_basic_request())
    assert response.stop_reason == "error"


@respx.mock
def test_missing_stop_reason_maps_to_error_without_raising() -> None:
    """Architect NB2: missing stop_reason maps to error (not raise)."""
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response(stop_reason=None))
    )
    provider = _make_provider()
    response = provider.complete(_basic_request())
    assert response.stop_reason == "error"


# ---------------------------------------------------------------------------
# Inconsistent stop_reason vs content
# ---------------------------------------------------------------------------


@respx.mock
def test_inconsistent_stop_reason_tool_use_with_no_tool_use_block_raises() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_anthropic_response(
                content=[{"type": "text", "text": "hi"}], stop_reason="tool_use"
            ),
        )
    )
    provider = _make_provider()
    with pytest.raises(ValueError, match="inconsistent.*tool_use"):
        provider.complete(_basic_request())


@respx.mock
def test_inconsistent_stop_reason_end_turn_with_tool_use_block_raises() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_anthropic_response(
                content=[
                    {
                        "type": "tool_use",
                        "id": "t",
                        "name": "echo",
                        "input": {},
                    }
                ],
                stop_reason="end_turn",
            ),
        )
    )
    provider = _make_provider()
    with pytest.raises(ValueError, match="inconsistent.*end_turn"):
        provider.complete(_basic_request())


# ---------------------------------------------------------------------------
# Response defense (Q11)
# ---------------------------------------------------------------------------


@respx.mock
def test_response_type_not_message_raises() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200, json=_anthropic_response(response_type="completion")
        )
    )
    provider = _make_provider()
    with pytest.raises(ValueError, match="'type' was not 'message'"):
        provider.complete(_basic_request())


@respx.mock
def test_response_role_not_assistant_raises() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response(role="user"))
    )
    provider = _make_provider()
    with pytest.raises(ValueError, match="'role' was not 'assistant'"):
        provider.complete(_basic_request())


@respx.mock
def test_response_content_not_a_list_raises() -> None:
    payload = _anthropic_response()
    payload["content"] = "not-a-list"
    respx.post(MESSAGES_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
    provider = _make_provider()
    with pytest.raises(ValueError, match="'content' must be a list"):
        provider.complete(_basic_request())


@respx.mock
def test_unknown_response_block_type_is_silently_skipped_intentional() -> None:
    """Intentional forward-compat: when Anthropic introduces new content
    block types (e.g. ``server_tool_use`` / ``image`` / future thinking
    variants), the adapter must NOT raise — it strips the unknown block
    and surfaces the rest. Verify is then the layer that catches drift.

    Pinning this as a test (not just a docstring) prevents the
    behaviour from being silently flipped to "raise on unknown" by a
    well-meaning future refactor.
    """
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_anthropic_response(
                content=[
                    {"type": "text", "text": "before"},
                    {"type": "future_block_type", "payload": "unknown"},
                    {"type": "text", "text": "after"},
                ]
            ),
        )
    )
    provider = _make_provider()
    response = provider.complete(_basic_request())
    # Adapter survives + retains the known blocks; unknown is dropped.
    assert response.content == [
        TextBlock(text="before"),
        TextBlock(text="after"),
    ]


@respx.mock
def test_response_tool_use_input_not_dict_raises() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_anthropic_response(
                content=[
                    {
                        "type": "tool_use",
                        "id": "t",
                        "name": "echo",
                        "input": "not-a-dict",
                    }
                ],
                stop_reason="tool_use",
            ),
        )
    )
    provider = _make_provider()
    with pytest.raises(ValueError, match="tool_use.input"):
        provider.complete(_basic_request())


@respx.mock
def test_response_invalid_json_raises() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, content=b"not-json")
    )
    provider = _make_provider()
    with pytest.raises(ValueError, match="not valid JSON"):
        provider.complete(_basic_request())


# ---------------------------------------------------------------------------
# HTTP error translation (G5 + ② error recovery)
# ---------------------------------------------------------------------------
#
# Was "pass-through httpx.HTTPStatusError"; ② now requires the adapter to
# translate into the neutral Noeta taxonomy at its boundary.


@respx.mock
def test_http_4xx_translates_to_fatal() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            401, json={"type": "error", "error": {"type": "authentication_error"}}
        )
    )
    provider = _make_provider()
    with pytest.raises(FatalError):
        provider.complete(_basic_request())


@respx.mock
def test_http_5xx_translates_to_transient() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(return_value=httpx.Response(500))
    provider = _make_provider()
    with pytest.raises(TransientError):
        provider.complete(_basic_request())


# ---------------------------------------------------------------------------
# usage / cache fields (G5)
# ---------------------------------------------------------------------------


@respx.mock
def test_usage_passes_through_with_cache_fields() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_anthropic_response(
                usage={
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "cache_creation_input_tokens": 50,
                    "cache_read_input_tokens": 25,
                }
            ),
        )
    )
    provider = _make_provider()
    response = provider.complete(_basic_request())
    # Anthropic input_tokens is the *uncached* portion; cache read/write are
    # separate. The derived Usage.input must sum to the full input total.
    assert response.usage == Usage(
        uncached=100,
        cache_read=25,
        cache_write=50,
        output=200,
    )
    assert response.usage.input == 175


@respx.mock
def test_usage_without_cache_fields_input_equals_uncached() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_anthropic_response(
                usage={"input_tokens": 40, "output_tokens": 12}
            ),
        )
    )
    provider = _make_provider()
    response = provider.complete(_basic_request())
    assert response.usage == Usage(uncached=40, output=12)
    assert response.usage.cache_read == 0
    assert response.usage.cache_write == 0
    assert response.usage.input == 40


@respx.mock
def test_usage_missing_yields_empty_usage_without_raising() -> None:
    body = _anthropic_response()
    del body["usage"]
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=body)
    )
    provider = _make_provider()
    response = provider.complete(_basic_request())
    assert response.usage == Usage()


# ---------------------------------------------------------------------------
# Determinism (G6)
# ---------------------------------------------------------------------------


@respx.mock
def test_same_request_and_mock_produces_byte_equal_response() -> None:
    """G6: adapter is pure translation — same LLMRequest + same mock
    response → byte-equal LLMResponse twice in a row."""
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_anthropic_response(
                content=[
                    {"type": "thinking", "thinking": "...", "signature": "s"},
                    {"type": "text", "text": "hi"},
                ]
            ),
        )
    )
    provider = _make_provider()
    request = _basic_request(temperature=0.7)
    r1 = provider.complete(request)
    r2 = provider.complete(request)
    assert r1 == r2


# ---------------------------------------------------------------------------
# Temperature passthrough
# ---------------------------------------------------------------------------


@respx.mock
def test_temperature_passed_through() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    provider.complete(_basic_request(temperature=0.5))
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body["temperature"] == 0.5


@respx.mock
def test_temperature_none_omits_field() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    provider.complete(_basic_request(temperature=None))
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert "temperature" not in body


# ---------------------------------------------------------------------------
# unsupported role
# ---------------------------------------------------------------------------


@respx.mock
def test_unsupported_role_raises() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    # Construct via raw fields to bypass Literal check (this is a
    # defensive path for future Message role evolution)
    bad = Message.__new__(Message)
    object.__setattr__(bad, "role", "narrator")
    object.__setattr__(bad, "content", [TextBlock(text="x")])
    with pytest.raises(ValueError, match="unsupported message role"):
        provider.complete(_basic_request(messages=[bad]))


# ---------------------------------------------------------------------------
# ② error recovery — neutral error translation
# ---------------------------------------------------------------------------
#
# The adapter translates Anthropic's wire-shape failures into the neutral
# Noeta taxonomy so the runtime never sees an httpx type. Anthropic error
# body: ``{"type": "error", "error": {"type", "message"}}``.


@respx.mock
def test_429_with_retry_after_maps_to_transient() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            429,
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
            headers={"retry-after": "5"},
        )
    )
    with pytest.raises(TransientError) as ex:
        _make_provider().complete(_basic_request())
    assert ex.value.retry_after == 5.0


@respx.mock
def test_529_overloaded_maps_to_transient() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            529,
            json={"type": "error", "error": {"type": "overloaded_error", "message": "overloaded"}},
        )
    )
    with pytest.raises(TransientError):
        _make_provider().complete(_basic_request())


@respx.mock
def test_500_maps_to_transient() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            500,
            json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
        )
    )
    with pytest.raises(TransientError):
        _make_provider().complete(_basic_request())


@respx.mock
def test_400_prompt_too_long_maps_to_overflow() -> None:
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
        _make_provider().complete(_basic_request())


@respx.mock
def test_400_invalid_request_non_overflow_maps_to_fatal() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "missing field"},
            },
        )
    )
    with pytest.raises(FatalError):
        _make_provider().complete(_basic_request())


@respx.mock
def test_401_maps_to_fatal() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(
            401,
            json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}},
        )
    )
    with pytest.raises(FatalError):
        _make_provider().complete(_basic_request())


@respx.mock
def test_connect_error_maps_to_transient() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    with pytest.raises(TransientError):
        _make_provider().complete(_basic_request())


@respx.mock
def test_timeout_maps_to_transient() -> None:
    respx.post(MESSAGES_ENDPOINT).mock(
        side_effect=httpx.ReadTimeout("timed out")
    )
    with pytest.raises(TransientError):
        _make_provider().complete(_basic_request())


# ---------------------------------------------------------------------------
# origin rendering: the
# vendor-specific tag syntax lives only in the adapter
# ---------------------------------------------------------------------------


def _injected(text: str, origin: str) -> Message:
    return Message(
        role="user", content=[TextBlock(text=text)], origin=origin  # type: ignore[arg-type]
    )


def _wire_messages(messages: list[Message]) -> list[dict[str, Any]]:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    _make_provider().complete(_basic_request(messages=messages))
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    return body["messages"]


@respx.mock
def test_origin_system_wrapped_in_system_reminder_and_merged_into_prev_user_turn() -> None:
    wire = _wire_messages(
        [_user("real human words"), _injected("host says hi", "system")]
    )
    assert len(wire) == 1
    assert wire[0]["role"] == "user"
    # #4: the last block of the (only, hence last) message carries cache_control.
    assert wire[0]["content"] == [
        {"type": "text", "text": "real human words"},
        {
            "type": "text",
            "text": "<system-reminder>\nhost says hi\n</system-reminder>",
            "cache_control": {"type": "ephemeral"},
        },
    ]


@respx.mock
def test_origin_memory_before_user_merges_into_next_user_turn() -> None:
    """Injected message first, real user message after (the other ordering at the memory-recall seam) merges just the same."""
    wire = _wire_messages(
        [_injected("recalled note", "memory"), _user("the actual ask")]
    )
    assert len(wire) == 1
    assert wire[0]["role"] == "user"
    # #4: the last block of the (only, hence last) message carries cache_control.
    assert wire[0]["content"] == [
        {
            "type": "text",
            "text": "<system-reminder>\nrecalled note\n</system-reminder>",
        },
        {
            "type": "text",
            "text": "the actual ask",
            "cache_control": {"type": "ephemeral"},
        },
    ]


@respx.mock
def test_origin_system_with_no_adjacent_user_turn_stands_alone() -> None:
    assistant = Message(role="assistant", content=[TextBlock(text="working on it")])
    wire = _wire_messages(
        [_user("q"), assistant, _injected("mid-loop reminder", "system")]
    )
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    # #4: last block of the last message carries cache_control.
    assert wire[2]["content"] == [
        {
            "type": "text",
            "text": "<system-reminder>\nmid-loop reminder\n</system-reminder>",
            "cache_control": {"type": "ephemeral"},
        }
    ]


@respx.mock
def test_origin_system_merges_into_tool_result_user_turn() -> None:
    """A tool_result wire turn is also a user role, so the injected message merges into it too."""
    assistant = Message(
        role="assistant",
        content=[
            ToolUseBlock(call_id="c1", tool_name="echo", arguments={"k": "x"})
        ],
    )
    tool = Message(
        role="tool",
        content=[ToolResultBlock(call_id="c1", output="ok", success=True)],
    )
    wire = _wire_messages(
        [_user("q"), assistant, tool, _injected("file changed on disk", "system")]
    )
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    assert wire[2]["content"][0]["type"] == "tool_result"
    # #4: last block of the last message carries cache_control.
    assert wire[2]["content"][1] == {
        "type": "text",
        "text": "<system-reminder>\nfile changed on disk\n</system-reminder>",
        "cache_control": {"type": "ephemeral"},
    }


@respx.mock
def test_plain_consecutive_user_turns_stay_unmerged() -> None:
    """Adjacent user messages without origin keep their original rendering (no merge) — existing behavior untouched."""
    wire = _wire_messages([_user("one"), _user("two")])
    assert [m["role"] for m in wire] == ["user", "user"]


@respx.mock
def test_origin_human_renders_as_plain_user_turn() -> None:
    """origin=human: the role is the natural author, so rendering exactly matches the default (no tag wrapper)."""
    wire = _wire_messages([_injected("hello", "human")])
    # #4: last block of the last message carries cache_control.
    assert wire == [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "hello",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    ]


# -- output_schema / thinking / effort (new optional fields, wire mapping) ---


@respx.mock
def test_output_schema_wired_to_output_config_json_schema() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    schema = {"type": "object", "properties": {"n": {"type": "number"}}}
    provider = _make_provider()
    provider.complete(_basic_request(output_schema=schema))
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": schema},
    }


@respx.mock
def test_effort_wired_to_output_config_effort() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    provider.complete(_basic_request(effort="high"))
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body["output_config"] == {"effort": "high"}


@respx.mock
def test_output_schema_and_effort_merge_inside_output_config() -> None:
    """Both fields set: they merge inside output_config without overwriting each other."""
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    schema = {"type": "string"}
    provider = _make_provider()
    provider.complete(_basic_request(output_schema=schema, effort="xhigh"))
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": schema},
        "effort": "xhigh",
    }


@respx.mock
def test_thinking_wired_to_top_level_thinking() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    provider.complete(_basic_request(thinking="adaptive"))
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body["thinking"] == {"type": "adaptive"}


@respx.mock
def test_all_three_fields_none_omitted_from_body() -> None:
    """All three fields None: no related key appears in the body — existing behavior untouched."""
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    # Send a basic request (the new fields default to None).
    provider.complete(_basic_request(text="hi"))
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert "output_config" not in body
    assert "thinking" not in body


# ---------------------------------------------------------------------------
# Image support (vision-capable models) + non-vision degrade / guard
# ---------------------------------------------------------------------------
#
# A vision-capable model (catalog supports_vision=True) gets images on the wire:
# a top-level ``ImageBlock`` (user/assistant content) becomes an ``image`` block,
# and a ``ToolResultBlock``'s ``images`` ride the ``tool_result.content`` array.
# A non-vision model degrades tool-result images to string content; a top-level
# image bound for a non-vision model is a loud misroute → FatalError up front
# (no HTTP request sent). The default ``_basic_request`` model (``claude-opus-4-7``)
# is NOT in the catalog, so it counts as non-vision; ``claude-opus-4-8`` is the
# vision-capable model.

_IMG_REF = ContentRef(hash="sha256:img", size=3, media_type="image/png")
_VISION_MODEL = "claude-opus-4-8"
_PNG_BYTES = b"\x89PNG\r\n\x1a\nFAKEPNGDATA"


def _const_resolver(raw: bytes) -> Any:
    return lambda ref: raw


def test_image_block_to_anthropic_emits_base64_source() -> None:
    """Unit: the ImageBlock translator deref's via the resolver and emits the
    Anthropic base64 ``image`` content block with the ref's media_type."""
    from noeta.providers.anthropic import _image_block_to_anthropic

    block = ImageBlock(source=_IMG_REF)
    out = _image_block_to_anthropic(block, _const_resolver(_PNG_BYTES))
    assert out == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(_PNG_BYTES).decode("ascii"),
        },
    }


@respx.mock
def test_tool_result_with_images_vision_model_emits_image_content_array() -> None:
    """Vision model: a ToolResultBlock carrying images renders tool_result
    content as an ARRAY — a text block (the original string output) followed by
    one base64 image block per image."""
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider(image_resolver=_const_resolver(_PNG_BYTES))
    img = ImageBlock(source=_IMG_REF)
    tool_msg = Message(
        role="tool",
        content=[
            ToolResultBlock(
                call_id="c1",
                output="read chart.png (image, 12 bytes)",
                success=True,
                images=[img],
            )
        ],
    )
    provider.complete(
        _basic_request(model=_VISION_MODEL, messages=[_user("read it"), tool_msg])
    )

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    tr = body["messages"][1]["content"][0]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] == "c1"
    assert tr["is_error"] is False
    content = tr["content"]
    assert isinstance(content, list)
    assert content[0] == {
        "type": "text",
        "text": "read chart.png (image, 12 bytes)",
    }
    assert content[1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(_PNG_BYTES).decode("ascii"),
        },
    }


@respx.mock
def test_tool_result_with_images_non_vision_model_stays_string() -> None:
    """Non-vision model: tool-result images degrade — content stays the plain
    string (byte-identical to the image-less path), and the resolver is never
    invoked. The guard does NOT fire (the images are nested on the
    ToolResultBlock, not a top-level ImageBlock)."""
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    calls: list[Any] = []

    def resolver(ref: Any) -> bytes:
        calls.append(ref)
        return _PNG_BYTES

    provider = _make_provider(image_resolver=resolver)
    img = ImageBlock(source=_IMG_REF)
    tool_msg = Message(
        role="tool",
        content=[
            ToolResultBlock(
                call_id="c1", output="saw a chart", success=True, images=[img]
            )
        ],
    )
    # default model claude-opus-4-7 is not catalogued -> non-vision
    provider.complete(_basic_request(messages=[_user("read it"), tool_msg]))

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    tr = body["messages"][1]["content"][0]
    assert tr["content"] == "saw a chart"
    assert isinstance(tr["content"], str)
    assert calls == []


@respx.mock
def test_image_block_in_user_message_vision_model_translates() -> None:
    """Vision model: a top-level ImageBlock in a user message becomes an
    Anthropic base64 ``image`` content block, after the text block."""
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider(image_resolver=_const_resolver(_PNG_BYTES))
    request = _basic_request(
        model=_VISION_MODEL,
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="look at this"), ImageBlock(source=_IMG_REF)],
            )
        ],
    )
    provider.complete(request)

    assert route.called
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "look at this"
    img_block = content[1]
    assert img_block["type"] == "image"
    assert img_block["source"]["type"] == "base64"
    assert img_block["source"]["media_type"] == "image/png"
    assert img_block["source"]["data"] == base64.b64encode(_PNG_BYTES).decode("ascii")


@respx.mock
def test_image_block_in_assistant_message_vision_model_translates() -> None:
    """Vision model: a top-level ImageBlock in an assistant message is regrouped
    after text and before tool_use, as a base64 ``image`` content block."""
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider(image_resolver=_const_resolver(_PNG_BYTES))
    assistant = Message(
        role="assistant",
        content=[
            TextBlock(text="here"),
            ImageBlock(source=_IMG_REF),
            ToolUseBlock(call_id="t1", tool_name="echo", arguments={}),
        ],
    )
    provider.complete(
        _basic_request(model=_VISION_MODEL, messages=[_user("q"), assistant])
    )

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    types = [b["type"] for b in body["messages"][1]["content"]]
    assert types == ["text", "image", "tool_use"]


@respx.mock
def test_image_block_in_user_message_non_vision_model_guards_fatal() -> None:
    """Non-vision model + a top-level user ImageBlock is a loud misroute →
    FatalError before any HTTP request is sent."""
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider(image_resolver=_const_resolver(_PNG_BYTES))
    request = _basic_request(
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="look at this"), ImageBlock(source=_IMG_REF)],
            )
        ]
    )
    with pytest.raises(FatalError, match="not vision-capable"):
        provider.complete(request)
    assert not route.called


@respx.mock
def test_image_block_in_assistant_message_non_vision_model_guards_fatal() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider(image_resolver=_const_resolver(_PNG_BYTES))
    request = _basic_request(
        messages=[Message(role="assistant", content=[ImageBlock(source=_IMG_REF)])]
    )
    with pytest.raises(FatalError, match="not vision-capable"):
        provider.complete(request)
    assert not route.called


@respx.mock
def test_image_block_in_tool_message_non_vision_model_guards_fatal() -> None:
    """A top-level ImageBlock placed directly in a tool message is still a
    top-level image, so the vision guard catches it on a non-vision model."""
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider(image_resolver=_const_resolver(_PNG_BYTES))
    request = _basic_request(
        messages=[Message(role="tool", content=[ImageBlock(source=_IMG_REF)])]
    )
    with pytest.raises(FatalError, match="not vision-capable"):
        provider.complete(request)
    assert not route.called


@respx.mock
def test_image_block_vision_model_without_resolver_raises_loud() -> None:
    """Vision model but no image_resolver configured = incomplete config → a
    loud ValueError, never a silently dropped image."""
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()  # no image_resolver
    request = _basic_request(
        model=_VISION_MODEL,
        messages=[Message(role="user", content=[ImageBlock(source=_IMG_REF)])],
    )
    with pytest.raises(ValueError, match="no image_resolver configured"):
        provider.complete(request)
    assert not route.called


# ---------------------------------------------------------------------------
# prompt caching — cache_control breakpoints (#4)
# ---------------------------------------------------------------------------
#
# Ephemeral cache_control is stamped on the OUTBOUND wire body only — the last
# tool, the last message's last content block, and (lifting the flat string into
# block form) the system preamble. It must never reach LLMRequest / request_ref;
# the recorded canonical bytes stay provider-neutral and unchanged.


@respx.mock
def test_cache_control_stamped_on_system_tools_and_last_message() -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    sys = Message(role="system", content=[TextBlock(text="you are helpful")])
    tools = [
        {
            "type": "function",
            "function": {"name": "a", "parameters": {"type": "object"}},
        },
        {
            "type": "function",
            "function": {"name": "z", "parameters": {"type": "object"}},
        },
    ]
    provider.complete(
        _basic_request(
            system=sys,
            tools=tools,
            messages=[_user("first"), _user("last message")],
        )
    )

    body = json.loads(route.calls.last.request.content.decode("utf-8"))

    # system lifted to block form carrying the breakpoint
    assert body["system"] == [
        {
            "type": "text",
            "text": "you are helpful",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    # only the LAST tool is stamped
    assert "cache_control" not in body["tools"][0]
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    # only the LAST message's LAST block is stamped
    assert "cache_control" not in body["messages"][0]["content"][-1]
    assert body["messages"][-1]["content"][-1]["cache_control"] == {
        "type": "ephemeral"
    }
    # total breakpoints ≤ 4 (here: system + last tool + last message = 3)
    breakpoints = json.dumps(body).count('"cache_control"')
    assert breakpoints == 3


@respx.mock
def test_cache_control_omitted_when_no_system_and_no_tools() -> None:
    """No system, no tools: only the last message's last block is stamped —
    nothing crashes on the absent system/tools fields."""
    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    provider.complete(_basic_request(text="just a message", system=None, tools=[]))

    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert "system" not in body
    assert "tools" not in body
    assert body["messages"][-1]["content"][-1]["cache_control"] == {
        "type": "ephemeral"
    }
    assert json.dumps(body).count('"cache_control"') == 1


@respx.mock
def test_cache_control_does_not_enter_recorded_request_bytes() -> None:
    """Red line: cache_control lives only on the wire body. The recorded
    LLMRequest / request_ref canonical bytes (via the exact runtime
    serializer) must NOT contain it — recording stays byte-stable."""
    from noeta.runtime.llm import _serialize_request

    route = respx.post(MESSAGES_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_anthropic_response())
    )
    provider = _make_provider()
    sys = Message(role="system", content=[TextBlock(text="sys")])
    tools = [
        {
            "type": "function",
            "function": {"name": "t", "parameters": {"type": "object"}},
        }
    ]
    request = _basic_request(system=sys, tools=tools, text="hello")

    # Bytes that WOULD be recorded by RuntimeLLMClient, captured before/after
    # the wire body is built and sent.
    recorded_before = _serialize_request(request)
    provider.complete(request)
    recorded_after = _serialize_request(request)

    # The wire body DID carry cache_control...
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert json.dumps(body).count('"cache_control"') >= 2

    # ...but the recorded request bytes never do, and building the body did
    # not mutate the request object (byte-stable across the call).
    assert b"cache_control" not in recorded_before
    assert b"cache_control" not in recorded_after
    assert recorded_before == recorded_after

