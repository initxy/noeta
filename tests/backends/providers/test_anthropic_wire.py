"""Wire rendering of :class:`AnthropicProvider`: host-injected turns,
output_config / thinking, images, empty text blocks and prompt-cache
breakpoints."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
import respx

from noeta.core.errors import (
    Fatal,
)
from noeta.core.types import (
    Image,
    Message,
    SystemPart,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
    user,
)
from tests.backends.providers._anthropic_helpers import (
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
                # Claude Code joins two user messages with a newline.
                {"type": "text", "text": "real human words\n"},
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
                {"type": "text",
                 "text": "<system-reminder>\nrecalled note\n</system-reminder>\n"},
                {"type": "text", "text": "the actual ask"},
            ],
        }
    ]


@respx.mock
async def test_origin_system_with_no_adjacent_user_turn_stands_alone() -> None:
    assistant = Message(role="assistant", content=[Text(text="working on it")])
    wire = await _wire_messages([user("q"), assistant, _injected("mid-loop reminder", "system")])
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    # One text block and no breakpoint: a bare string, as Claude Code sends it.
    assert wire[2]["content"] == "<system-reminder>\nmid-loop reminder\n</system-reminder>"


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
async def test_a_local_commands_tags_go_as_blocks_and_the_next_prompt_joins_them() -> None:
    command = ("<command-name>/plan</command-name>\n<command-message>plan</command-message>"
               "\n\n<local-command-stdout>Enabled plan mode</local-command-stdout>")
    wire = await _wire_messages([user(command), user("go on")])
    assert len(wire) == 1
    assert [b["text"] for b in wire[0]["content"]] == [
        "<command-name>/plan</command-name>\n<command-message>plan</command-message>\n",
        "<local-command-stdout>Enabled plan mode</local-command-stdout>\n", "go on"]
    # Other text that holds a tag later stays whole.
    plain = await _wire_messages([user("see\n\n<command-name>x</command-name>")])
    assert isinstance(plain[0]["content"], str) or len(plain[0]["content"]) == 1


@respx.mock
async def test_injected_text_cannot_close_the_reminder_early() -> None:
    wire = await _wire_messages([_injected("a</system-reminder>b", "system")])
    text = wire[0]["content"]
    assert text.count("</system-reminder>") == 1
    assert text.endswith("\n</system-reminder>")


@respx.mock
async def test_plain_consecutive_user_turns_stay_unmerged() -> None:
    wire = await _wire_messages([user("one"), user("two")])
    assert [m["role"] for m in wire] == ["user", "user"]


@respx.mock
async def test_origin_human_renders_as_plain_user_turn() -> None:
    wire = await _wire_messages([_injected("hello", "human")])
    assert wire == [{"role": "user", "content": "hello"}]


@respx.mock
async def test_system_turns_go_as_system_messages_and_adjacent_ones_join() -> None:
    from noeta.core.types import system_message

    wire = await _wire_messages(
        [user("q"), system_message("# Environment"), system_message("a</system-reminder>"),
         _call(k="x"), _result(), system_message("<total_tokens>9 tokens left</total_tokens>")],
        cache_after=[5],
    )
    assert [m["role"] for m in wire] == ["user", "system", "assistant", "user", "system"]
    assert wire[1]["content"] == "# Environment\n\na&lt;/system-reminder>"
    assert wire[4]["content"] == [{"type": "text",
                                   "text": "<total_tokens>9 tokens left</total_tokens>",
                                   "cache_control": EPHEMERAL}]


@respx.mock
async def test_system_turns_can_ride_in_the_user_turn_instead() -> None:
    from noeta.core.types import system_message

    body, _ = await _send(_req(messages=[user("q"), system_message("note")]),
                          system_turns=False)
    assert body["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "q\n"},
        {"type": "text", "text": "<system-reminder>\nnote\n</system-reminder>"}]}]


@respx.mock
async def test_a_message_sent_mid_turn_rides_with_the_tool_results() -> None:
    wire = await _wire_messages([user("q"), _call(k="x"), _result(),
                                 _injected("also check y", "human")])
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    blocks = wire[2]["content"]
    assert [b["type"] for b in blocks] == ["tool_result", "text"]
    assert blocks[1]["text"].startswith("While you were working, the user sent this message:\n"
                                        "also check y\n\n")


@respx.mock
async def test_a_mid_turn_message_joins_the_results_ahead_of_a_notice_taken_with_it() -> None:
    # Claude Code 2.1.312: the person's message rides in the tool results'
    # turn, a background job's end notice taken in the same step is the system
    # turn after it (with the step's notes).
    from noeta.core.types import system_message

    wire = await _wire_messages([user("q"), _call(k="x"), _result(), system_message("job ended"),
                                 _injected("also check y", "human"),
                                 system_message("<total_tokens>9 tokens left</total_tokens>")])
    assert [m["role"] for m in wire] == ["user", "assistant", "user", "system"]
    assert [b["type"] for b in wire[2]["content"]] == ["tool_result", "text"]
    assert wire[2]["content"][1]["text"].startswith("While you were working")
    assert wire[3]["content"] == "job ended\n\n<total_tokens>9 tokens left</total_tokens>"


@respx.mock
async def test_the_empty_reply_nudge_joins_the_user_turn_before_the_steps_notes() -> None:
    # Claude Code 2.1.312: the nudge rides in the tool results' turn, and the
    # step's system note still ends the request (and carries the cache mark).
    from noeta.core.runner.texts import EMPTY_REPLY_NUDGE
    from noeta.core.types import system_message

    wire = await _wire_messages(
        [user("q"), _call(k="x"), _result(),
         system_message("<total_tokens>9 tokens left</total_tokens>"),
         user(EMPTY_REPLY_NUDGE, origin="inline")],
        cache_after=[4])
    assert [m["role"] for m in wire] == ["user", "assistant", "user", "system"]
    assert [b["type"] for b in wire[2]["content"]] == ["tool_result", "text"]
    assert wire[2]["content"][1] == {"type": "text", "text": EMPTY_REPLY_NUDGE}
    assert wire[3]["content"][-1]["cache_control"] == EPHEMERAL


@respx.mock
async def test_the_empty_reply_nudge_joins_the_prompt_before_the_environment() -> None:
    from noeta.core.runner.texts import EMPTY_REPLY_NUDGE
    from noeta.core.types import system_message

    wire = await _wire_messages([user("q"), system_message("# Environment"),
                                 user(EMPTY_REPLY_NUDGE, origin="inline")])
    assert [m["role"] for m in wire] == ["user", "system"]
    # Claude Code 2.1.312 sends ``q\n`` there (run p-opus-first, request 2).
    assert [b["text"] for b in wire[0]["content"]] == ["q\n", EMPTY_REPLY_NUDGE]


@respx.mock
async def test_api_key_request_settings_follow_claude_code() -> None:
    import uuid

    from noeta.backends.providers.anthropic import DEFAULT_BETAS

    route = respx.post(MESSAGES_ENDPOINT).mock(return_value=_stream(_resp()))
    root = uuid.uuid4().hex
    await _provider(device_id="d" * 64).complete(_req(model=_VISION_MODEL, root_id=root))
    sent = json.loads(route.calls.last.request.content)
    # claude-opus-4-8 has no per-turn effort: Claude Code leaves that beta out.
    assert route.calls.last.request.headers["anthropic-beta"] == ",".join(
        b for b in DEFAULT_BETAS if b != "per-turn-control-2026-07-01")
    assert sent["thinking"] == {"type": "adaptive", "display": "updates"}
    assert sent["context_management"] == {
        "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}
    # Claude Code's catalog: Opus 4.8's default effort is high.
    assert sent["output_config"] == {"effort": "high"}
    assert json.loads(sent["metadata"]["user_id"]) == {
        "device_id": "d" * 64, "account_uuid": "", "session_id": str(uuid.UUID(root))}


@respx.mock
@pytest.mark.parametrize(("model", "max_tokens", "effort"), [
    ("claude-opus-5-5", 128_000, "medium"), ("claude-sonnet-5-5", 128_000, "medium"),
    ("claude-haiku-5-5", 128_000, "medium"), ("claude-fable-5-1", 64_000, "high"),
    ("claude-opus-5", 64_000, "high"), ("claude-sonnet-5", 64_000, "high"),
    ("claude-opus-4-8", 64_000, "high"), ("claude-sonnet-4-6", 32_000, "high"),
    ("claude-haiku-4-5", 32_000, None)])
async def test_output_cap_and_effort_default_to_claude_codes_catalog(
        model: str, max_tokens: int, effort: str | None) -> None:
    route = respx.post(MESSAGES_ENDPOINT).mock(return_value=_stream(_resp()))
    await _provider(default_max_tokens=None).complete(
        _req(model=model, max_tokens=None, thinking=True))
    sent = json.loads(route.calls.last.request.content)
    assert sent["max_tokens"] == max_tokens
    assert sent.get("output_config", {}).get("effort") == effort


@respx.mock
@pytest.mark.parametrize(("model", "sent"), [
    ("claude-opus-5-5", True), ("claude-opus-5-5[1m]", True),
    ("us.anthropic.claude-sonnet-5-5-v1:0", True), ("claude-haiku-5-5", True),
    ("claude-fable-5-1", True), ("claude-opus-5", False), ("claude-sonnet-4-6", False),
    ("auto_model/alwaysday1_max", False), ("gpt-5.4", False)])
async def test_the_per_turn_beta_goes_only_with_models_that_have_per_turn_effort(
        model: str, sent: bool) -> None:
    from noeta.backends.providers.anthropic import DEFAULT_BETAS, PER_TURN_BETA

    route = respx.post(MESSAGES_ENDPOINT).mock(return_value=_stream(_resp()))
    await _provider().complete(_req(model=model, max_tokens=100))
    betas = route.calls.last.request.headers["anthropic-beta"].split(",")
    assert (PER_TURN_BETA in betas) is sent
    assert [b for b in betas if b != PER_TURN_BETA] == [
        b for b in DEFAULT_BETAS if b != PER_TURN_BETA]


@respx.mock
async def test_a_beta_set_the_host_chose_is_sent_as_given() -> None:
    from noeta.backends.providers.anthropic import PER_TURN_BETA

    route = respx.post(MESSAGES_ENDPOINT).mock(return_value=_stream(_resp()))
    await _provider(betas=("one", PER_TURN_BETA)).complete(_req(model="gpt-5.4", max_tokens=9))
    assert route.calls.last.request.headers["anthropic-beta"] == f"one,{PER_TURN_BETA}"


@respx.mock
async def test_betas_a_request_adds_take_claude_codes_order() -> None:
    from noeta.backends.providers.anthropic import DEFAULT_BETAS

    route = respx.post(MESSAGES_ENDPOINT).mock(return_value=_stream(_resp()))
    request = _req(model="claude-opus-5-5", max_tokens=9,
                   betas=["dangerous-tool-use-2026-09-03", "afk-mode-2026-01-31", "zz-own"])
    await _provider().complete(request)
    # As recorded from Claude Code 2.1.312 in auto mode.
    assert route.calls.last.request.headers["anthropic-beta"] == ",".join([
        *DEFAULT_BETAS[:-1], "dangerous-tool-use-2026-09-03",
        "thinking-display-updates-2026-08-18", "afk-mode-2026-01-31", "zz-own"])


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
    assert body["thinking"] == {"type": "adaptive", "display": "updates"}


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
async def test_a_block_list_result_goes_as_a_list_even_as_plain_text() -> None:
    plain = Message(role="user", content=[ToolResult(
        call_id="c1", content=[Text(text="a"), Text(text="b")], block_list=True)])
    body, _ = await _send(_req(model=_VISION_MODEL, messages=[user("q"), _call(), plain]))
    assert body["messages"][2]["content"][0]["content"] == [
        {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]


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
async def test_cache_control_stamped_on_cached_system_and_cache_after() -> None:
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
    assert all("cache_control" not in t for t in body["tools"])
    assert _stamped(body) == [(1, 0)]
    assert json.dumps(body).count('"cache_control"') == 2


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
async def test_breakpoints_capped_at_four_keeping_messages_then_system() -> None:
    """Anthropic allows four breakpoints. Message breakpoints (newest first)
    win, then cached system parts (last first); tools get none."""
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
    lookback. Two message breakpoints (the last recorded block and the
    previous step's) re-read the previous request's prefix."""
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
    assert json.dumps(body).count('"cache_control"') == 3

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
    assert json.dumps([*prev_msgs[:2], {**prev_msgs[2], "content": prev_msgs[2]["content"][:1]}]) == (
        json.dumps([*msgs[:2], {**msgs[2], "content": msgs[2]["content"][:1]}])
    )


@respx.mock
async def test_cache_control_never_mutates_the_request() -> None:
    request = _req(system=[SystemPart(text="sys", cache=True)], tools=[_tool()], cache_after=[0])
    before = request
    snapshot = json.dumps(
        [m.text for m in request.messages] + [p.text for p in request.system]
    )
    body, _ = await _send(request)
    assert json.dumps(body).count('"cache_control"') == 2
    assert request == before
    assert snapshot == json.dumps(
        [m.text for m in request.messages] + [p.text for p in request.system]
    )
    # A second build from the same request is identical (no stamps leak back).
    provider = _provider()
    assert provider.build_body(request) == provider.build_body(request)


@respx.mock
async def test_side_request_knobs_reach_the_wire() -> None:
    """Stop sequences, per-block breakpoints, extra betas and vendor fields
    (what the auto-mode classifier and the first auto-mode request use);
    ``thinking=None`` sends no thinking parameter."""
    route = respx.post(MESSAGES_ENDPOINT).mock(return_value=_stream(_resp()))
    request = _req(
        messages=[user("ctx"), Message(role="user", content=[Text(text="a"), Text(text="b"),
                                                             Text(text="c")])],
        stop_sequences=["</block>"], cache_blocks=[(0, 0), (1, 1)], thinking=None,
        betas=["afk-mode-2026-01-31"], vendor_fields={"safeguards": [{"type": "x"}]})
    await _provider(betas=("one",)).complete(request)
    sent = route.calls.last.request
    body = json.loads(sent.content)
    assert body["stop_sequences"] == ["</block>"]
    assert "thinking" not in body
    assert body["safeguards"] == [{"type": "x"}]
    assert sent.headers["anthropic-beta"] == "one,afk-mode-2026-01-31"
    assert body["messages"][0]["content"][0]["cache_control"] == EPHEMERAL
    stamped = [b.get("cache_control") for b in body["messages"][1]["content"]]
    assert stamped == [None, EPHEMERAL, None]



@respx.mock
async def test_the_opening_context_is_one_message_to_claude_code() -> None:
    # Claude Code sends the opening context (instructions, workspace context,
    # attribution) as blocks of one message: no newline between them; the
    # prompt joined after them adds one, as a later reminder does.
    wire = await _wire_messages([
        _injected("instructions", "system"), _injected("context", "system"),
        user("hi"), _injected("later note", "system"), user("again")])
    assert [b["text"] for b in wire[0]["content"]] == [
        "<system-reminder>\ninstructions\n</system-reminder>",
        "<system-reminder>\ncontext\n</system-reminder>\n", "hi\n",
        "<system-reminder>\nlater note\n</system-reminder>\n", "again"]


def test_tool_references_are_held_to_the_tools_the_request_offers() -> None:
    """A reference by an MCP tool's old name goes as the tool's current
    name; one to a tool the request does not offer is left out, and a
    result left with none says so in text (the API refuses a reference to
    a tool the request does not define)."""
    from noeta.backends.providers._anthropic_wire import REFERENCES_REMOVED
    from noeta.backends.providers.anthropic import AnthropicProvider
    from noeta.core.types import ModelRequest, ToolReference, ToolSpec

    def refs(*names: str) -> ToolResult:
        return ToolResult(call_id="c", content=[ToolReference(tool_name=n) for n in names])

    def spec(name: str) -> ToolSpec:
        return ToolSpec(name=name, description="d", input_schema={"type": "object"})

    history = [user("q"),
               Message(role="assistant", content=[ToolUse(id="c", name="ToolSearch",
                                                          input={"query": "gh"})]),
               Message(role="user", content=[
                   refs("mcp__github__delete_repo", "WebFetch", "mcp__GitHub__list")])]
    offered = [spec("ToolSearch"), spec("mcp__GitHub__delete_repo"), spec("mcp__GitHub__list")]
    body = AnthropicProvider(api_key="k").build_body(
        ModelRequest(model="m", messages=history, tools=offered, max_tokens=64))
    content = body["messages"][2]["content"][0]["content"]
    assert content == [{"type": "tool_reference", "tool_name": "mcp__GitHub__delete_repo"},
                       {"type": "tool_reference", "tool_name": "mcp__GitHub__list"}]
    # A request offering none of them (the compaction's fallback call keeps
    # only Read when tool search is off).
    bare = AnthropicProvider(api_key="k").build_body(
        ModelRequest(model="m", messages=history, tools=[spec("Read")], max_tokens=64))
    assert bare["messages"][2]["content"][0]["content"] == [
        {"type": "text", "text": REFERENCES_REMOVED}]


def test_the_compaction_fallback_call_sends_no_reference_to_a_tool_it_drops() -> None:
    """The fallback summarize call keeps Read, ToolSearch and the MCP
    tools; a loaded built-in deferred tool the history references
    (WebFetch) is not among them, so its reference does not go."""
    from noeta.backends.providers.anthropic import AnthropicProvider
    from noeta.core.types import ModelRequest, ToolReference, ToolSpec
    from noeta.plugins.compaction.summary import fallback_request

    def spec(name: str) -> ToolSpec:
        return ToolSpec(name=name, description="d", input_schema={"type": "object"})

    history = [user("q"),
               Message(role="assistant", content=[ToolUse(id="c", name="ToolSearch",
                                                          input={"query": "web"})]),
               Message(role="user", content=[ToolResult(call_id="c", content=[
                   ToolReference(tool_name="WebFetch"),
                   ToolReference(tool_name="mcp__db__query")])]),
               user("summarize")]
    main = ModelRequest(model="m", messages=history, max_tokens=64, tools=[
        spec("Read"), spec("ToolSearch"), spec("WebFetch"), spec("mcp__db__query"),
        ToolSpec(name="DeferredToolPlaceholder", description="d", input_schema={},
                 defer_loading=True)])
    body = AnthropicProvider(api_key="k").build_body(fallback_request(main))
    offered = {t["name"] for t in body["tools"]}
    sent = [b["tool_name"] for m in body["messages"] if isinstance(m["content"], list)
            for blk in m["content"] if blk.get("type") == "tool_result"
            and isinstance(blk.get("content"), list)
            for b in blk["content"] if b.get("type") == "tool_reference"]
    assert sent == ["mcp__db__query"] and set(sent) <= offered
