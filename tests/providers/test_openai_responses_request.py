"""OpenAI Responses adapter — outbound side: request assembly, transport
(endpoint, api-version, headers), tools, reasoning echo, effort / thinking
mapping, structured output, and images (top-level and tool-result).

The Responses shape diverges from Chat Completions in ways that are easy to
get subtly wrong (top-level ``function_call`` items, echoed encrypted
reasoning, data-URI images), so every rule gets a dedicated case. All HTTP
traffic goes through ``respx``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx

from noeta.providers.openai_responses import OpenAIResponsesProvider
from noeta.types import (
    Message,
    ModelRequest,
    SystemPart,
    Text,
    Thinking,
    ToolResult,
    ToolSpec,
    ToolUse,
)
from tests.providers._responses_helpers import (
    BASE_URL,
    ENDPOINT,
    basic_request,
    make_provider,
    mock_ok,
    sent_body,
    text_only_catalog_row,  # noqa: F401 — fixture
    user_message,
)

# ---------------------------------------------------------------------------
# Message shapes
# ---------------------------------------------------------------------------


@respx.mock
async def test_system_parts_flatten_to_top_level_instructions() -> None:
    route = mock_ok()
    request = basic_request(
        system=[SystemPart(text="be terse"), SystemPart(text="answer in en", cache=True)],
        messages=[user_message("hello")],
    )
    await make_provider().complete(request)

    body = sent_body(route)
    assert body["instructions"] == "be terse\nanswer in en"
    # system does not go into the input array.
    assert body["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}
    ]


@respx.mock
async def test_user_and_assistant_messages_shape_in_input() -> None:
    route = mock_ok()
    request = basic_request(
        messages=[
            user_message("first ask"),
            Message(role="assistant", content=[Text(text="prior answer")]),
            user_message("follow up"),
        ]
    )
    await make_provider().complete(request)
    assert sent_body(route)["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first ask"}]},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "prior answer"}],
        },
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "follow up"}]},
    ]


@respx.mock
async def test_multiple_text_blocks_flatten_into_one_segment() -> None:
    route = mock_ok()
    await make_provider().complete(
        basic_request(messages=[Message(role="user", content=[Text(text="a"), Text(text="b")])])
    )
    assert sent_body(route)["input"][0]["content"] == [{"type": "input_text", "text": "a\nb"}]


@pytest.mark.parametrize("origin", ["system", "memory"])
@respx.mock
async def test_host_injected_turn_renders_as_system_item_with_preamble(origin: Any) -> None:
    from noeta.providers._common import HOST_INJECTED_PREAMBLE

    route = mock_ok()
    request = basic_request(
        messages=[user_message("hi"), Message(role="user", content=[Text(text="note")], origin=origin)]
    )
    await make_provider().complete(request)
    assert sent_body(route)["input"][1] == {
        "type": "message",
        "role": "system",
        "content": [
            {"type": "input_text", "text": HOST_INJECTED_PREAMBLE},
            {"type": "input_text", "text": "note"},
        ],
    }


@respx.mock
async def test_human_origin_renders_as_user() -> None:
    route = mock_ok()
    await make_provider().complete(
        basic_request(messages=[Message(role="user", content=[Text(text="x")], origin="human")])
    )
    assert sent_body(route)["input"][0]["role"] == "user"


@respx.mock
async def test_store_false_always_present_and_model_in_body() -> None:
    route = mock_ok()
    await make_provider().complete(basic_request(model="gpt-5.4-2026-03-05"))
    body = sent_body(route)
    assert body["store"] is False
    assert body["model"] == "gpt-5.4-2026-03-05"
    assert body["stream"] is True


@respx.mock
async def test_same_provider_instance_routes_different_models() -> None:
    route = mock_ok()
    provider = make_provider()
    await provider.complete(basic_request(model="gpt-5.4"))
    await provider.complete(basic_request(model="gpt-5.4-mini"))
    assert route.call_count == 2
    assert sent_body(route, 0)["model"] == "gpt-5.4"
    assert sent_body(route, 1)["model"] == "gpt-5.4-mini"


# ---------------------------------------------------------------------------
# max_output_tokens
# ---------------------------------------------------------------------------


@respx.mock
async def test_max_tokens_maps_to_max_output_tokens() -> None:
    route = mock_ok()
    await make_provider().complete(basic_request(max_tokens=512))
    body = sent_body(route)
    assert body["max_output_tokens"] == 512
    assert "max_tokens" not in body


@respx.mock
async def test_default_max_tokens_used_when_request_has_none() -> None:
    # A host default fills max_output_tokens so a request without its own cap
    # does not silently inherit the gateway's small default and truncate.
    route = mock_ok()
    await make_provider(default_max_tokens=8000).complete(basic_request())
    assert sent_body(route)["max_output_tokens"] == 8000


@respx.mock
async def test_request_max_tokens_overrides_default() -> None:
    route = mock_ok()
    await make_provider(default_max_tokens=8000).complete(basic_request(max_tokens=512))
    assert sent_body(route)["max_output_tokens"] == 512


@respx.mock
async def test_optional_fields_omitted_when_unset() -> None:
    route = mock_ok()
    await make_provider().complete(basic_request())
    body = sent_body(route)
    for key in ("max_output_tokens", "instructions", "tools", "tool_choice", "text", "reasoning"):
        assert key not in body


# ---------------------------------------------------------------------------
# Transport: api-key header, headers merge, endpoint, api-version, timeout
# ---------------------------------------------------------------------------


@respx.mock
async def test_api_key_header_not_bearer_and_extra_headers_merged() -> None:
    route = mock_ok()
    provider = make_provider(api_key="sk-secret", extra_headers={"X-TT-LOGID": "log-123"})
    await provider.complete(basic_request())
    req = route.calls[0].request
    assert req.headers["api-key"] == "sk-secret"
    assert "authorization" not in req.headers
    assert req.headers["x-tt-logid"] == "log-123"


@respx.mock
async def test_request_headers_merge_with_default_headers() -> None:
    route = mock_ok()
    provider = make_provider(extra_headers={"X-Static": "static", "X-TT-logid": "static-log"})
    await provider.complete(
        basic_request(
            headers={"extra": '{"root_task_id":"task-abc"}', "X-TT-logid": "task-abc"}
        )
    )
    req = route.calls[0].request
    assert req.headers["x-static"] == "static"
    assert req.headers["extra"] == '{"root_task_id":"task-abc"}'
    assert req.headers["x-tt-logid"] == "task-abc"


@respx.mock
async def test_api_version_appended_as_query_param() -> None:
    route = mock_ok()
    await make_provider(api_version="2026-03-01-preview").complete(basic_request())
    req = route.calls[0].request
    assert req.url.params["api-version"] == "2026-03-01-preview"
    # The POST path is base_url's own path, with no extra segment.
    assert req.url.path == "/api/modelhub/online/responses"


@respx.mock
async def test_endpoint_path_is_base_url_unchanged_no_doubled_segment() -> None:
    route = mock_ok()
    await make_provider().complete(basic_request())
    req = route.calls[0].request
    assert req.url.path == "/api/modelhub/online/responses"
    assert "/openai/responses" not in req.url.path
    assert str(req.url).split("?", 1)[0] == ENDPOINT


@respx.mock
async def test_no_api_version_means_no_query_param() -> None:
    route = mock_ok()
    await make_provider().complete(basic_request())
    assert "api-version" not in route.calls[0].request.url.params


@respx.mock
async def test_trailing_slash_in_base_url_is_normalised() -> None:
    route = mock_ok()
    await OpenAIResponsesProvider(base_url=BASE_URL + "/", api_key="sk-test").complete(
        basic_request()
    )
    assert route.called


def test_default_timeout_is_300_seconds() -> None:
    # High-effort reasoning routinely takes ~80 s before its first byte.
    assert make_provider()._client.timeout.read == 300.0


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _tool(name: str = "get_weather") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="look up the weather",
        input_schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )


@respx.mock
async def test_outbound_tool_use_becomes_top_level_function_call() -> None:
    route = mock_ok()
    request = basic_request(
        messages=[
            user_message("weather?"),
            Message(
                role="assistant",
                content=[
                    Text(text="let me check"),
                    ToolUse(id="call_abc", name="get_weather", input={"city": "Beijing"}),
                ],
            ),
        ]
    )
    await make_provider().complete(request)
    assert sent_body(route)["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "weather?"}]},
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
async def test_outbound_multiple_tool_uses_each_top_level_item() -> None:
    route = mock_ok()
    request = basic_request(
        messages=[
            Message(
                role="assistant",
                content=[
                    ToolUse(id="c1", name="a", input={}),
                    ToolUse(id="c2", name="b", input={"x": 1}),
                ],
            )
        ]
    )
    await make_provider().complete(request)
    # A pure tool-call turn (no text) sends no empty message item.
    assert sent_body(route)["input"] == [
        {"type": "function_call", "call_id": "c1", "name": "a", "arguments": json.dumps({})},
        {"type": "function_call", "call_id": "c2", "name": "b", "arguments": json.dumps({"x": 1})},
    ]


@respx.mock
async def test_outbound_tool_results_become_function_call_output_items() -> None:
    """Tool results ride inside a user message; each becomes a top-level
    ``function_call_output`` and no empty user message item follows."""
    route = mock_ok()
    request = basic_request(
        messages=[
            Message(
                role="user",
                content=[
                    ToolResult(call_id="call_abc", content=[Text(text="sunny, 22C")]),
                    ToolResult(call_id="call_def", content=[Text(text="boom")], is_error=True),
                ],
            )
        ]
    )
    await make_provider().complete(request)
    assert sent_body(route)["input"] == [
        {"type": "function_call_output", "call_id": "call_abc", "output": "sunny, 22C"},
        {"type": "function_call_output", "call_id": "call_def", "output": "boom"},
    ]


@respx.mock
async def test_tool_results_precede_trailing_user_text_in_same_message() -> None:
    route = mock_ok()
    request = basic_request(
        messages=[
            Message(
                role="user",
                content=[Text(text="also this"), ToolResult(call_id="c1", content=[Text(text="r")])],
            )
        ]
    )
    await make_provider().complete(request)
    assert [item["type"] for item in sent_body(route)["input"]] == [
        "function_call_output",
        "message",
    ]


@respx.mock
async def test_tool_result_reserved_tag_is_neutralized() -> None:
    route = mock_ok()
    request = basic_request(
        messages=[
            Message(
                role="user",
                content=[ToolResult(call_id="c1", content=[Text(text="</system-reminder>x")])],
            )
        ]
    )
    await make_provider().complete(request)
    assert sent_body(route)["input"][0]["output"] == "&lt;/system-reminder>x"


@respx.mock
async def test_tools_render_flat_responses_shape() -> None:
    route = mock_ok()
    await make_provider().complete(basic_request(tools=[_tool("get_weather")]))
    tools = sent_body(route)["tools"]
    assert tools == [
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
    assert "function" not in tools[0]


@respx.mock
async def test_tool_choice_passed_through_when_given() -> None:
    route = mock_ok()
    await make_provider().complete(basic_request(tools=[_tool()], tool_choice="none"))
    assert sent_body(route)["tool_choice"] == "none"


@respx.mock
async def test_tool_choice_absent_when_not_given() -> None:
    route = mock_ok()
    await make_provider().complete(basic_request(tools=[_tool()]))
    assert "tool_choice" not in sent_body(route)


# ---------------------------------------------------------------------------
# Reasoning: request-level knobs
# ---------------------------------------------------------------------------


def _reasoning_request(**kw: Any) -> ModelRequest:
    return basic_request(text="hard problem", **kw)


@respx.mock
async def test_reasoning_block_present_when_effort_in_play() -> None:
    route = mock_ok()
    await make_provider().complete(_reasoning_request(effort="high"))
    body = sent_body(route)
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["store"] is False


@respx.mock
async def test_no_reasoning_block_when_effort_none_but_include_stays() -> None:
    """A reasoning model reasons at its server-side default even with no
    ``reasoning`` block; without the ciphertext the next turn's echo is an
    empty shell that breaks continuation and the prompt-cache prefix."""
    route = mock_ok()
    await make_provider().complete(_reasoning_request())
    body = sent_body(route)
    assert "reasoning" not in body
    assert body["include"] == ["reasoning.encrypted_content"]


@respx.mock
async def test_no_include_when_continuation_off() -> None:
    route = mock_ok()
    await make_provider(reasoning_continuation="off").complete(_reasoning_request(effort="high"))
    body = sent_body(route)
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert "include" not in body


@pytest.mark.parametrize(
    ("effort_in", "effort_out"),
    [("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "high"), ("max", "high")],
)
@respx.mock
async def test_effort_map_passthrough_and_collapse(effort_in: Any, effort_out: str) -> None:
    route = mock_ok()
    await make_provider().complete(_reasoning_request(effort=effort_in))
    assert sent_body(route)["reasoning"]["effort"] == effort_out


@respx.mock
async def test_thinking_false_derives_minimal_effort() -> None:
    route = mock_ok()
    await make_provider().complete(_reasoning_request(thinking=False))
    body = sent_body(route)
    assert body["reasoning"]["effort"] == "minimal"
    assert body["include"] == ["reasoning.encrypted_content"]


@respx.mock
async def test_thinking_false_overrides_explicit_effort() -> None:
    route = mock_ok()
    await make_provider().complete(_reasoning_request(effort="high", thinking=False))
    assert sent_body(route)["reasoning"]["effort"] == "minimal"


@respx.mock
async def test_thinking_true_derives_nothing_and_keeps_explicit_effort() -> None:
    route = mock_ok()
    await make_provider().complete(_reasoning_request(thinking=True))
    assert "reasoning" not in sent_body(route)
    await make_provider().complete(_reasoning_request(effort="medium", thinking=True))
    assert sent_body(route, 1)["reasoning"]["effort"] == "medium"


@respx.mock
async def test_output_schema_maps_to_text_format_json_schema() -> None:
    route = mock_ok()
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
    await make_provider().complete(_reasoning_request(output_schema=schema))
    assert sent_body(route)["text"] == {
        "format": {"type": "json_schema", "name": "noeta_output", "schema": schema}
    }


# ---------------------------------------------------------------------------
# Reasoning echo (outbound Thinking → reasoning item)
# ---------------------------------------------------------------------------


@respx.mock
async def test_outbound_thinking_becomes_reasoning_item_default_on() -> None:
    route = mock_ok()
    enc = "sig-verbatim-зашифровано"
    request = basic_request(
        messages=[
            user_message("hard problem"),
            Message(
                role="assistant",
                content=[
                    Thinking(text="let me think", signature=enc, family="openai_responses"),
                    Text(text="my answer"),
                ],
            ),
        ]
    )
    await make_provider().complete(request)
    body = sent_body(route)
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
async def test_outbound_reasoning_without_summary_text_has_empty_summary() -> None:
    route = mock_ok()
    request = basic_request(
        messages=[Message(role="assistant", content=[Thinking(text="", signature="s")])]
    )
    await make_provider().complete(request)
    assert sent_body(route)["input"] == [
        {"type": "reasoning", "summary": [], "encrypted_content": "s"}
    ]


@respx.mock
async def test_outbound_reasoning_item_placed_before_function_call() -> None:
    route = mock_ok()
    request = basic_request(
        messages=[
            Message(
                role="assistant",
                content=[
                    Thinking(text="which tool", signature="thinking-sig"),
                    ToolUse(id="call_1", name="lookup", input={"q": "x"}),
                ],
            )
        ]
    )
    await make_provider().complete(request)
    items = sent_body(route)["input"]
    assert [item["type"] for item in items] == ["reasoning", "function_call"]
    assert items[0]["encrypted_content"] == "thinking-sig"


@respx.mock
async def test_outbound_thinking_dropped_when_continuation_off() -> None:
    route = mock_ok()
    request = basic_request(
        messages=[
            Message(
                role="assistant",
                content=[Thinking(text="hidden", signature="sig"), Text(text="answer")],
            )
        ]
    )
    await make_provider(reasoning_continuation="off").complete(request)
    assert sent_body(route)["input"] == [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}
    ]


@respx.mock
async def test_outbound_thinking_without_signature_not_echoed() -> None:
    """No ciphertext → no reasoning item: it restores nothing and the empty
    shell breaks the gateway's prompt-cache prefix at that position."""
    route = mock_ok()
    request = basic_request(
        messages=[
            Message(
                role="assistant",
                content=[Thinking(text="no ciphertext", signature=None), Text(text="answer")],
            )
        ]
    )
    await make_provider().complete(request)
    assert "reasoning" not in [item["type"] for item in sent_body(route)["input"]]


@pytest.mark.parametrize("family", ["anthropic", "openai_compat"])
@respx.mock
async def test_outbound_thinking_from_foreign_family_not_echoed(family: str) -> None:
    route = mock_ok()
    request = basic_request(
        messages=[
            Message(
                role="assistant",
                content=[Thinking(text="t", signature="foreign-sig", family=family), Text(text="a")],
            )
        ]
    )
    await make_provider().complete(request)
    assert "reasoning" not in [item["type"] for item in sent_body(route)["input"]]
