"""Calls one after another fold into one counted line, Claude Code's
(``Searched for 2 patterns, read 3 files, ran 2 shell commands``): present
tense while one runs (that call shown under the line), a failure counted in
red, the model's thoughts between the calls in it, a click opening the line
or a call's folded result; paths under the task's directory are shown
relative to it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Delta, Thinking, ToolUse
from noeta.testing import ScriptedProvider, reply
from noeta_tui import render
from noeta_tui.widgets import ApprovalCard, ThinkingBlock, ToolCall, ToolGroup

from support import AskBash, Lines, make_app, open_detail, plain, settle, shown, type_line


class Grep:
    """A search: ``pattern`` "missing" fails."""

    name = "Grep"
    description = "Search."
    input_schema = {"type": "object"}
    read_only = True
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        if input.get("pattern") == "missing":
            return text_output("no such directory", is_error=True)
        return text_output(f"calc.py:1:{input.get('pattern')}")


class Thinks(ScriptedProvider):
    """Reasons a little before each reply."""

    async def complete(self, request: Any, on_delta: Any = None) -> Any:
        if on_delta is not None:
            on_delta(Delta(kind="thinking", text="weighing it up"))
        out = await super().complete(request, on_delta)
        out.message.content.insert(0, Thinking(text="weighing it up"))
        return out


def _files(tmp_path: Path) -> Path:
    work = tmp_path / "w"
    work.mkdir(exist_ok=True)
    for name in ("a.py", "b.py", "c.py"):
        (work / name).write_text(f"# {name}\n")
    return work


def _styles(text: Any) -> str:
    return " ".join(str(span.style) for span in text.spans)


async def test_a_run_of_calls_is_one_counted_line_a_click_opens(tmp_path: Path) -> None:
    work = _files(tmp_path)
    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": str(work / "a.py")}),
              ("Grep", {"pattern": "def add"}),
              ("Read", {"file_path": str(work / "b.py")}),
              ("Bash", {"command": "seq 9", "n": 9}),
              ("Grep", {"pattern": "def sub", "path": str(work)}),
              ("Read", {"file_path": str(work / "c.py")})),
        reply("", ("Bash", {"command": "seq 2", "n": 2})),
        reply("Done looking:\n\n- a\n- b", ("Read", {"file_path": str(work / "a.py")})),
        reply("ok")])
    app = make_app(tmp_path, provider, tools=[Lines(), Grep()])
    async with app.run_test(size=(120, 50)) as pilot:
        await type_line(pilot, "look around")
        await settle(pilot, lambda: provider.steps == [] and not app.busy)
        seven, one = app.query(ToolGroup)
        assert plain(seven.head) == ("● Searched for 2 patterns, read 3 files, ran 2 shell commands "
                                     "(ctrl+o to expand)")
        assert len(seven.calls) == 7 and not any(c.display for c in seven.calls)
        # After a reply of its own, a new run: one call shows as itself, its
        # result under its line.
        assert plain(one.head) == "● Read(a.py)" and one.calls[0].display
        assert not one.calls[0].head.display and "Read 1 line" in plain(one.calls[0].body)
        # Paths under the task's directory are relative; the directory itself is ".".
        heads = [plain(c.head) for c in seven.calls]
        assert "● Read(a.py)" in heads and '● Search(pattern: "def sub", path: ".")' in heads
        await pilot.click(seven.head)
        assert seven.expanded and all(c.display for c in seven.calls)
        assert plain(seven.head) == "● Searched for 2 patterns, read 3 files, ran 2 shell commands"
        bash = next(c for c in seven.calls if c.call.input.get("n") == 9)
        assert "… +6 lines (ctrl+o to expand)" in plain(bash.body)
        await pilot.click(bash.body)
        assert "line 9" in plain(bash.body) and "ctrl+o" not in plain(bash.body)
        await pilot.click(bash.body)
        assert "line 9" not in plain(bash.body)
        await pilot.click(seven.head)
        assert not seven.expanded and not any(c.display for c in seven.calls)
        view = await open_detail(pilot)  # every call and result in full
        assert "line 9" in shown(view) and not seven.expanded


async def test_a_running_call_shows_under_its_runs_present_tense_line(tmp_path: Path) -> None:
    work = _files(tmp_path)
    lines = Lines()
    lines.release.clear()
    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": str(work / "a.py")}),
              ("Bash", {"command": "make", "n": 3})),
        reply("built")])
    app = make_app(tmp_path, provider, tools=[lines])
    async with app.run_test(size=(120, 40)) as pilot:
        await type_line(pilot, "build it")
        await settle(pilot, lambda: any(c.state == "running" and c.call.name == "Bash"
                                        for c in app.query(ToolCall)))
        [group] = app.query(ToolGroup)
        read, bash = group.calls
        await settle(pilot, lambda: read.state == "done")
        assert plain(group.head) == ("● Reading 1 file, running 1 shell command… "
                                     "(ctrl+o to expand)")
        assert bash.display and bash.has_class("-live") and not read.display
        lines.release.set()
        await settle(pilot, lambda: not app.busy)
        assert plain(group.head) == "● Read 1 file, ran 1 shell command (ctrl+o to expand)"
        assert not bash.display and not bash.has_class("-live")


async def test_a_call_waiting_for_approval_shows_and_a_failure_is_counted(
        tmp_path: Path) -> None:
    provider = ScriptedProvider([
        reply("", ("Grep", {"pattern": "missing"}), ("Bash", {"command": "rm -rf build", "n": 1})),
        reply("ok")])
    app = make_app(tmp_path, provider, AskBash(), tools=[Lines(), Grep()])
    async with app.run_test(size=(120, 40)) as pilot:
        await type_line(pilot, "clean up")
        await settle(pilot, lambda: bool(app.query(ApprovalCard)))
        [group] = app.query(ToolGroup)
        grep, bash = group.calls
        await settle(pilot, lambda: grep.state == "error" and bash.state == "waiting")
        head = group.head.render()
        head = getattr(head, "_renderable", head)
        assert plain(group.head).startswith("● Searching for 1 pattern, running 1 shell command…")
        assert "(1 failed)" in plain(group.head) and "red" in _styles(head)
        # What waits shows under the line, and what failed pops out of it.
        assert "yellow" in _styles(head) and bash.display and grep.display
        assert "rm -rf build" in shown(app)  # what would run, on the card
        await pilot.press("y")
        await settle(pilot, lambda: not app.busy)
        assert plain(group.head) == ("● Searched for 1 pattern, ran 1 shell command (1 failed) "
                                     "(ctrl+o to expand)")


async def test_the_models_thoughts_between_calls_join_their_run(tmp_path: Path) -> None:
    work = _files(tmp_path)
    provider = Thinks([reply("", ("Read", {"file_path": str(work / "a.py")})),
                       reply("", ("Bash", {"command": "make", "n": 1})),
                       reply("Done.")])
    app = make_app(tmp_path, provider, tools=[Lines()])
    async with app.run_test(size=(120, 40)) as pilot:
        await type_line(pilot, "look")
        await settle(pilot, lambda: not provider.steps and not app.busy
                     and "Done." in shown(app))
        [group] = app.query(ToolGroup)
        line = plain(group.head)
        assert line.startswith("● Thought for ") and line.endswith(
            "s, read 1 file, ran 1 shell command (ctrl+o to expand)")
        thoughts = [p for p in group.parts if isinstance(p, ThinkingBlock)]
        assert len(thoughts) == 2 and not any(t.display for t in thoughts)
        assert len(app.query(ThinkingBlock)) == 3  # the last one, before the reply, on its own
        view = await open_detail(pilot)
        assert sum("weighing it up" in plain(t) for t in view.query(ThinkingBlock)) == 3


def test_the_run_line_names_each_kind_in_claude_codes_order() -> None:
    def use(name: str) -> ToolUse:
        return ToolUse(id=name, name=name, input={})

    facts = [render.CallFacts(render.call_kind(use(n)), render.call_key(use(n))) for n in
             ("Bash", "WebFetch", "mcp__github__get_issue", "LS", "Glob", "WebSearch",
              "Translate", "mcp__github__list_prs", "mcp__slack__post")]
    line = render.run_line(facts, running=False, waiting=False, failed=0, expanded=True)
    assert line.plain == ("Searched for 1 pattern, listed 1 directory, called github, slack 3 "
                          "times, fetched 1 page, ran 1 web search, called 1 tool, "
                          "ran 1 shell command")
    assert [render.call_kind(use(n)) for n in ("Edit", "Write", "Agent", "TaskCreate",
                                                "AskUserQuestion", "Skill")] == [""] * 6


def test_a_path_is_shown_relative_to_the_tasks_directory() -> None:
    home = str(Path.home())
    assert render.display_path("/work/app/src/calc.py", "/work/app") == "src/calc.py"
    assert render.display_path("/work/other/calc.py", "/work/app") == "/work/other/calc.py"
    assert render.display_path(f"{home}/notes.md", "/work/app") == "~/notes.md"
    assert render.display_path("calc.py", "/work/app") == "calc.py"
    edit = ToolUse(id="e", name="Edit", input={"file_path": "/work/app/calc.py"})
    assert render.tool_header(edit, cwd="/work/app") == "Update(calc.py)"
    assert render.tool_header(edit) == "Update(/work/app/calc.py)"
