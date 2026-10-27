"""The pure parts of compaction: window math, the estimate, the valves, the
note gate, the verbatim constraints, the overflow retry's round dropping,
RecallHistory's rendering, and the 0.6 texts kept byte for byte."""

from __future__ import annotations

import pytest

from noeta.core.errors import ContextOverflow, Fatal
from noeta.core.types import Image, Message, ModelResponse, Text, ToolResult, ToolUse, Usage, user
from noeta.plugins.compaction import clearing, summary
from noeta.plugins.compaction.estimate import IMAGE_TOKENS, messages_tokens, text_tokens
from noeta.plugins.compaction.recall import (
    READ_SUGGESTION,
    RECALL_HISTORY_DESCRIPTION,
    collapsed_context,
    render_slice,
)
from noeta.plugins.compaction.window import derive_window

# ------------------------------------------------------------------ window


def test_usable_window_is_claude_codes_margin() -> None:
    w = derive_window("claude-sonnet-4-6")
    assert w.usable == 1_000_000 - 20_000 - 13_000
    assert w.tail_budget == w.usable // 3
    assert w.micro_fraction == 0.5
    haiku = derive_window("claude-haiku-4-5", auto_compact_window=100_000)
    assert haiku.context_window == 100_000 and haiku.usable == 100_000 - 33_000


def test_unknown_model_gets_the_conservative_window() -> None:
    w = derive_window("no-such-model")
    assert (w.context_window, w.max_output_tokens) == (128_000, 16_384)
    assert w.usable == 128_000 - 16_384 - 13_000


def test_cap_and_clear_point() -> None:
    w = derive_window("claude-opus-5", auto_compact_window=200_000,
                      clear_tool_outputs_at=50_000)
    assert w.usable == 167_000
    assert w.micro_fraction == 50_000 / 167_000
    assert derive_window("m", clear_tool_outputs_at=10**9).micro_fraction == 1.0
    with pytest.raises(ValueError, match="leaves no usable window"):
        derive_window("claude-opus-5", auto_compact_window=33_000)
    with pytest.raises(ValueError, match="positive int"):
        derive_window("m", auto_compact_window=0)


# ------------------------------------------------------------------ estimate


def test_estimate() -> None:
    assert text_tokens("") == 0 and text_tokens("abcde") == 2
    m = Message(role="user", content=[ToolResult(call_id="a", content=[
        Text(text="x" * 8), Image(media_type="image/png", data=b"\0" * 10_000)])])
    assert messages_tokens([m]) == 1 + 2 + IMAGE_TOKENS


# ------------------------------------------------------------------ valves


def _history(n: int, size: int = 2_000, tool: str = "Read") -> list[Message]:
    msgs = [user("go")]
    for i in range(n):
        msgs.append(Message(role="assistant", content=[ToolUse(id=f"c{i}", name=tool, input={})]))
        msgs.append(Message(role="user", content=[
            ToolResult(call_id=f"c{i}", content=[Text(text="y" * size)])]))
    return msgs


VALVES = clearing.Valves(usable=100_000, tail_budget=33_000, micro_fraction=0.5,
                         keep_recent=5, batch_tokens=1_000)


def _select(msgs: list[Message], size: int, valves: clearing.Valves = VALVES,
            tools: frozenset[str] = clearing.CLAUDE_CODE_CLEARABLE) -> list[str]:
    return clearing.select(msgs, is_clearable=tools.__contains__, size=size, valves=valves,
                           real_baseline=0, estimated=size)


def test_valves_shut_below_the_clear_point() -> None:
    assert _select(_history(10), 49_999) == []


def test_micro_valve_clears_complete_batches_older_than_the_newest_five() -> None:
    # 9 results of 500 tokens: 4 older than the newest five -> two batches.
    assert _select(_history(9), 50_000) == ["c0", "c1", "c2", "c3"]
    # 8 results: 3 older -> one complete batch, the third waits.
    assert _select(_history(8), 50_000) == ["c0", "c1"]


def test_only_declared_tools_and_bulky_outputs_are_cleared() -> None:
    assert _select(_history(9, tool="Task"), 50_000) == []
    assert _select(_history(9, size=399), 99_000, clearing.Valves(
        usable=100_000, tail_budget=0, micro_fraction=0.5, keep_recent=0,
        batch_tokens=1)) == []


def test_relief_valve_keeps_the_tail_and_the_newest_message() -> None:
    valves = clearing.Valves(usable=1_000, tail_budget=1_100, micro_fraction=1.0,
                             keep_recent=None)
    # Tail of 1,100 tokens covers the two newest results (~502 + ~2 each).
    assert _select(_history(5), 1_000, valves) == ["c0", "c1", "c2"]
    tiny = clearing.Valves(usable=1_000, tail_budget=1, micro_fraction=1.0, keep_recent=None)
    assert _select(_history(3), 1_000, tiny) == ["c0", "c1"]  # newest message stays


def test_cleared_result_keeps_its_call_and_error_flag() -> None:
    msgs = _history(1)
    msgs[2] = Message(role="user", content=[
        ToolResult(call_id="c0", content=[Text(text="z" * 500)], is_error=True)])
    out = clearing.apply_cleared(msgs, {"c0"})
    (block,) = out[2].content
    assert isinstance(block, ToolResult) and block.is_error
    assert block.text == clearing.CLEARED_MARKER == "[Old tool result content cleared]"
    assert out[1] is msgs[1]  # arguments as sent
    assert clearing.raw_boundary(msgs, {"c0"}) == 3


def test_density_is_clamped() -> None:
    assert clearing.density(10, 2_600) == clearing.DENSITY_MIN
    assert clearing.density(0, 100) == 1.0
    assert clearing.density(100_000, 10) == clearing.DENSITY_MAX


# ------------------------------------------------------------------ summary


def test_every_section_title_is_in_the_prompt() -> None:
    for title in summary.SUMMARY_SECTION_TITLES:
        assert title in summary.SUMMARIZE_PROMPT


def test_note_gate() -> None:
    assert summary.looks_like_summary_note("Primary Request & Intent: x\nCurrent Work: y")
    assert summary.looks_like_summary_note("## 1. 主要请求\n...\n## 2. 概念")
    assert not summary.looks_like_summary_note("1. 主要请求\n2. 概念")
    assert not summary.looks_like_summary_note("The file is long. Fetching the rest.")


def test_failure_reasons() -> None:
    empty = ModelResponse(message=Message(role="assistant", content=[]), stop_reason="end_turn",
                          usage=Usage(output=0))
    assert summary.summary_failure(empty, "") == (
        "empty summarize response (stop_reason=end_turn, output_tokens=0, blocks=none)")
    assert summary.summary_failure(Fatal("bad"), "") == (
        "errored summarize round-trip (category=fatal, error='bad')")
    assert summary.summary_failure(ContextOverflow("long"), "").startswith(
        "errored summarize round-trip (category=overflow")


def test_constraints_are_read_only_from_the_users_own_turns() -> None:
    msgs = [
        user("Please work.\n- Do not touch secrets.txt"),
        user("never edit a.py", origin="system"),
        Message(role="assistant", content=[Text(text="must not delete b.py")]),
        Message(role="user", content=[
            ToolResult(call_id="x", content=[Text(text="forbidden: c.py")])]),
        user("严禁修改 d.py", origin="human"),
    ]
    assert summary.extract_safety_constraints(msgs) == ["Do not touch secrets.txt", "严禁修改 d.py"]


def test_enforcing_constraints_is_idempotent() -> None:
    once = summary.enforce_verbatim_constraints("note", ["Do not touch x"])
    assert once == f"note\n\n{summary.VERBATIM_HEADER}\n- Do not touch x"
    again = summary.extract_safety_constraints([user(once)])
    assert summary.enforce_verbatim_constraints(once, again) == once


def test_drop_oldest_rounds() -> None:
    msgs = _history(9)  # 10 rounds: the goal, then nine call+result pairs
    shorter = summary.drop_oldest_rounds(msgs, keep_first=False)
    assert shorter is not None
    assert shorter[0].text == summary.SUMMARY_TRUNCATION_MARKER
    assert len(shorter) == 1 + len(msgs) - 3  # two rounds: the goal and one pair
    again = summary.drop_oldest_rounds(shorter, keep_first=False)
    assert again is not None and sum(
        m.text == summary.SUMMARY_TRUNCATION_MARKER for m in again) == 1
    kept = summary.drop_oldest_rounds([user("note"), *msgs], keep_first=True)
    assert kept is not None and kept[0].text == "note"
    assert summary.drop_oldest_rounds([user("only")], keep_first=False) is None


def test_framing_is_applied_once() -> None:
    assert summary.framed("n") == f"{summary.SUMMARY_FRAME}\n\nn"
    assert summary.framed(summary.framed("n")) == summary.framed("n")


# ------------------------------------------------------------------ recall


def test_recall_rendering() -> None:
    history = _history(3)
    text = render_slice(history, 4, 0, 0, 2)
    assert text.startswith("Collapsed range: 4 messages (indices 0..3). Showing 0..1.")
    assert "[0] user:\n  text: go" in text
    assert "[1] assistant:\n  tool_use Read: {}" in text
    assert text.endswith("(more collapsed messages follow — continue with offset=2.)")
    assert render_slice(history, 0, 0, 0, 5).startswith("No messages have been collapsed yet")
    assert render_slice(history, 2, 0, 5, 1).startswith("offset 5 is past the collapsed range")
    wide = render_slice(history, 2, 5, 4, 1)
    assert wide.startswith("Recallable range: 5 messages (indices 0..4; the first 2 are")
    assert "y" * 2_000 in wide  # limit 1 shows the block whole
    assert "…[truncated]" in render_slice(_history(1, size=3_000), 3, 0, 0, 3)


def test_collapsed_context_texts() -> None:
    assert collapsed_context(0, 0) is None
    both = collapsed_context(4, 9)
    assert both is not None and both.startswith(
        "The note at the head of this conversation stands in for the first 4 original")
    assert both.endswith("limit 1 shows a single message in full.")
    assert "in messages 0..8" in both
    for text in (both, READ_SUGGESTION, RECALL_HISTORY_DESCRIPTION):
        assert summary.extract_safety_constraints([user(text)]) == []
