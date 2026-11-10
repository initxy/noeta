"""How calls fold into one line, as Claude Code folds them: a shell command
counts as what it does, a commit or a push as what it made; a run goes on
past an answered approval and the task list; a running read shows as a dim
hint; consecutive edits to one file are one line, and agents started
together are one line with a line each."""

from __future__ import annotations

from pathlib import Path

from noeta.core.types import ToolUse
from noeta.plugins.todos import Todos
from noeta.testing import ScriptedProvider, reply
from noeta_tui import render
from noeta_tui.widgets import ApprovalCard, ToolGroup

from support import AskBash, Lines, make_app, plain, settle, type_line


def test_a_shell_command_counts_as_what_it_does() -> None:
    kind = render.shell_kind
    assert kind("cat a.py") == kind("head -n 5 a.py | wc -l") == "read"
    assert kind("grep -rn foo . 2>/dev/null | head") == kind("rg foo") == "search"
    assert kind("ls -la") == kind("cd src && ls") == "list"
    assert kind("git status") == kind("git log --oneline -5") == kind("git diff HEAD") == "read"
    for writes in ("npm test", "cat a > b", "rm -rf build", "find . -delete", "git commit -m x",
                   "echo $(whoami)", "sed -i s/a/b/ f", "grep x f; make"):
        assert kind(writes) == "shell", writes


def test_a_commit_a_push_and_a_pull_request_are_named() -> None:
    def bash(command: str) -> ToolUse:
        return ToolUse(id=command, name="Bash", input={"command": command})

    assert render.git_ops(bash('git commit -m "fix"'),
                          "[main 1a2b3c4d] fix\n 1 file changed") == [("committed", "1a2b3c4")]
    assert render.git_ops(bash("git push origin main"),
                          "To github.com:a/b.git\n   1a2b3c4..5d6e7f8  main -> main\n") \
        == [("pushed to", "main")]
    assert render.git_ops(bash("gh pr create --fill"),
                          "https://github.com/a/b/pull/12\n") == [("created", "PR #12")]
    assert render.git_ops(bash("git status"), "On branch main") == []
    facts = [render.CallFacts("shell", "1", (("committed", "1a2b3c4"),)),
             render.CallFacts("shell", "2", (("pushed to", "main"),)),
             render.CallFacts("read", "a.py"), render.CallFacts("shell", "3")]
    line = render.run_line(facts, running=False, waiting=False, failed=0, expanded=True)
    assert line.plain == "Committed 1a2b3c4, pushed to main, read 1 file, ran 1 shell command"


def test_a_remark_between_calls_is_short_words() -> None:
    assert render.interstitial("Let me check the tests.")
    assert render.interstitial("Now the config.\nThen the tests.")
    assert not render.interstitial("One.\nTwo.\nThree.")
    assert not render.interstitial("- a list item")
    assert not render.interstitial("```\ncode\n```")
    assert not render.interstitial("x" * 300)


async def test_a_run_goes_on_past_an_answered_approval_and_the_task_list(
        tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    (work / "a.py").write_text("x = 1\n")
    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": str(work / "a.py")}),
              ("Bash", {"command": "make", "n": 1})),
        reply("", ("TaskCreate", {"subject": "Check it", "description": "d"})),
        reply("", ("Grep", {"pattern": "x"})),
        reply("ok")])
    from test_tool_runs import Grep

    app = make_app(tmp_path, provider, AskBash(), Todos(), tools=[Lines(), Grep()])
    async with app.run_test(size=(120, 40)) as pilot:
        await type_line(pilot, "look")
        await settle(pilot, lambda: bool(app.query(ApprovalCard)))
        await pilot.press("y")
        await settle(pilot, lambda: not provider.steps and not app.busy)
        [run] = app.query(ToolGroup)
        assert [c.call.name for c in run.calls] == ["Read", "Bash", "Grep"]
        assert plain(run.head) == ("● Searched for 1 pattern, read 1 file, ran 1 shell command "
                                   "(ctrl+o to expand)")


async def test_a_running_read_shows_as_a_hint_under_its_runs_line(tmp_path: Path) -> None:
    lines = Lines()
    lines.release.clear()
    provider = ScriptedProvider([reply("", ("Bash", {"command": "cat todo.txt", "n": 1}),
                                       ("Bash", {"command": "cat notes.txt", "n": 2})),
                                 reply("read")])
    app = make_app(tmp_path, provider, tools=[lines])
    async with app.run_test(size=(120, 40)) as pilot:
        await type_line(pilot, "read it")
        await settle(pilot, lambda: len(app.calls) == 2
                     and any(c.state == "running" for c in app.calls.values()))
        [run] = app.query(ToolGroup)
        await settle(pilot, lambda: run.hint.display)
        assert plain(run.head) == "● Reading 2 files… (ctrl+o to expand)"
        assert "cat notes.txt" in plain(run.hint) and not any(c.display for c in run.calls)
        lines.release.set()
        await settle(pilot, lambda: not app.busy)
        assert plain(run.head) == "● Read 2 files (ctrl+o to expand)" and not run.hint.display


async def test_edits_to_one_file_one_after_another_are_one_line(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    calc, other = work / "calc.py", work / "other.py"
    calc.write_text("a = 1\nb = 2\nc = 3\n")
    other.write_text("z = 0\n")

    def edit(path: Path, old: str, new: str) -> tuple[str, dict[str, str]]:
        return ("Edit", {"file_path": str(path), "old_string": old, "new_string": new})

    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": str(calc)}), ("Read", {"file_path": str(other)})),
        reply("", edit(calc, "a = 1", "a = 10")),
        reply("", edit(calc, "b = 2", "b = 20")),
        reply("", edit(calc, "c = 3", "c = 30"), edit(other, "z = 0", "z = 9")),
        reply("done")])
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(120, 50)) as pilot:
        await type_line(pilot, "edit them")
        await settle(pilot, lambda: not provider.steps and not app.busy)
        _, three, one = app.query(ToolGroup)
        assert plain(three.head) == "● Edited calc.py ×3 (+3 −3) (ctrl+o to expand)"
        assert not any(c.display for c in three.calls)
        # One edit alone shows as itself, its diff under it.
        assert not one.head.display and one.calls[0].display
        assert "z = 9" in plain(one.calls[0].body)
        await pilot.click(three.head)
        assert all(c.display for c in three.calls)
        assert "a = 10" in plain(three.calls[0].body)


async def test_agents_started_together_are_one_line_with_a_line_each(tmp_path: Path) -> None:
    from test_agents import CHILD, Routed
    from test_agents import make_app as agents_app

    first = {"description": "explore the API", "subagent_type": "Explore", "prompt": CHILD}
    second = {"description": "explore the tests", "subagent_type": "Explore", "prompt": CHILD}
    provider = Routed(main=[reply("", ("Agent", first), ("Agent", second)),
                            reply("They are looking."), reply("All done.")],
                      child=[reply("one is done"), reply("two is done")])
    app = agents_app(tmp_path, provider)
    async with app.run_test(size=(120, 40)) as pilot:
        await type_line(pilot, "explore")
        await settle(pilot, lambda: any(g.kind == "agents" for g in app.query(ToolGroup)))
        [agents] = [g for g in app.query(ToolGroup) if g.kind == "agents"]
        await settle(pilot, lambda: all(c.child is not None for c in agents.calls))
        assert plain(agents.head).startswith("● Running 2 agents…")
        lines = [plain(c.body) for c in agents.calls]
        assert "explore the API" in lines[0] and "explore the tests" in lines[1]
        assert not any(c.head.display for c in agents.calls)  # a line each
        provider.release.set()
        await settle(pilot, lambda: plain(agents.head).startswith("● Ran 2 agents"))
        assert all("Done" in plain(c.body) for c in agents.calls)


async def test_one_call_shows_as_itself_and_its_line_counts_once_a_second_joins(
        tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    (work / "a.py").write_text("x = 1\n")
    lines = Lines()
    lines.release.clear()
    provider = ScriptedProvider([reply("", ("Bash", {"command": "pytest -x", "n": 2})),
                                 reply("", ("Read", {"file_path": str(work / "a.py")})),
                                 reply("ok")])
    app = make_app(tmp_path, provider, tools=[lines])
    async with app.run_test(size=(120, 40)) as pilot:
        await type_line(pilot, "test")
        await settle(pilot, lambda: bool(app.query(ToolGroup)))
        [run] = app.query(ToolGroup)
        await pilot.pause(0.1)
        head, at = run.head, run.head.region.y
        assert plain(head) == "● Bash(pytest -x)" and run.calls[0].display
        lines.release.set()
        await settle(pilot, lambda: not provider.steps and not app.busy)
        # The same line, in the same place, now counting: nothing remounted.
        assert app.query(ToolGroup).first() is run and run.head is head
        assert run.head.region.y == at
        assert plain(head) == "● Read 1 file, ran 1 shell command (ctrl+o to expand)"
        assert not any(c.display for c in run.calls)
