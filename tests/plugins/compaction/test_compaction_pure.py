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
    # Claude Code with a 100,000-token window cap and a 30 % override
    # (NOETA_AUTO_COMPACT_WINDOW, NOETA_AUTOCOMPACT_PCT_OVERRIDE here):
    # min(30 % of 80K, 80K - 13K).
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
    assert text_tokens("压缩摘要") == 4 and text_tokens("ab压缩") == 3
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
        "read it\n", "SUMMARIZE"]  # joined as Claude Code joins two messages
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


def test_a_named_gap_drops_just_enough_rounds() -> None:
    # Claude Code drops rounds until their tokens cover the gap the error
    # names, keeping at least the last.
    rounds = [user("x" * 400), Message(role="assistant", content=[Text(text="y" * 400)])] * 5
    over = ContextOverflow("prompt is too long: 1010 tokens > 1000 maximum")
    gap = summary.overflow_gap(over)
    assert gap == 10
    shorter = summary.drop_oldest_rounds(rounds, gap)
    assert shorter is not None and len(shorter) == len(rounds)  # one round out, a stand-in in
    assert shorter[0].text == summary.EARLIER_LEFT_OUT
    # The stand-in from an earlier retry is not a round of its own.
    again = summary.drop_oldest_rounds(shorter, gap)
    assert again is not None and again[0].text != summary.EARLIER_LEFT_OUT
    assert len(again) == len(shorter) - 2
    # Never the last round (here a reply, behind its stand-in).
    assert len(summary.drop_oldest_rounds(rounds, 10**9) or []) == 2
    assert summary.overflow_gap(ContextOverflow("context window exceeded")) is None
    assert summary.overflow_gap(ContextOverflow("x", body="Prompt is too long: 5 tokens > 9")) \
        is None


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


def test_the_context_is_the_last_usage_plus_what_came_after() -> None:
    """Claude Code's estimate: the newest reply's reported usage anchors it;
    the character estimate (which over-counts CJK text) is used only while
    no reply has reported usage."""
    from noeta.core.loop.state import TaskState

    cjk = "压缩" * 5_000  # 10,000 estimated tokens
    messages = [user(cjk), Message(role="assistant", content=[Text(text="ok")]), user("more")]
    request = ModelRequest(model="m", messages=messages)
    state = TaskState(messages=messages)
    full, _ = Compaction()._tokens(state, request, len(messages))
    assert full >= 10_000
    state.last_usage = Usage(uncached=3_000, cache_read=1_000, output=20)
    tokens, before = Compaction()._tokens(state, request, len(messages))
    assert before == 4_020
    assert tokens == 4_020 + messages_tokens([user("more")])


def _read(call: str, path: str) -> list[Message]:
    return [Message(role="assistant", content=[
                ToolUse(id=call, name="Read", input={"file_path": path})]),
            Message(role="user", content=[ToolResult(call_id=call, content=[Text(text="x")])])]


def test_restore_candidates_follow_the_read_state_a_compaction_resets() -> None:
    """Claude Code clears its read-file state at a compaction and refills
    it with the files restored in full: a file touched before the previous
    compaction comes back only if that compaction restored it."""
    from noeta.plugins.compaction.restore import recent_files

    history = [user("go"), *_read("a", "/w/old.txt"), *_read("b", "/w/big.txt"),
               *_read("c", "/w/kept.txt"), *_read("d", "/w/new.txt"), user("next")]
    # Without a previous compaction every touch counts, newest first.
    assert recent_files(history, len(history)) == [
        "/w/new.txt", "/w/kept.txt", "/w/big.txt", "/w/old.txt"]
    # The previous compaction ran after message 7 and restored only old.txt
    # (big.txt got the too-large note, kept.txt sat in its kept messages).
    assert recent_files(history, len(history), since=7, carried=["/w/old.txt"]) == [
        "/w/new.txt", "/w/old.txt"]


def test_files_the_conversation_carries_anyway_are_not_restored() -> None:
    """Claude Code's exact-path filter: the instruction files the instructions
    resident gives again (start and nested), the memory index and the plan
    file stay out; a nested ``NOETA.md`` that was never shown is restored like
    any file."""
    from noeta.plugins.compaction.extension import _carried_files
    from noeta.plugins.compaction.restore import recent_files

    class Plan:
        plan = {"path": "/home/p/plans/plan.md"}

    ext = {"instructions": {"files": [{"path": "/w/NOETA.md", "real": "/w/NOETA.md"}],
                            "nested": [{"path": "/w/sub/AGENTS.md", "real": "/w/sub/AGENTS.md"}],
                            "digests": {"/home/m/MEMORY.md": "d"}},
           "memory": {"dir": "/home/m"}, "plan_mode": Plan()}
    skip = _carried_files(ext, {})
    history = [user("go"), *_read("a", "NOETA.md"), *_read("b", "/w/sub/AGENTS.md"),
               *_read("c", "/home/m/MEMORY.md"), *_read("d", "/home/p/plans/plan.md"),
               *_read("e", "/w/other/NOETA.md"), *_read("f", "/w/x.py"), user("next")]
    assert recent_files(history, len(history), cwd="/w", skip=skip) == [
        "/w/x.py", "/w/other/NOETA.md"]
    # Not given again after a compaction (``rehang=False``), or a memory
    # index the instructions never showed: restored like any file.
    assert _carried_files(ext, {}, rehung=False) == {"/home/p/plans/plan.md"}
    shown = {**ext, "instructions": {**ext["instructions"], "digests": {}}}
    assert "/home/m/MEMORY.md" not in _carried_files(shown, {})


def _shell(call: str, command: str, output: str = "x", *, failed: bool = False
           ) -> list[Message]:
    return [Message(role="assistant", content=[
                ToolUse(id=call, name="Bash", input={"command": command})]),
            Message(role="user", content=[ToolResult(call_id=call, content=[Text(text=output)],
                                                     is_error=failed)])]


def test_files_a_shell_command_showed_are_restored_too() -> None:
    """Claude Code's read-file state takes in what a ``Bash`` command
    printed of a file (``cat``, ``head``, ``sed -n`` ...), so a compaction
    restores it like a ``Read``; relative paths are taken from the task's
    directory."""
    from noeta.plugins.compaction.restore import recent_files
    from noeta.toolkit.shell import NO_OUTPUT

    history = [user("go"), *_read("a", "/w/read.txt"),
               *_shell("b", "cat skills/x/SKILL.md"),
               *_shell("c", "cat gone.txt", "No such file", failed=True),
               *_shell("d", "grep -n nothing quiet.txt", NO_OUTPUT),
               *_shell("e", "grep -n TODO found.txt", "3:TODO"),
               *_shell("f", "cat a.txt | wc -l"),
               *_shell("g", "head -n 5 ./src/a.py && echo --- && sed -n '1,9p' /abs/b.py"),
               user("next")]
    assert recent_files(history, len(history), cwd="/w") == [
        "/abs/b.py", "/w/src/a.py", "/w/found.txt", "/w/skills/x/SKILL.md", "/w/read.txt"]


def test_a_relative_path_a_command_showed_is_taken_from_where_it_ran() -> None:
    """``cd frontend`` then ``cat package.json``: the restore reads
    ``frontend/package.json`` (from the file tools' record of the call, else
    the shell's directory), never the one in the task's directory."""
    from noeta.plugins.compaction.restore import recent_files

    history = [user("go"), *_shell("a", "cd frontend", ""),
               *_shell("b", "cat package.json"), user("next")]
    # The shell has moved on since; the record of call b says where it ran.
    shown = {"b": ["/w/frontend/package.json"]}
    assert recent_files(history, len(history), cwd="/w", shown=shown) == [
        "/w/frontend/package.json"]
    # Without a record of the call: the shell's directory now.
    assert recent_files(history[:5], 5, cwd="/w/frontend") == ["/w/frontend/package.json"]


def test_the_restore_finds_the_shell_directory_and_the_calls_by_name() -> None:
    from noeta.plugins.fs.state import FileRead, FsState
    from noeta.plugins.shell.state import ShellState
    from noeta.toolkit.plugin_records import files_shown_by_commands, shell_cwd

    ext = {"shell": ShellState(cwd="/w/frontend"),
           "fs": FsState(reads={"/w/a": FileRead(digest="d"),
                                "/w/frontend/package.json": FileRead(digest="d", call_id="b")})}
    assert shell_cwd(ext) == "/w/frontend"
    assert shell_cwd({}) is None
    assert files_shown_by_commands(ext) == {"b": ["/w/frontend/package.json"]}
    assert files_shown_by_commands({}) == {}


def test_the_fallback_summarize_request() -> None:
    """Claude Code's fallback call: its own plain system prompt, only
    ``Read``, images as ``[image]`` (in tool results too), no cache marks;
    the conversation and the rest of the request as the first call's."""
    from noeta.core.types import SystemPart, ToolSpec
    pic = Image(media_type="image/png", data=b"x")
    first = ModelRequest(
        model="m", system=[SystemPart(text="main", cache=True)],
        tools=[ToolSpec(name="Bash", description="", input_schema={}),
               ToolSpec(name="Read", description="", input_schema={})],
        messages=[user("look"), Message(role="user", content=[Text(text="see"), pic]),
                  Message(role="assistant", content=[ToolUse(id="c", name="Read", input={})]),
                  Message(role="user", content=[ToolResult(call_id="c", content=[pic])])],
        cache_after=[2], cache_blocks=[(1, 0)], max_tokens=99)
    fb = summary.fallback_request(first)
    assert fb.system == [SystemPart(text=summary.FALLBACK_SYSTEM)]
    assert [t.name for t in fb.tools] == ["Read"]
    assert fb.cache_after == [] and fb.cache_blocks == [] and fb.max_tokens == 99
    assert fb.messages[0] is first.messages[0]
    assert fb.messages[1].content == [Text(text="see"), Text(text=summary.IMAGE_PLACEHOLDER)]
    result = fb.messages[3].content[0]
    assert isinstance(result, ToolResult)
    assert result.content == [Text(text=summary.IMAGE_PLACEHOLDER)]


def test_the_fallback_call_offers_tool_search_and_mcp_tools_when_searching() -> None:
    """Claude Code's fallback offers ToolSearch and every MCP tool besides
    Read when tool search is on, and only Read otherwise."""
    from noeta.core.types import ToolSpec

    def spec(name: str) -> ToolSpec:
        return ToolSpec(name=name, description="", input_schema={})

    tools = [spec("Bash"), spec("mcp__db__query"), spec("Read"), spec("ToolSearch"),
             spec("mcp__gh__issue")]
    fb = summary.fallback_request(ModelRequest(model="m", tools=tools))
    assert [t.name for t in fb.tools] == ["Read", "ToolSearch", "mcp__db__query",
                                          "mcp__gh__issue"]
    plain = summary.fallback_request(ModelRequest(
        model="m", tools=[t for t in tools if t.name != "ToolSearch"]))
    assert [t.name for t in plain.tools] == ["Read"]
    # Claude Code's list goes through a by-name de-duplication (lodash
    # ``uniqBy``): a name offered twice is kept once, where it came first.
    twice = summary.fallback_request(ModelRequest(model="m", tools=[
        *tools, ToolSpec(name="mcp__db__query", description="again", input_schema={})]))
    assert [t.name for t in twice.tools] == ["Read", "ToolSearch", "mcp__db__query",
                                             "mcp__gh__issue"]
    assert next(t for t in twice.tools if t.name == "mcp__db__query").description == ""


def test_a_continued_task_counts_reads_from_its_own_start() -> None:
    """The last compaction's ``seen_through`` is a seq of the source's log:
    carried on, it would hide the continuing task's own early reads from
    its first compaction. Its carried messages sit at seq 0."""
    ext = Compaction()
    st = ext.reduce(ext.initial_state(), "compacted",
                    {"seen_through": 50, "restored_paths": ["/w/a.py"], "turn": 7})
    carried = ext.continue_state(st)
    assert carried.seen_through == 0 and carried.restored_paths == ("/w/a.py",)
