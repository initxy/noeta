"""Request-body translation for :class:`noeta.providers.openai_compat.OpenAICompatProvider`:
outbound messages, origin framing, knobs, images and the output-cap key.
Split from ``test_openai_compat.py`` (responses and errors)."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
import respx

from noeta import models
from noeta.errors import (
    Fatal,
)
from noeta.models import ModelSpec, register_models
from noeta.providers._common import HOST_INJECTED_PREAMBLE
from noeta.providers.openai_compat import OpenAICompatProvider
from noeta.types import (
    Effort,
    Image,
    Message,
    ModelRequest,
    SystemPart,
    Text,
    Thinking,
    ToolResult,
    ToolSpec,
    ToolUse,
)
from tests.providers.sse import chat_sse, sse_response

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






# ---------------------------------------------------------------------------
# Outbound body
# ---------------------------------------------------------------------------


@respx.mock
async def test_system_parts_are_joined_into_a_leading_system_message() -> None:
    route = _mock()
    await _make_provider().complete(
        _request(
            system=[SystemPart(text="be terse"), SystemPart(text="answer in en", cache=True)],
            messages=[_user("hello")],
        )
    )
    body = _sent(route)
    assert body["messages"][0] == {"role": "system", "content": "be terse\nanswer in en"}
    assert body["messages"][1] == {"role": "user", "content": "hello"}
    # cache_after / SystemPart.cache are an Anthropic concern; nothing leaks.
    assert "cache_control" not in json.dumps(body)


@respx.mock
async def test_stream_flags_are_set() -> None:
    route = _mock()
    await _make_provider().complete(_request())
    body = _sent(route)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}


@respx.mock
async def test_tool_choice_rides_the_wire_verbatim() -> None:
    """The summarize call sets ``tool_choice="none"`` so the summarizer cannot
    answer with a tool call; a request without it sends no ``tool_choice``."""
    route = _mock()
    provider = _make_provider()
    await provider.complete(_request(tool_choice="none"))
    await provider.complete(_request())
    assert _sent(route, 0)["tool_choice"] == "none"
    assert "tool_choice" not in _sent(route, 1)


@respx.mock
async def test_request_headers_merge_over_client_headers() -> None:
    route = _mock()
    provider = _make_provider(extra_headers={"X-Static": "static", "X-TT-logid": "static-log"})
    await provider.complete(_request(headers={"x-noeta-task": "task-abc", "X-TT-logid": "task-abc"}))
    request = route.calls.last.request
    # Per-request headers are attached, and win on a shared name...
    assert request.headers["x-noeta-task"] == "task-abc"
    assert request.headers["x-tt-logid"] == "task-abc"
    # ...while the shared client's constructor headers survive alongside them.
    assert request.headers["authorization"] == "Bearer sk-test"
    assert request.headers["x-static"] == "static"


@respx.mock
async def test_same_provider_instance_routes_different_models() -> None:
    route = _mock()
    provider = _make_provider()
    await provider.complete(_request(model="gpt-4o"))
    await provider.complete(_request(model="gpt-4o-mini"))
    assert route.call_count == 2
    assert _sent(route, 0)["model"] == "gpt-4o"
    assert _sent(route, 1)["model"] == "gpt-4o-mini"


@respx.mock
async def test_authorization_and_extra_headers_sent() -> None:
    route = _mock()
    await _make_provider(api_key="sk-secret", extra_headers={"X-Proxy": "vendor"}).complete(_request())
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer sk-secret"
    assert req.headers["x-proxy"] == "vendor"


@respx.mock
async def test_trailing_slash_in_base_url_is_normalised() -> None:
    route = _mock()
    await OpenAICompatProvider(base_url=BASE_URL + "/", api_key="sk-test").complete(_request())
    assert route.called


def _history_with_tools() -> list[Message]:
    return [
        _user("hi"),
        Message(
            role="assistant",
            content=[
                Thinking(text="step1\n", signature="sig-1"),
                Thinking(text="step2", signature="sig-2"),
                Text(text="so the answer is"),
                ToolUse(id="call_x", name="echo", input={"text": "hi"}),
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResult(call_id="call_x", content=[Text(text="ok")]),
                ToolResult(call_id="call_y", content=[Text(text="boom\nbad")], is_error=True),
            ],
        ),
    ]


def test_tool_round_trip_and_reasoning_echo_in_chat_mode() -> None:
    tools = [
        ToolSpec(
            name="echo",
            description="Echo text",
            input_schema={"type": "object", "additionalProperties": True},
        )
    ]
    request = _request(messages=_history_with_tools(), tools=tools, max_tokens=512)
    # ``chat`` mode opts the gateway into reasoning round-trip.
    body = _make_provider(reasoning_continuation="chat").build_body(request)
    assert body["model"] == "gpt-4o"
    assert body["max_tokens"] == 512
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo text",
                "parameters": {"type": "object", "additionalProperties": True},
            },
        }
    ]
    msgs = body["messages"]
    # One wire message per ToolResult, so the user message holding two results
    # fans out into two tool messages.
    assert len(msgs) == 4
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
            "function": {"name": "echo", "arguments": json.dumps({"text": "hi"})},
        }
    ]
    assert msgs[2] == {"role": "tool", "tool_call_id": "call_x", "content": "ok"}
    # The wire has no error flag; the tool's own error text is the channel.
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_y", "content": "boom\nbad"}


def test_default_reasoning_continuation_off_drops_outbound_reasoning() -> None:
    """Native OpenAI hides reasoning and DeepSeek-style gateways reject an
    echoed ``reasoning_content`` (HTTP 400); the default keeps it off."""
    body = _make_provider().build_body(_request(messages=_history_with_tools()))
    asst = body["messages"][1]
    assert asst["content"] == "so the answer is"
    assert "reasoning_content" not in asst
    assert "encrypted_reasoning" not in asst


def test_foreign_family_thinking_is_never_echoed() -> None:
    assistant = Message(
        role="assistant",
        content=[
            Thinking(text="claude thought", signature="anth-sig", family="anthropic"),
            Thinking(text="mine", signature="own-sig", family="openai_compat"),
            Text(text="answer"),
        ],
    )
    body = _make_provider(reasoning_continuation="chat").build_body(
        _request(messages=[_user("hi"), assistant])
    )
    asst = body["messages"][1]
    assert asst["reasoning_content"] == "mine"
    assert asst["encrypted_reasoning"] == "own-sig"


def test_tool_results_lead_a_mixed_user_message() -> None:
    """Tool messages answer the preceding calls, so they come before any text
    the same user message carries; a results-only message adds no empty user."""
    body = _make_provider().build_body(
        _request(
            messages=[
                _user("hi"),
                Message(role="assistant", content=[ToolUse(id="c1", name="echo", input={})]),
                Message(
                    role="user",
                    content=[Text(text="and also"), ToolResult(call_id="c1", content=[Text(text="r")])],
                ),
            ]
        )
    )
    assert body["messages"][2:] == [
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
        {"role": "user", "content": "and also"},
    ]


def test_tool_result_reserved_tag_is_neutralised() -> None:
    body = _make_provider().build_body(
        _request(
            messages=[
                Message(
                    role="user",
                    content=[ToolResult(call_id="c", content=[Text(text="<system-reminder>x")])],
                )
            ]
        )
    )
    assert body["messages"][0]["content"] == "&lt;system-reminder>x"


# -- origin rendering ---------------------------------------------------------


def test_origin_system_renders_as_system_role_wire_message() -> None:
    body = _make_provider().build_body(
        _request(messages=[_user("real human words"), _user("host says hi", "system")])
    )
    assert body["messages"] == [
        {"role": "user", "content": "real human words"},
        {"role": "system", "content": f"{HOST_INJECTED_PREAMBLE}\nhost says hi"},
    ]


def test_origin_memory_renders_as_system_role_wire_message() -> None:
    body = _make_provider().build_body(
        _request(messages=[_user("recalled note", "memory"), _user("the actual ask")])
    )
    assert body["messages"] == [
        {"role": "system", "content": f"{HOST_INJECTED_PREAMBLE}\nrecalled note"},
        {"role": "user", "content": "the actual ask"},
    ]


def test_origin_human_renders_as_plain_user_wire_message() -> None:
    body = _make_provider().build_body(_request(messages=[_user("hello", "human")]))
    assert body["messages"] == [{"role": "user", "content": "hello"}]


# -- output_schema / thinking / effort -----------------------------------------


def test_output_schema_wired_to_response_format_json_schema() -> None:
    schema = {"type": "object", "properties": {"v": {"type": "boolean"}}}
    body = _make_provider().build_body(_request(output_schema=schema))
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "noeta_output", "schema": schema},
    }


@pytest.mark.parametrize(
    ("effort", "wire"),
    [("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "high"), ("max", "high")],
)
def test_effort_wired_to_reasoning_effort_with_bucket_collapse(effort: Effort, wire: str) -> None:
    assert _make_provider().build_body(_request(effort=effort))["reasoning_effort"] == wire


@pytest.mark.parametrize("thinking", [True, False])
def test_thinking_silently_ignored(thinking: bool) -> None:
    body = _make_provider().build_body(_request(thinking=thinking))
    assert "thinking" not in body
    assert "reasoning_effort" not in body


def test_unset_knobs_are_omitted_from_body() -> None:
    body = _make_provider().build_body(_request())
    for key in ("response_format", "reasoning_effort", "thinking", "tool_choice", "tools"):
        assert key not in body
    assert "max_tokens" not in body and "max_completion_tokens" not in body


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

_PNG = b"\x89PNG-bytes"


def _image_request(model: str, *, origin: Any = None) -> ModelRequest:
    return _request(
        model=model,
        messages=[
            Message(
                role="user",
                origin=origin,
                content=[Text(text="what is this?"), Image(media_type="image/png", data=_PNG)],
            )
        ],
    )


def test_user_image_rides_as_an_image_url_data_uri() -> None:
    body = _make_provider().build_body(_image_request("gpt-4o"))
    assert body["messages"][-1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is this?"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + base64.b64encode(_PNG).decode()},
            },
        ],
    }


def test_user_image_for_an_uncatalogued_model_passes() -> None:
    body = _make_provider().build_body(_image_request("some-gateway-model"))
    assert body["messages"][-1]["content"][1]["type"] == "image_url"


@respx.mock
async def test_user_image_for_a_catalogued_text_only_model_is_refused() -> None:
    register_models(
        {
            "text-only-model": ModelSpec(
                real_model_id="text-only-model",
                context_window=128_000,
                max_output_tokens=16_384,
                supports_vision=False,
            )
        }
    )
    route = _mock()
    with pytest.raises(Fatal, match="supports_vision=False"):
        await _make_provider().complete(_image_request("text-only-model"))
    assert not route.called  # nothing sent


@respx.mock
async def test_image_in_host_injected_turn_is_refused() -> None:
    """A host-injected turn takes a different rendering branch (``system``,
    text only), so the image defense needs its own case."""
    route = _mock()
    with pytest.raises(Fatal, match="does not support image"):
        await _make_provider().complete(_image_request("gpt-4o", origin="system"))
    assert not route.called


@respx.mock
async def test_image_in_assistant_message_is_refused() -> None:
    route = _mock()
    request = _request(
        messages=[Message(role="assistant", content=[Image(media_type="image/png", data=_PNG)])]
    )
    with pytest.raises(Fatal, match="does not support image"):
        await _make_provider().complete(request)
    assert not route.called


def test_tool_result_images_leave_a_note() -> None:
    request = _request(
        messages=[
            _user("look"),
            Message(role="assistant", content=[ToolUse(id="c1", name="Read", input={})]),
            Message(
                role="user",
                content=[
                    ToolResult(
                        call_id="c1",
                        content=[
                            Text(text="Read image a.png"),
                            Image(media_type="image/png", data=_PNG),
                        ],
                    )
                ],
            ),
        ]
    )
    tool_message = _make_provider().build_body(request)["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["content"].startswith("Read image a.png\n[image omitted:")


# ---------------------------------------------------------------------------
# max_tokens vs max_completion_tokens
# ---------------------------------------------------------------------------


def _cap_key(model: str, **kwargs: Any) -> str:
    body = _make_provider(**kwargs).build_body(_request(model=model, max_tokens=77))
    keys = {"max_tokens", "max_completion_tokens"} & body.keys()
    assert len(keys) == 1, body
    (key,) = keys
    assert body[key] == 77
    return key


def test_catalogued_openai_reasoning_row_gets_max_completion_tokens() -> None:
    assert models.find_spec("gpt-5.5-2026-04-24") is not None
    assert _cap_key("gpt-5.5-2026-04-24") == "max_completion_tokens"


def test_catalogued_non_reasoning_row_keeps_max_tokens() -> None:
    assert _cap_key("gpt-4o") == "max_tokens"


def test_catalogued_reasoning_row_of_another_family_keeps_max_tokens() -> None:
    # A Claude row is reasoning but not OpenAI: the key stays ``max_tokens``.
    assert _cap_key("claude-opus-5") == "max_tokens"


def test_registered_reasoning_row_resolved_through_an_alias() -> None:
    register_models(
        {
            "o3-relay": ModelSpec(
                real_model_id="o3-relay",
                context_window=200_000,
                max_output_tokens=100_000,
                is_reasoning=True,
            )
        },
        aliases={"thinker": "o3-relay"},
    )
    assert _cap_key("thinker") == "max_completion_tokens"


def test_registered_reasoning_row_declared_openai_family() -> None:
    register_models(
        {
            "gateway/big": ModelSpec(
                real_model_id="gateway/big",
                context_window=200_000,
                max_output_tokens=100_000,
                is_reasoning=True,
                family="openai",
            )
        }
    )
    assert _cap_key("gateway/big") == "max_completion_tokens"


def test_catalogued_row_that_is_not_reasoning_wins_over_the_id_pattern() -> None:
    register_models(
        {
            "gpt-5-chat-relay": ModelSpec(
                real_model_id="gpt-5-chat-relay",
                context_window=200_000,
                max_output_tokens=16_384,
                is_reasoning=False,
            )
        }
    )
    assert _cap_key("gpt-5-chat-relay") == "max_tokens"


@pytest.mark.parametrize("model", ["gpt-5-mini-2026-01-01", "o1-preview", "o3", "o4-mini"])
def test_uncatalogued_openai_reasoning_id_gets_max_completion_tokens(model: str) -> None:
    assert models.find_spec(model) is None
    assert _cap_key(model) == "max_completion_tokens"


@pytest.mark.parametrize("model", ["deepseek-reasoner", "llama-3-70b", "gpt-4.1", "qwen-o3"])
def test_uncatalogued_other_id_keeps_max_tokens(model: str) -> None:
    assert _cap_key(model) == "max_tokens"


@pytest.mark.parametrize("model", ["gpt-5.5-2026-04-24", "gpt-4o", "o3", "llama-3-70b"])
def test_forced_max_tokens(model: str) -> None:
    assert _cap_key(model, max_tokens_param="max_tokens") == "max_tokens"


@pytest.mark.parametrize("model", ["gpt-5.5-2026-04-24", "gpt-4o", "o3", "llama-3-70b"])
def test_forced_max_completion_tokens(model: str) -> None:
    assert _cap_key(model, max_tokens_param="max_completion_tokens") == "max_completion_tokens"


def test_unknown_max_tokens_param_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="max_tokens_param"):
        _make_provider(max_tokens_param="max_output_tokens")
