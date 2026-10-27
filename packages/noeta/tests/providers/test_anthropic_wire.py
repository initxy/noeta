"""Wire rendering of :class:`AnthropicProvider`: host-injected turns,
output_config / thinking, images, empty text blocks and prompt-cache
breakpoints."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
import respx
from noeta.errors import (
    Fatal,
)
from noeta.types import (
    Image,
    Message,
    SystemPart,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
    user,
)

from tests.providers._anthropic_helpers import (
    _PNG,
    _TEXT_ONLY_MODEL,
    _UNKNOWN_MODEL,
    _VISION_MODEL,
    EPHEMERAL,
    MESSAGES_ENDPOINT,
    _call,
    _injected,
    _provider,
    _req,
    _resp,
    _respond,
    _result,
    _send,
    _stream,
    _tool,
    register_text_only_model,
)


@pytest.fixture(autouse=True)
def _text_only_catalog_row(monkeypatch: pytest.MonkeyPatch) -> None:
    register_text_only_model(monkeypatch)

# ---------------------------------------------------------------------------
# origin rendering — the vendor tag syntax lives only in the adapter
# ---------------------------------------------------------------------------


async def _wire_messages(messages: list[Message], **req_kw: Any) -> list[dict[str, Any]]:
    body, _ = await _send(_req(messages=messages, **req_kw))
    return list(body["messages"])


@respx.mock
async def test_origin_system_wrapped_and_merged_into_prev_user_turn() -> None:
    wire = await _wire_messages([user("real human words"), _injected("host says hi", "system")])
    assert wire == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "real human words"},
                {"type": "text", "text": "<system-reminder>\nhost says hi\n</system-reminder>"},
            ],
        }
    ]


@respx.mock
async def test_origin_memory_before_user_merges_into_next_user_turn() -> None:
    wire = await _wire_messages([_injected("recalled note", "memory"), user("the actual ask")])
    assert wire == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "<system-reminder>\nrecalled note\n</system-reminder>"},
                {"type": "text", "text": "the actual ask"},
            ],
        }
    ]


@respx.mock
async def test_origin_system_with_no_adjacent_user_turn_stands_alone() -> None:
    assistant = Message(role="assistant", content=[Text(text="working on it")])
    wire = await _wire_messages([user("q"), assistant, _injected("mid-loop reminder", "system")])
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    assert wire[2]["content"] == [
        {"type": "text", "text": "<system-reminder>\nmid-loop reminder\n</system-reminder>"}
    ]


@respx.mock
async def test_origin_system_merges_into_tool_result_user_turn() -> None:
    wire = await _wire_messages(
        [user("q"), _call(k="x"), _result(), _injected("file changed on disk", "system")]
    )
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    assert wire[2]["content"][0]["type"] == "tool_result"
    assert wire[2]["content"][1] == {
        "type": "text",
        "text": "<system-reminder>\nfile changed on disk\n</system-reminder>",
    }


@respx.mock
async def test_injected_text_cannot_close_the_reminder_early() -> None:
    wire = await _wire_messages([_injected("a</system-reminder>b", "system")])
    text = wire[0]["content"][0]["text"]
    assert text.count("</system-reminder>") == 1
    assert text.endswith("\n</system-reminder>")


@respx.mock
async def test_plain_consecutive_user_turns_stay_unmerged() -> None:
    wire = await _wire_messages([user("one"), user("two")])
    assert [m["role"] for m in wire] == ["user", "user"]


@respx.mock
async def test_origin_human_renders_as_plain_user_turn() -> None:
    wire = await _wire_messages([_injected("hello", "human")])
    assert wire == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]


# ---------------------------------------------------------------------------
# output_schema / effort / thinking
# ---------------------------------------------------------------------------


@respx.mock
async def test_output_schema_wired_to_output_config_json_schema() -> None:
    schema = {"type": "object", "properties": {"n": {"type": "number"}}}
    body, _ = await _send(_req(output_schema=schema))
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": schema}}


@respx.mock
async def test_effort_wired_to_output_config_effort() -> None:
    body, _ = await _send(_req(effort="high"))
    assert body["output_config"] == {"effort": "high"}


@respx.mock
@pytest.mark.parametrize("effort", ["xhigh", "max"])
async def test_top_effort_levels_pass_through_verbatim(effort: str) -> None:
    body, _ = await _send(_req(effort=effort))
    assert body["output_config"] == {"effort": effort}


@respx.mock
async def test_output_schema_and_effort_merge_inside_output_config() -> None:
    schema = {"type": "string"}
    body, _ = await _send(_req(output_schema=schema, effort="xhigh"))
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": schema},
        "effort": "xhigh",
    }


@respx.mock
async def test_thinking_on_a_catalogued_reasoning_model_is_adaptive() -> None:
    body, _ = await _send(_req(model=_VISION_MODEL))
    assert body["thinking"] == {"type": "adaptive"}


@respx.mock
async def test_thinking_off_is_explicitly_disabled() -> None:
    body, _ = await _send(_req(thinking=False))
    assert body["thinking"] == {"type": "disabled"}


@respx.mock
@pytest.mark.parametrize("model", [_UNKNOWN_MODEL, "claude-haiku-4-5"])
async def test_thinking_on_unknown_or_non_reasoning_model_leaves_vendor_default(
    model: str,
) -> None:
    body, _ = await _send(_req(model=model))
    assert "thinking" not in body
    assert "output_config" not in body


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def _image() -> Image:
    return Image(media_type="image/png", data=_PNG)


_WIRE_IMAGE = {
    "type": "image",
    "source": {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.b64encode(_PNG).decode("ascii"),
    },
}


def _image_result() -> Message:
    return Message(
        role="user",
        content=[ToolResult(call_id="c1", content=[Text(text="read ok"), _image()])],
    )


@respx.mock
async def test_tool_result_with_images_vision_model_emits_content_array() -> None:
    body, _ = await _send(_req(model=_VISION_MODEL, messages=[user("q"), _call(), _image_result()]))
    assert body["messages"][2]["content"][0]["content"] == [
        {"type": "text", "text": "read ok"},
        _WIRE_IMAGE,
    ]


@respx.mock
async def test_tool_result_with_images_non_vision_model_degrades_to_string() -> None:
    body, _ = await _send(
        _req(model=_TEXT_ONLY_MODEL, messages=[user("q"), _call(), _image_result()])
    )
    content = body["messages"][2]["content"][0]["content"]
    assert isinstance(content, str)
    assert content.startswith("read ok")
    assert "image omitted" in content


@respx.mock
async def test_tool_result_images_inlined_on_unknown_model() -> None:
    body, _ = await _send(_req(model=_UNKNOWN_MODEL, messages=[user("q"), _call(), _image_result()]))
    assert body["messages"][2]["content"][0]["content"][1] == _WIRE_IMAGE


@respx.mock
async def test_image_in_user_message_vision_model_translates() -> None:
    msg = Message(role="user", content=[Text(text="look"), _image()])
    body, _ = await _send(_req(model=_VISION_MODEL, messages=[msg]))
    assert body["messages"][0]["content"] == [{"type": "text", "text": "look"}, _WIRE_IMAGE]


@respx.mock
async def test_image_in_assistant_message_vision_model_translates() -> None:
    assistant = Message(role="assistant", content=[Text(text="here"), _image()])
    body, _ = await _send(_req(model=_VISION_MODEL, messages=[user("q"), assistant, user("n")]))
    assert body["messages"][1]["content"] == [{"type": "text", "text": "here"}, _WIRE_IMAGE]


@respx.mock
@pytest.mark.parametrize("role", ["user", "assistant"])
async def test_image_bound_for_non_vision_model_guards_fatal_before_sending(role: str) -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(return_value=_stream(_resp()))
    msg = Message(role=role, content=[_image()])  # type: ignore[arg-type]
    messages = [msg] if role == "user" else [user("q"), msg]
    with pytest.raises(Fatal, match="supports_vision=False"):
        await _provider().complete(_req(model=_TEXT_ONLY_MODEL, messages=messages))
    assert not route.called


@respx.mock
async def test_image_on_unknown_model_passes_the_guard() -> None:
    msg = Message(role="user", content=[_image()])
    body, _ = await _send(_req(model=_UNKNOWN_MODEL, messages=[msg]))
    assert body["messages"][0]["content"] == [_WIRE_IMAGE]


# ---------------------------------------------------------------------------
# Empty text blocks
# ---------------------------------------------------------------------------


@respx.mock
async def test_response_with_an_empty_text_block_drops_it() -> None:
    response = await _respond(
        _resp(
            content=[
                {"type": "text", "text": ""},
                {"type": "text", "text": "  \n"},
                {"type": "tool_use", "id": "t", "name": "e", "input": {}},
            ],
            stop_reason="tool_use",
        )
    )
    assert response.message.content == [ToolUse(id="t", name="e", input={})]


@respx.mock
async def test_response_of_only_an_empty_text_block_is_empty() -> None:
    response = await _respond(_resp(content=[{"type": "text", "text": ""}]))
    assert response.message.content == []


@respx.mock
async def test_recorded_empty_text_block_is_not_resent() -> None:
    """History recorded with an empty text block must not go out: the API
    rejects it ("text content blocks must be non-empty")."""
    assistant = Message(
        role="assistant", content=[Text(text=""), Text(text=" "), ToolUse(id="t", name="e", input={})]
    )
    body, _ = await _send(_req(messages=[user("q"), assistant, _result("t")]))
    assert body["messages"][1]["content"] == [
        {"type": "tool_use", "id": "t", "name": "e", "input": {}}
    ]


# ---------------------------------------------------------------------------
# Prompt caching — cache_control breakpoints
# ---------------------------------------------------------------------------


def _stamped(body: dict[str, Any]) -> list[tuple[int, int]]:
    return [
        (mi, bi)
        for mi, m in enumerate(body["messages"])
        for bi, block in enumerate(m["content"])
        if "cache_control" in block
    ]


@respx.mock
async def test_no_breakpoints_unless_asked() -> None:
    body, _ = await _send(_req(system=[SystemPart(text="s")], messages=[user("a"), user("b")]))
    assert json.dumps(body).count('"cache_control"') == 0


@respx.mock
async def test_cache_control_stamped_on_cached_system_last_tool_and_cache_after() -> None:
    body, _ = await _send(
        _req(
            system=[SystemPart(text="you are helpful", cache=True), SystemPart(text="volatile")],
            tools=[_tool("a"), _tool("z")],
            messages=[user("first"), user("last message")],
            cache_after=[1],
        )
    )
    assert body["system"] == [
        {"type": "text", "text": "you are helpful", "cache_control": EPHEMERAL},
        {"type": "text", "text": "volatile"},
    ]
    assert "cache_control" not in body["tools"][0]
    assert body["tools"][-1]["cache_control"] == EPHEMERAL
    assert _stamped(body) == [(1, 0)]
    assert json.dumps(body).count('"cache_control"') == 3


@respx.mock
async def test_cache_after_stamps_the_last_block_of_that_message() -> None:
    """The composer anchors on the last recorded message; a trailing
    host-injected reminder merged into the same wire turn stays outside."""
    wire = await _wire_messages(
        [user("q"), _call(k="x"), _result(), _injected("todo: keep going", "system"),
         _injected("recalled pointer", "memory")],
        cache_after=[0, 2],
    )
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    blocks = wire[2]["content"]
    assert [b["type"] for b in blocks] == ["tool_result", "text", "text"]
    assert blocks[0]["cache_control"] == EPHEMERAL
    assert all("cache_control" not in b for b in blocks[1:])
    assert wire[0]["content"][-1]["cache_control"] == EPHEMERAL
    assert json.dumps(wire).count('"cache_control"') == 2


@respx.mock
async def test_cache_prefix_is_stable_when_only_the_reminder_tail_changes() -> None:
    """Two steps resending the same history under a different reminder tail
    produce byte-identical wire bytes through the breakpoint."""
    history = [user("q"), _call(k="x"), _result()]
    one = await _wire_messages([*history, _injected("todo: 2 left", "system")], cache_after=[2])
    two = await _wire_messages([*history, _injected("todo: 1 left", "system")], cache_after=[2])

    def through_breakpoint(wire: list[dict[str, Any]]) -> str:
        out: list[Any] = []
        for message in wire:
            blocks = []
            for block in message["content"]:
                blocks.append(block)
                if "cache_control" in block:
                    return json.dumps([*out, {**message, "content": blocks}])
            out.append(message)
        raise AssertionError("no breakpoint in messages")

    assert through_breakpoint(one) == through_breakpoint(two)
    assert one[-1]["content"][-1] != two[-1]["content"][-1]


@respx.mock
async def test_cache_after_on_a_message_with_no_wire_content_walks_back() -> None:
    """An assistant turn whose only block is foreign thinking renders empty;
    its breakpoint lands on the previous message's last block."""
    assistant = Message(role="assistant", content=[Thinking(text="x", family="openai_compat")])
    body, _ = await _send(_req(messages=[user("q"), assistant], cache_after=[1]))
    assert _stamped(body) == [(0, 0)]


@respx.mock
async def test_out_of_range_cache_after_is_ignored() -> None:
    body, _ = await _send(_req(cache_after=[5, -1]))
    assert json.dumps(body).count('"cache_control"') == 0


@respx.mock
async def test_breakpoints_capped_at_four_keeping_messages_then_system_then_tools() -> None:
    """Anthropic allows four breakpoints. Message breakpoints (newest first)
    win, then cached system parts (last first), then the last tool."""
    body, _ = await _send(
        _req(
            system=[SystemPart(text="a", cache=True), SystemPart(text="b", cache=True)],
            tools=[_tool()],
            messages=[user("1"), user("2"), user("3")],
            cache_after=[0, 2],
        )
    )
    assert _stamped(body) == [(0, 0), (2, 0)]
    assert [("cache_control" in s) for s in body["system"]] == [True, True]
    assert "cache_control" not in body["tools"][0]
    assert json.dumps(body).count('"cache_control"') == 4


@respx.mock
async def test_many_message_breakpoints_keep_the_newest_four() -> None:
    body, _ = await _send(
        _req(
            system=[SystemPart(text="s", cache=True)],
            messages=[user(str(i)) for i in range(6)],
            cache_after=[0, 1, 2, 3, 4, 5],
        )
    )
    assert _stamped(body) == [(2, 0), (3, 0), (4, 0), (5, 0)]
    assert "cache_control" not in body["system"][0]


@respx.mock
async def test_parallel_step_previous_breakpoint_reads_back_the_prior_entry() -> None:
    """A step of twelve parallel calls appends ~25 blocks — past Anthropic's
    lookback. With the composer's two message breakpoints (the last recorded
    block and the previous step's) the previous request's prefix is re-read."""
    first = _call("r0", "read", p="a")
    first_result = _result("r0", "body")
    fan = Message(
        role="assistant",
        content=[Text(text="Reading in parallel.")]
        + [ToolUse(id=f"p{k}", name="grep", input={"k": k}) for k in range(12)],
    )
    fan_results = Message(
        role="user",
        content=[ToolResult(call_id=f"p{k}", content=[Text(text=f"hit {k}")]) for k in range(12)],
    )
    history = [user("q"), first, first_result, fan, fan_results]
    request = _req(
        system=[SystemPart(text="sys", cache=True)],
        tools=[_tool()],
        messages=[*history, _injected("todo: 1 left", "system")],
        cache_after=[2, 4],
    )
    body, _ = await _send(request)
    assert _stamped(body) == [(2, 0), (4, 11)]
    assert "cache_control" not in body["messages"][4]["content"][-1]
    assert json.dumps(body).count('"cache_control"') == 4

    prev, _ = await _send(
        _req(
            system=[SystemPart(text="sys", cache=True)],
            tools=[_tool()],
            messages=[*history[:3], _injected("todo: 2 left", "system")],
            cache_after=[2],
        )
    )
    assert _stamped(prev)[-1] == (2, 0)
    msgs, prev_msgs = body["messages"], prev["messages"]
    assert json.dumps(prev_msgs[:2] + [{**prev_msgs[2], "content": prev_msgs[2]["content"][:1]}]) == (
        json.dumps(msgs[:2] + [{**msgs[2], "content": msgs[2]["content"][:1]}])
    )


@respx.mock
async def test_cache_control_never_mutates_the_request() -> None:
    request = _req(system=[SystemPart(text="sys", cache=True)], tools=[_tool()], cache_after=[0])
    before = request
    snapshot = json.dumps(
        [m.text for m in request.messages] + [p.text for p in request.system]
    )
    body, _ = await _send(request)
    assert json.dumps(body).count('"cache_control"') == 3
    assert request == before
    assert snapshot == json.dumps(
        [m.text for m in request.messages] + [p.text for p in request.system]
    )
    # A second build from the same request is identical (no stamps leak back).
    provider = _provider()
    assert provider.build_body(request) == provider.build_body(request)

