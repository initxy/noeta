"""The terminal UI after the 2026-11-04 review: a task only once something
needs it, the trust question in a folder not yet trusted, the effort over
the prompt and ``/effort``, Ctrl+R over earlier prompts, an edit's diff in
its approval dialog, and reads one after another as one line."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.plugins.permissions import Permissions
from noeta.testing import ScriptedProvider, reply
from noeta_tui import render
from noeta_tui.widgets import ApprovalCard, Completions, Picker, PromptInput, ToolCall, ToolGroup

from support import MODEL, make_app, open_detail, plain, ready, settle, shown, type_line


async def test_a_task_is_created_by_the_first_message_not_by_opening(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("hi")])
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app) and bool(app.plugin_commands))
        await type_line(pilot, "/clear")
        await settle(pilot, lambda: ready(app))
        assert app.task_id == "" and await app.local.client.tasks() == []
        await type_line(pilot, "hello")
        await settle(pilot, lambda: not app.busy and len(provider.requests) == 1)
        [row] = await app.local.client.tasks()
        assert row.task_id == app.task_id
        await type_line(pilot, "/new")
        await settle(pilot, lambda: app.task_id == "")
        assert len(await app.local.client.tasks()) == 1


async def test_an_untrusted_folder_asks_first_and_no_exits(tmp_path: Path) -> None:
    trusted: list[bool] = []
    app = make_app(tmp_path, ScriptedProvider([]), trust=lambda: trusted.append(True))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: bool(app.query(Picker)))
        text = shown(app)
        assert "Accessing workspace:" in text and app.cwd in text
        assert "❯ 1. No, exit" in text and "2. Yes, I trust this folder" in text
        assert not ready(app)  # nothing opens before the answer
        await pilot.press("escape")
        await settle(pilot, lambda: not app.is_running)
    assert trusted == []


async def test_trusting_the_folder_then_opens_the_app(tmp_path: Path) -> None:
    trusted: list[bool] = []
    app = make_app(tmp_path, ScriptedProvider([]), trust=lambda: trusted.append(True))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: bool(app.query(Picker)))
        await pilot.press("2")
        await settle(pilot, lambda: ready(app))
        assert trusted == [True] and app.query_one(PromptInput).has_focus


async def test_the_effort_is_shown_and_set_with_slash_effort(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("one"), reply("two")])
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        effort = app.query_one("#status")  # the footer shows it after the model
        assert f"{MODEL} ◐ medium" in plain(effort)  # the model's default
        await type_line(pilot, "/effort high")  # before the task: it starts with it
        await settle(pilot, lambda: "● high" in plain(effort))
        await type_line(pilot, "go")
        await settle(pilot, lambda: not app.busy and len(provider.requests) == 1)
        await type_line(pilot, "/effort")
        await settle(pilot, lambda: bool(app.query(Picker)))
        await pilot.press("1")  # low, from the next request on
        await settle(pilot, lambda: "○ low" in plain(effort))
        await type_line(pilot, "again")
        await settle(pilot, lambda: not app.busy and len(provider.requests) == 2)
        assert [r.effort for r in provider.requests] == ["high", "low"]
        await type_line(pilot, "/effort extreme")
        await settle(pilot, lambda: "effort is one of low" in shown(app))


async def test_no_effort_for_a_model_not_known_to_take_one(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]))
    app.model = "some-gateway-model"
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        assert "some-gateway-model  ·" in plain(app.query_one("#status"))  # no effort


async def test_model_before_the_first_message_goes_into_the_new_task(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("hi")])
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "/model claude-sonnet-5-5")
        await settle(pilot, lambda: "model set to claude-sonnet-5-5" in shown(app))
        await type_line(pilot, "hi")
        await settle(pilot, lambda: not app.busy and len(provider.requests) == 1)
        assert provider.requests[0].model == "claude-sonnet-5-5"


async def test_ctrl_r_searches_the_earlier_prompts(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("a"), reply("b"), reply("c")])
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(120, 36)) as pilot:
        box, menu = app.query_one(PromptInput), app.query_one(Completions)
        for n, prompt in enumerate(["fix the add test", "write docs", "fix lint"], 1):
            await type_line(pilot, prompt)
            await settle(pilot, lambda n=n: not app.busy and len(provider.requests) == n)
        await pilot.press("ctrl+r")
        assert app.searching and plain(app.query_one("#marker")) == "⌕"
        assert [v for v, _, _ in menu.items] == ["fix lint", "write docs", "fix the add test"]
        await pilot.press(*"fix")
        assert [v for v, _, _ in menu.items] == ["fix lint", "fix the add test"]
        await pilot.press("down", "enter")
        assert box.text == "fix the add test" and not app.searching and not menu.display
        assert len(provider.requests) == 3  # put in the box, not sent
        await pilot.press("ctrl+r", "escape")
        assert box.text == "" and not app.searching


async def test_an_edit_approval_shows_the_diff_it_would_make(tmp_path: Path) -> None:
    target = tmp_path / "w" / "calc.py"
    target.parent.mkdir(exist_ok=True)
    target.write_text("import math\n\n\ndef add(a, b):\n    return a - b\n")
    provider = ScriptedProvider([reply("", ("Edit", {"file_path": str(target),
                                                     "old_string": "a - b",
                                                     "new_string": "a + b"})),
                                 reply("done")])
    app = make_app(tmp_path, provider, Permissions(mode="default"))
    async with app.run_test(size=(120, 40)) as pilot:
        await type_line(pilot, "fix add")
        await settle(pilot, lambda: bool(app.query(ApprovalCard)))
        diff = plain(app.query_one(".approval-diff"))
        assert "   5 -    return a - b" in diff and "   5 +    return a + b" in diff
        assert "   4  def add(a, b):" in diff  # the file's own line numbers
        await pilot.press("1")
        await settle(pilot, lambda: not app.busy and provider.steps == [])
        assert "⎿  Yes" in plain(app.query_one(".approval-result"))


def test_the_preview_of_a_new_file_and_of_an_edit_not_found_here() -> None:
    written = render.edit_preview("Write", {"content": "a\nb\n"}, None)
    assert written == [("   1 +a", "green"), ("   2 +b", "green")]
    edit = render.edit_preview("Edit", {"old_string": "x = 1", "new_string": "x = 2"}, None)
    assert edit == [("   1 -x = 1", "red"), ("   1 +x = 2", "green")]
    every = render.edit_preview("Edit", {"old_string": "a", "new_string": "b",
                                         "replace_all": True}, "a\na\n")
    assert [row for row, _ in every] == ["   1 -a", "   2 -a", "   1 +b", "   2 +b"]
    long = render.edit_preview("Write", {"content": "x\n" * 40}, None)
    assert len(long) == render.PREVIEW_LINES + 1 and long[-1][0] == "… +10 lines"
    assert render.edit_preview("Bash", {}, None) == []


async def test_reads_one_after_another_are_one_line(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (work / name).write_text(f"{name}\n")
    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": str(work / "a.txt")}),
              ("Read", {"file_path": str(work / "b.txt")})),
        reply("", ("Read", {"file_path": str(work / "c.txt")})),
        reply("Now one more.", ("Read", {"file_path": str(work / "a.txt")})),
        reply("done")])
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(120, 40)) as pilot:
        await type_line(pilot, "read them")
        await settle(pilot, lambda: not app.busy and provider.steps == [])
        three, one = app.query(ToolGroup)
        assert plain(three.head) == "● Read 3 files (ctrl+o to expand)"
        assert not any(c.display for c in three.calls)
        assert not one.head.display and one.calls[0].display  # alone: shown as itself
        view = await open_detail(pilot)  # each call on its own line there
        assert len(view.query(ToolCall)) == 4 and not three.expanded
        assert MODEL in plain(app.query_one("#status"))


async def test_continue_opens_the_newest_task_here_with_the_effort_it_runs_with(
        tmp_path: Path) -> None:
    first = make_app(tmp_path, ScriptedProvider([reply("hi")]))
    async with first.run_test(size=(120, 36)) as pilot:
        await type_line(pilot, "hello")
        await settle(pilot, lambda: not first.busy and bool(first.task_id))
        made = first.task_id
    again = make_app(tmp_path, ScriptedProvider([]), continue_last=True)
    again.task_config["effort"] = "high"  # the command's, not the task's
    async with again.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: again.task_id == made and "hello" in shown(again))
        assert "◐ medium" in plain(again.query_one("#status"))  # the model's default


async def test_the_command_lines_prompt_is_the_first_message(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("hi"), reply("again")])
    app = make_app(tmp_path, provider, prompt="hello")
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: not app.busy and "hi" in shown(app))
        made = app.task_id
        assert made and "hello" in shown(app)
    # with -c it goes to the task continued, and while picking one it waits
    again = make_app(tmp_path, provider, continue_last=True, prompt="more")
    async with again.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: not again.busy and "again" in shown(again))
        assert again.task_id == made and len(provider.requests) == 2
    picking = make_app(tmp_path, ScriptedProvider([]), resume="", prompt="later")
    async with picking.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: bool(picking.query(Picker)))
        assert picking.query_one(PromptInput).text == "later"


async def test_continue_with_nothing_here_says_so_and_exits(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]), continue_last=True)
    said: list[str] = []
    real_exit = app.exit

    def exit(*args: Any, **kwargs: Any) -> None:
        said.append(str(kwargs.get("message")))
        real_exit(*args, **kwargs)

    app.exit = exit  # type: ignore[method-assign]
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: app.return_code is not None)
    assert app.return_code == 1 and app.task_id == ""
    assert said == ["No conversation found to continue"]
