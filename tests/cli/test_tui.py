"""The TUI driven headless: type a prompt, see the tool call and the answer,
approve with a key, stop with Esc."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

from noeta.backends.stores.sqlite import SqliteStore
from noeta.cli.tui.app import NoetaApp
from noeta.cli.tui.widgets import (
    ApprovalCard,
    AssistantMessage,
    ToolCall,
    UserMessage,
)
from noeta.core.agent import Agent
from noeta.core.extension import Ask, Extension, TaskView
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import ToolUse
from noeta.host.local import LocalApp
from noeta.testing import ScriptedProvider, reply

SHOTS = Path(__file__).parent / "_shots"


class Look:
    name = "Read"
    description = "Read."
    input_schema = {"type": "object"}
    read_only = True
    clearable = True

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("1\tdef add(a, b):\n2\t    return a - b")


class AskReads(Extension):
    name = "ask"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Ask(prompt="read a file?")


def make_app(tmp_path: Path, provider: ScriptedProvider, *exts: Extension) -> NoetaApp:
    agent = Agent(name="main", model="claude-sonnet-4-5", tools=[Look()], extensions=list(exts))
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                     env_for=lambda _s: None)
    return NoetaApp(local, agent="main", model="claude-sonnet-4-5", cwd=str(tmp_path))


async def settle(pilot: Any, app: NoetaApp, cond: Any, tries: int = 400) -> None:
    for _ in range(tries):
        await pilot.pause(0.05)
        if cond():
            return
    raise AssertionError("UI did not settle")


async def test_prompt_tool_and_answer(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("Let me look.", ("Read", {"file_path": "calc.py"})),
                                 reply("`add` subtracts; it should add.")])
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"what is wrong?")
        await pilot.press("enter")
        await settle(pilot, app, lambda: len(app.query(AssistantMessage)) >= 2 and not app.busy)
        assert [w.render().plain for w in app.query(UserMessage)] == ["what is wrong?"]
        [call] = list(app.query(ToolCall))
        assert call.state == "done" and "calc.py" in call.title
        SHOTS.mkdir(exist_ok=True)
        app.save_screenshot(str(SHOTS / "answer.svg"))


async def test_approval_with_a_key(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Read", {"file_path": "calc.py"})), reply("ok")])
    app = make_app(tmp_path, provider, AskReads())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"read it", "enter")
        await settle(pilot, app, lambda: len(app.query(ApprovalCard)) == 1)
        SHOTS.mkdir(exist_ok=True)
        app.save_screenshot(str(SHOTS / "approval.svg"))
        await pilot.press("y")
        await settle(pilot, app, lambda: not app.busy and len(app.query(AssistantMessage)) == 1)
        [call] = list(app.query(ToolCall))
        assert call.state == "done"


async def test_a_new_task_asks_only_for_its_task_config(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Attached to a server, a new task names no directory or model of its
    own: the server picks them. It names the person's time zone."""
    from noeta.core.events import TaskCreated

    monkeypatch.setenv("TZ", "Asia/Shanghai")

    agent = Agent(name="main", model="claude-sonnet-4-5", tools=[Look()])
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], ScriptedProvider([]),
                     env_for=lambda _s: None)
    app = NoetaApp(local, agent="main", model="the server's model", cwd=str(tmp_path),
                   task_config={})
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot, app, lambda: app.task_id != "")
        first = (await local.client.events(app.task_id))[0].event
        assert isinstance(first, TaskCreated) and first.config == {"timezone": "Asia/Shanghai"}


async def test_shift_tab_cycles_the_permission_mode(tmp_path: Path) -> None:
    from noeta.core.events import External
    from noeta.plugins.permissions import Permissions

    app = make_app(tmp_path, ScriptedProvider([]), Permissions(mode="default"))
    async with app.run_test(size=(140, 30)) as pilot:
        await settle(pilot, app, lambda: app.task_id != "")
        await pilot.press("shift+tab")
        await settle(pilot, app, lambda: app.mode == "acceptEdits")
        inbox = [i.body for i in await app.local.store.inbox(app.task_id)]
        assert External(name="permission_mode", data={"mode": "acceptEdits"}) in inbox


async def test_shift_tab_before_the_mode_is_shown_steps_from_the_tasks_mode(
        tmp_path: Path) -> None:
    """Shift+Tab pressed before the follower has shown the plugin's ``mode``
    and ``modes`` records (or before the task's start recorded them) steps
    from the task's mode, not from a guess (from ``auto`` it would step into
    ``bypassPermissions``)."""
    import asyncio

    from noeta.core.events import External
    from noeta.plugins.permissions import Permissions

    app = make_app(tmp_path, ScriptedProvider([]), Permissions(mode="default"))
    client = app.local.client
    watch, hold = client.watch, asyncio.Event()

    async def held_watch(task_id: str, after: int = 0) -> Any:
        await hold.wait()
        async for rec in watch(task_id, after):
            yield rec

    client.watch = held_watch  # type: ignore[method-assign]
    async with app.run_test(size=(140, 30)) as pilot:
        await settle(pilot, app, lambda: app.task_id != "")
        assert app.mode == ""
        await pilot.press("shift+tab")
        await pilot.pause(0.1)
        hold.set()
        await settle(pilot, app, lambda: app.mode == "acceptEdits")
        await pilot.pause(0.5)
        assert app.mode == "acceptEdits"
        inbox = [i.body for i in await app.local.store.inbox(app.task_id)]
        assert External(name="permission_mode", data={"mode": "acceptEdits"}) in inbox


async def test_shift_tab_steps_only_through_the_modes_the_plugin_offers(
        tmp_path: Path) -> None:
    """A plugin that refuses bypassPermissions publishes a cycle without it,
    so the mode key never steps into it."""
    from noeta.plugins.permissions import Permissions

    app = make_app(tmp_path, ScriptedProvider([]),
                   Permissions(mode="auto", disable_bypass_mode=True))
    async with app.run_test(size=(140, 30)) as pilot:
        await settle(pilot, app, lambda: app.mode == "auto")
        await pilot.pause(0.2)
        await pilot.press("shift+tab")
        await settle(pilot, app, lambda: app.mode != "auto")
        assert app.mode == "default"


async def test_an_approval_decided_before_its_card_mounts() -> None:
    """On a slow event loop a card can be decided after it composed its
    buttons but before its mount handler ran; that handler used to look for
    the removed buttons and take the whole app down."""
    from textual import events, on
    from textual.app import App
    from textual.widgets import Button, Static

    picked: list[str] = []

    async def decide(call_id: str, choice: str) -> None:
        picked.append(choice)

    class DecidedEarly(ApprovalCard):
        @on(events.Mount)  # runs before the card's own on_mount
        async def _decide_first(self) -> None:
            await self.choose("allow")

    options = [{"id": "allow", "label": "Yes"}, {"id": "deny", "label": "No"}]
    app: App[None] = App()
    async with app.run_test() as pilot:
        card = DecidedEarly("c1", "Echo", "", decide, options)
        await app.screen.mount(card)
        await pilot.pause()
        assert app.is_running and app._exception is None
        assert picked == ["allow"] and not card.query(Button)
        [result] = card.query(".approval-result").results(Static)
        assert str(result.render()) == "Yes"


async def test_on_a_server_the_status_shows_the_tasks_directory_and_no_file_is_attached(
        tmp_path: Path) -> None:
    """Attached to a server, the task runs in the server's directory: the
    status line shows the one its task recorded, and an ``@`` mention is
    sent as typed, for the task to attach from its own files."""
    from noeta.core.types import Text

    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    provider = ScriptedProvider([reply("seen")])
    agent = Agent(name="main", model="claude-sonnet-4-5", tools=[Look()])
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                     env_for=lambda _s: None)
    app = NoetaApp(local, agent="main", model="the server's model", cwd=str(tmp_path),
                   task_config={"cwd": "/srv/projects/web"}, on_server=True)
    async with app.run_test(size=(160, 30)) as pilot:
        def status() -> str:
            return str(app.query_one("#status").render())

        await settle(pilot, app, lambda: "/srv/projects/web" in status())
        assert str(tmp_path) not in status()
        await pilot.press(*"look at @calc.py", "enter")
        await settle(pilot, app, lambda: len(provider.requests) == 1 and not app.busy)
        sent = provider.requests[0].messages[-1]
        assert [b.text for b in sent.content if isinstance(b, Text)] == ["look at @calc.py"]


async def test_on_a_server_with_no_directory_asked_the_status_names_the_servers(
        tmp_path: Path) -> None:
    agent = Agent(name="main", model="claude-sonnet-4-5", tools=[Look()])
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], ScriptedProvider([]),
                     env_for=lambda _s: None)
    app = NoetaApp(local, agent="main", model="the server's model", cwd=str(tmp_path),
                   task_config={}, on_server=True)
    async with app.run_test(size=(160, 30)) as pilot:
        await settle(pilot, app, lambda: app.task_id != "")
        status = str(app.query_one("#status").render())
        assert str(tmp_path) not in status and "server's directory" in status


async def test_a_running_call_shows_its_latest_output_then_folds(tmp_path: Path) -> None:
    import asyncio

    release = asyncio.Event()

    class Build:
        name = "Bash"
        description = "Run."
        input_schema = {"type": "object"}
        read_only = False
        clearable = False

        async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
            assert ctx.progress is not None
            ctx.progress("".join(f"step {i}\n" for i in range(1, 9)))
            await release.wait()
            return text_output("built")

    provider = ScriptedProvider([reply("", ("Bash", {"command": "make"})), reply("done")])
    agent = Agent(name="main", model="claude-sonnet-4-5", tools=[Build()])
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                     env_for=lambda _s: None)
    app = NoetaApp(local, agent="main", model="claude-sonnet-4-5", cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"build it")
        await pilot.press("enter")
        await settle(pilot, app, lambda: any(c.progressed for c in app.query(ToolCall)))
        [call] = list(app.query(ToolCall))
        shown = str(call.body.render())
        assert "step 8" in shown and "step 3" not in shown and not call.collapsed
        release.set()
        await settle(pilot, app, lambda: call.state == "done")
        assert call.collapsed
