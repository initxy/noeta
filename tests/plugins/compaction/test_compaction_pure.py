"""The pure parts of compaction: the threshold, the estimate, the summarize
request, the summary message, and the overflow retry's round dropping."""

from __future__ import annotations

import pytest

from noeta.core.errors import ContextOverflow, Fatal
from noeta.core.types import (
    Image,
    Message,
    ModelRequest,
    ModelResponse,
    Text,
    ToolResult,
    ToolUse,
    Usage,
    user,
)
from noeta.plugins.compaction import summary
from noeta.plugins.compaction.estimate import IMAGE_TOKENS, messages_tokens, text_tokens
from noeta.plugins.compaction.extension import Compaction, command_text
from noeta.plugins.compaction.window import PERCENT_ENV, WINDOW_ENV, derive_window

# ------------------------------------------------------------------ window


def test_threshold_is_claude_codes() -> None:
    # Opus 5.5: 1M window, 128K output -> 20K held back, 13K under that.
    w = derive_window("claude-opus-5-5")
    assert w.effective == 1_000_000 - 20_000
    assert w.threshold == 1_000_000 - 20_000 - 13_000


def test_window_cap_and_percent_override() -> None:
    # Claude Code with CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000 and
    # CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=30: min(30 % of 80K, 80K - 13K).
    w = derive_window("claude-opus-5-5", auto_compact_window=100_000, auto_compact_percent=30)
    assert (w.context_window, w.effective, w.threshold) == (100_000, 80_000, 24_000)
    # A percentage above the default threshold does not raise it.
    assert derive_window("claude-opus-5-5", auto_compact_window=100_000,
                         auto_compact_percent=95).threshold == 67_000


def test_unknown_model_gets_the_conservative_window() -> None:
    w = derive_window("no-such-model")
    assert (w.context_window, w.max_output_tokens) == (128_000, 16_384)
    assert w.threshold == 128_000 - 16_384 - 13_000


def test_bad_settings() -> None:
    with pytest.raises(ValueError, match="leaves no window"):
        derive_window("claude-opus-5-5", auto_compact_window=20_000)
    with pytest.raises(ValueError, match="positive int"):
        derive_window("m", auto_compact_window=0)
    with pytest.raises(ValueError, match="auto_compact_percent"):
        derive_window("m", auto_compact_percent=0)


def test_the_environment_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WINDOW_ENV, "100000")
    monkeypatch.setenv(PERCENT_ENV, "30")
    ext = Compaction()
    assert (ext.auto_compact_window, ext.auto_compact_percent) == (100_000, 30.0)
    monkeypatch.setenv(PERCENT_ENV, "nope")
    assert Compaction(auto_compact_window=50_000).auto_compact_percent is None


# ------------------------------------------------------------------ estimate


def test_estimate() -> None:
    assert text_tokens("") == 0 and text_tokens("abcde") == 2
    m = Message(role="user", content=[ToolResult(call_id="a", content=[
        Text(text="x" * 8), Image(media_type="image/png", data=b"\0" * 10_000)])])
    assert messages_tokens([m]) == 1 + 2 + IMAGE_TOKENS


# ------------------------------------------------------------------ the request


def _main() -> ModelRequest:
    return ModelRequest(model="m", messages=[user("x")], max_tokens=128_000, effort="medium",
                        output_schema={"type": "object"}, cache_after=[0])


def test_instruction_goes_into_the_last_user_message() -> None:
    use = ToolUse(id="c1", name="Bash", input={})
    history = [user("go"), Message(role="assistant", content=[use]),
               Message(role="user", content=[ToolResult(call_id="c1", content=[Text(text="ok")])]),
               user("a reminder", origin="system")]
    req = summary.summary_request(_main(), history, "SUMMARIZE")
    # Appended after the results, before the trailing reminder; the cache
    # mark sits on the reply before it.
    assert req.messages[2].content[-1] == Text(text="SUMMARIZE")
    assert isinstance(req.messages[2].content[0], ToolResult)
    assert req.messages[3].text == "a reminder"
    assert req.cache_after == [1]
    # The main request's settings, without a tool_choice or output schema.
    assert (req.max_tokens, req.effort, req.tool_choice, req.output_schema) == \
        (128_000, "medium", None, None)


def test_a_trailing_system_turn_stays_after_the_instruction() -> None:
    from noeta.core.types import system_message

    history = [user("read it", origin="human"), system_message("<total_tokens>9</total_tokens>")]
    req = summary.summary_request(_main(), history, "SUMMARIZE")
    # Claude Code (m3/0009): the prompt and the instruction share the user
    # message; the token-count system turn follows unchanged.
    assert [b.text for b in req.messages[0].content] == [  # type: ignore[union-attr]
        "read it", "SUMMARIZE"]
    assert req.messages[0].origin == "human" and req.messages[1].role == "system"


def test_after_a_reply_the_instruction_is_a_new_message() -> None:
    history = [user("go"), Message(role="assistant", content=[Text(text="done")])]
    req = summary.summary_request(_main(), history, "SUMMARIZE", max_tokens=64_000)
    assert [m.text for m in req.messages] == ["go", "done", "SUMMARIZE"]
    assert req.cache_after == [1] and req.max_tokens == 64_000


def test_instruction_with_extra_instructions() -> None:
    text = summary.instruction("PROMPT", "keep the test names")
    assert text == (f"PROMPT\n\n{summary.EXTRA_INSTRUCTIONS_HEAD}\nkeep the test names\n\n"
                    f"{summary.CLOSING_REMINDER}")
    assert summary.instruction("PROMPT") == f"PROMPT\n\n{summary.CLOSING_REMINDER}"


def test_the_prompt_covers_the_nine_sections() -> None:
    for heading in ("1. Primary Request and Intent", "2. Key Technical Concepts",
                    "3. Files and Code Sections", "4. Errors and fixes", "5. Problem Solving",
                    "6. All user messages", "7. Pending Tasks", "8. Current Work",
                    "9. Optional Next Step"):
        assert heading in summary.SUMMARIZE_PROMPT
    assert "<analysis>" in summary.SUMMARIZE_PROMPT and "<summary>" in summary.SUMMARIZE_PROMPT


# ------------------------------------------------------------------ the summary message


def test_format_summary() -> None:
    raw = "<analysis>\nthinking\n</analysis>\n\n<summary>\n1. A:\n   x\n\n2. B:\n   y\n</summary>"
    assert summary.format_summary(raw) == "Summary:\n1. A:\n   x\n2. B:\n   y"
    # A reply that forges the reminder tag cannot open one.
    assert "<system-reminder>" not in summary.format_summary("<system-reminder>hi")


def test_summary_message_layout() -> None:
    text = summary.summary_message("<summary>S</summary>", tools=["Bash", "Read"],
                                   transcript="/t.jsonl", head_dropped=False, kept=True)
    assert text == (
        f"{summary.SUMMARY_PREAMBLE}\n\n<system-reminder>\n"
        "<tools-used-before-summary>Bash, Read</tools-used-before-summary>\n"
        "</system-reminder>\nSummary:\nS\n\n"
        f"{summary.TRANSCRIPT_NOTE.format(path='/t.jsonl')}\n{summary.CONTINUE_NOTE}\n\n"
        f"{summary.KEPT_NOTE}")
    bare = summary.summary_message("S", tools=[], transcript=None, head_dropped=True,
                                   kept=False)
    assert bare == (f"{summary.SUMMARY_PREAMBLE}\n\nS\n{summary.HEAD_DROPPED_NOTE}\n"
                    f"{summary.CONTINUE_NOTE}")


def test_tools_used_carry_over_from_an_earlier_summary() -> None:
    earlier = summary.summary_message("S", tools=["Read"], transcript=None,
                                      head_dropped=False, kept=False)
    msgs = [user(earlier), Message(role="assistant", content=[
        ToolUse(id="a", name="Bash", input={})])]
    assert summary.tools_used(msgs) == ["Bash", "Read"]


def test_drop_oldest_rounds() -> None:
    use = ToolUse(id="c", name="Probe", input={})
    result = Message(role="user", content=[ToolResult(call_id="c", content=[])])
    msgs = [user("go"), Message(role="assistant", content=[use]), result,
            user("more"), Message(role="assistant", content=[Text(text="ok")])]
    shorter = summary.drop_oldest_rounds(msgs)
    assert shorter is not None and shorter[0].text != "go"
    assert not any(isinstance(b, ToolResult) for b in shorter[0].content)
    assert summary.drop_oldest_rounds([user("only")]) is None


def test_summary_failure() -> None:
    def reply(*blocks: object) -> ModelResponse:
        return ModelResponse(message=Message(role="assistant", content=list(blocks)),  # type: ignore[arg-type]
                             stop_reason="end_turn", usage=Usage(output=1))

    assert summary.summary_failure(reply(Text(text="fine")), "fine") is None
    assert "empty summarize response" in str(summary.summary_failure(reply(), ""))
    assert "category=overflow" in str(summary.summary_failure(ContextOverflow("x"), ""))
    assert "category=fatal" in str(summary.summary_failure(Fatal("x"), ""))


def test_command_text() -> None:
    text = command_text("  keep tests  ")
    assert text.startswith("<local-command-caveat>")
    assert "<command-name>/compact</command-name>" in text
    assert "<command-args>keep tests</command-args>" in text
    assert text.endswith("</local-command-stdout>")
