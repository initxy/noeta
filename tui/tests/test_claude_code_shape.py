"""The terminal UI as Claude Code's: welcome box, footer hints and mode line,
the shortcuts panel, the slash and ``@`` menus, bash mode, tool calls
(folded output, diffs, ctrl+o), the task list and the spinner, queued
messages, the approval dialog's keys, prompt history, Ctrl+C and Esc Esc,
and ``/status`` ``/permissions`` ``/agents`` ``/context``."""

from __future__ import annotations

from pathlib import Path

from noeta.core.types import Text
from noeta.plugins.permissions import Permissions
from noeta.plugins.todos import Todos
from noeta.testing import ScriptedProvider, reply
from noeta_tui.app import NoetaApp
from noeta_tui.widgets import ApprovalCard, Completions, PromptInput, ToolCall, Welcome

from support import MODEL, AskBash, Lines, make_app, plain, ready, settle, shown, type_line


def footer(app: NoetaApp) -> str:
    return plain(app.query_one("#hints"))


async def test_welcome_footer_and_the_shortcuts_panel(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]), Permissions(mode="default"))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        welcome = plain(app.query_one(Welcome))
        assert "noeta  v1.2.3" in welcome and f"model {MODEL}" in welcome
        assert "/help for commands · ? for shortcuts" in welcome
        assert footer(app) == "? for shortcuts"
        await pilot.press("question_mark")
        assert app.query_one("#shortcuts").display
        assert "! for bash mode" in plain(app.query_one("#shortcuts"))
        assert app.query_one(PromptInput).text == ""
        await pilot.press("question_mark")
        assert not app.query_one("#shortcuts").display
        await pilot.press("shift+tab")
        await settle(pilot, lambda: "accept edits on" in footer(app))
        assert footer(app) == "⏵⏵ accept edits on · shift+tab"
        await pilot.press("shift+tab")
        await settle(pilot, lambda: app.mode not in ("default", "acceptEdits"))
        assert footer(app).endswith(" mode on · shift+tab")


async def test_the_slash_menu_lists_and_completes_commands(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: bool(app.plugin_commands))
        menu = app.query_one(Completions)
        await pilot.press("/", "c", "o")
        assert menu.display
        names = [label for _, label, _ in menu.items]
        assert names[:4] == ["/context", "/cost", "/config", "/compact"][:len(names[:4])] \
            or {"/context", "/cost", "/config"} <= set(names)
        assert "Show the tokens and cost" in plain(menu)
        await pilot.press("escape")
        assert not menu.display
        app.query_one(PromptInput).text = ""
        await pilot.press("/", "w", "h")
        assert [label for _, label, _ in menu.items] == ["/where"]  # a plugin's command
        await pilot.press("enter")  # runs the highlighted command
        await settle(pilot, lambda: "you are in" in shown(app))
        await pilot.press("/", "s", "t", "a")
        await pilot.press("tab")
        assert app.query_one(PromptInput).text == "/status "


async def test_at_completes_a_path(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]))
    (tmp_path / "w" / "src").mkdir(parents=True)
    (tmp_path / "w" / "src" / "calc.py").write_text("x = 1\n")
    (tmp_path / "w" / "calendar.md").write_text("")
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        await pilot.press(*"look at @cal")
        menu = app.query_one(Completions)
        assert [value for value, _, _ in menu.items] == ["@calendar.md", "@src/calc.py"]
        await pilot.press("down", "tab")
        assert app.query_one(PromptInput).text == "look at @src/calc.py "
        assert not menu.display


async def test_bash_mode_runs_here_and_goes_with_the_next_message(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("seen")])
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        await pilot.press(*"!echo hi there")
        assert app.query_one("#prompt-box").has_class("-bash")
        assert plain(app.query_one("#marker")) == "!"
        await pilot.press("enter")
        await settle(pilot, lambda: "⎿  hi there" in shown(app))
        assert not provider.requests  # nothing is sent until the next message
        await type_line(pilot, "what did it print?")
        await settle(pilot, lambda: len(provider.requests) == 1 and not app.busy)
        sent = provider.requests[0].messages[-1]
        [text] = [b.text for b in sent.content if isinstance(b, Text)]
        assert text.startswith("<bash-input>echo hi there</bash-input>\n<bash-stdout>hi there")
        assert text.endswith("what did it print?")


async def test_tool_calls_fold_long_output_and_show_diffs(tmp_path: Path) -> None:
    target = tmp_path / "w" / "calc.py"
    provider = ScriptedProvider([
        reply("", ("Bash", {"command": "seq 9", "n": 9})),
        reply("", ("Read", {"file_path": str(target)})),
        reply("", ("Edit", {"file_path": str(target), "old_string": "a - b",
                            "new_string": "a + b"})),
        reply("fixed")])
    app = make_app(tmp_path, provider, tools=[Lines()])
    target.write_text("def add(a, b):\n    return a - b\n")
    async with app.run_test(size=(120, 50)) as pilot:
        await type_line(pilot, "fix it")
        await settle(pilot, lambda: provider.steps == [] and not app.busy)
        bash, read, edit = app.query(ToolCall)
        assert plain(bash.head) == "● Bash(seq 9)"
        assert plain(bash.body).splitlines() == [
            "  ⎿  line 1", "     line 2", "     line 3", "     … +6 lines (ctrl+o to expand)"]
        assert plain(read.body).startswith("  ⎿  Read ")
        path = str(target) if len(str(target)) <= 100 else f"{str(target)[:100]}…"  # macOS's
        assert plain(edit.head) == f"● Update({path})"  # long temporary directories
        body = plain(edit.body)
        assert "Added 1 line, removed 1 line" in body
        assert "2 -    return a - b" in body and "2 +    return a + b" in body
        await pilot.press("ctrl+o")
        assert "line 9" in plain(bash.body)
        await pilot.press("ctrl+o")
        assert "line 9" not in plain(bash.body)
        assert "✻ Worked for" in shown(app)


async def test_the_task_list_and_the_spinner(tmp_path: Path) -> None:
    lines = Lines()
    lines.release.clear()
    provider = ScriptedProvider([
        reply("", ("TaskCreate", {"subject": "Run the tests", "description": "d",
                                  "activeForm": "Running the tests"}),
              ("TaskCreate", {"subject": "Ship", "description": "d"})),
        reply("", ("TaskUpdate", {"taskId": "1", "status": "in_progress"})),
        reply("", ("Bash", {"command": "make test", "n": 1})),
        reply("done")])
    app = make_app(tmp_path, provider, Todos(all_models=True), tools=[lines])
    async with app.run_test(size=(120, 40)) as pilot:
        await type_line(pilot, "go")
        tasks, spinner = app.query_one("#tasks"), app.query_one("#spinner")
        await settle(pilot, lambda: "◼ Run the tests" in plain(tasks))
        assert "◻ Ship" in plain(tasks)
        await settle(pilot, lambda: "Running the tests…" in plain(spinner))
        assert spinner.display and "esc to interrupt" in plain(spinner)
        await pilot.press("ctrl+t")
        assert not tasks.display
        await pilot.press("ctrl+t")
        assert tasks.display
        lines.release.set()
        await settle(pilot, lambda: not app.busy and not spinner.display)


async def test_a_message_sent_during_a_turn_is_queued(tmp_path: Path) -> None:
    lines = Lines()
    lines.release.clear()
    provider = ScriptedProvider([reply("", ("Bash", {"command": "sleep", "n": 1})),
                                 reply("first done"), reply("second done")])
    app = make_app(tmp_path, provider, tools=[lines])
    async with app.run_test(size=(120, 40)) as pilot:
        await type_line(pilot, "first")
        await settle(pilot, lambda: any(c.state == "running" for c in app.query(ToolCall)))
        await type_line(pilot, "and then this")
        queued = app.query_one("#queued")
        assert queued.display and plain(queued) == "❯ and then this"
        lines.release.set()
        await settle(pilot, lambda: provider.steps == [] and not app.busy)
        assert not queued.display


async def test_the_approval_dialog_picks_with_arrows_and_esc_says_no(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Bash", {"command": "rm -rf build", "n": 1})),
                                 reply("", ("Bash", {"command": "ls", "n": 1})), reply("ok")])
    app = make_app(tmp_path, provider, AskBash(), tools=[Lines()])
    async with app.run_test(size=(120, 40)) as pilot:
        await type_line(pilot, "clean")
        await settle(pilot, lambda: len(app.query(ApprovalCard)) == 1)
        card = app.query_one(ApprovalCard)
        text = shown(app)
        assert "Do you want to proceed?" in text and "❯ 1. Yes" in text
        assert "No (esc)" in text and "Esc to cancel" in text
        await pilot.press("escape")  # No
        await settle(pilot, lambda: card.done and not app.busy)
        assert "⎿  No" in plain(card.query_one(".approval-result"))
        assert "declined" in plain(app.query_one(ToolCall).body)  # the turn stops there
        await type_line(pilot, "just list it")
        await settle(pilot, lambda: len(app.query(ApprovalCard)) == 2)
        second = list(app.query(ApprovalCard))[1]
        await pilot.press("down", "up", "enter")  # back to Yes
        await settle(pilot, lambda: second.done)
        assert "⎿  Yes" in plain(second.query_one(".approval-result"))
        await settle(pilot, lambda: provider.steps == [] and not app.busy)
        first, ran = app.query(ToolCall)
        assert first.state == "error" and ran.state == "done"


async def test_history_ctrl_c_and_double_esc(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("one"), reply("two")])
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(120, 36)) as pilot:
        box = app.query_one(PromptInput)
        await type_line(pilot, "first prompt")
        await settle(pilot, lambda: not app.busy and len(provider.requests) == 1)
        await type_line(pilot, "second prompt")
        await settle(pilot, lambda: not app.busy and len(provider.requests) == 2)
        await pilot.press("up")
        assert box.text == "second prompt"
        await pilot.press("up")
        assert box.text == "first prompt"
        await pilot.press("down", "down")
        assert box.text == ""
        await pilot.press(*"draft")
        await pilot.press("ctrl+c")  # clears the input
        assert box.text == "" and app.is_running
        await pilot.press(*"draft")
        await pilot.press("escape", "escape")  # double Esc clears it too
        assert box.text == ""
        await pilot.press("ctrl+c")
        assert footer(app) == "Press Ctrl-C again to exit"
        await pilot.press("ctrl+c")
        await settle(pilot, lambda: not app.is_running)


async def test_status_permissions_agents_and_context(tmp_path: Path) -> None:
    rows = [("store", "sqlite:///t.db"), ("permissions.allow", "Bash(ls *)")]
    app = make_app(tmp_path, ScriptedProvider([reply("hi")]), Permissions(mode="acceptEdits"),
                   settings=lambda: rows)
    async with app.run_test(size=(120, 50)) as pilot:
        await type_line(pilot, "hello")
        await settle(pilot, lambda: not app.busy and app.mode == "acceptEdits")
        await type_line(pilot, "/status")
        await settle(pilot, lambda: "Status" in shown(app))
        text = shown(app)
        assert "version" in text and "1.2.3" in text and app.task_id in text
        assert "sqlite:///t.db" in text and "in this process" in text
        await type_line(pilot, "/permissions")
        await settle(pilot, lambda: "Permissions" in shown(app))
        assert "acceptEdits — Accept edits" in shown(app) and "Bash(ls *)" in shown(app)
        await type_line(pilot, "/agents")
        await settle(pilot, lambda: "❯ main" in shown(app))
        await type_line(pilot, "/context")
        await settle(pilot, lambda: "Context Usage" in shown(app))
        assert "⛁" in shown(app) and "Free space" in shown(app)
