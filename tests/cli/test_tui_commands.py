"""The TUI's slash commands with a mechanism behind them (/model, /cost,
/context, /memory, /config, /rewind and Esc Esc, /clear, /resume <n>),
``@`` mentions, pasted images and local plugin commands."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

from textual.widgets import Markdown

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.cli.tui.app import NoetaApp
from noeta.cli.tui.widgets import Notice, PromptInput
from noeta.core.agent import Agent
from noeta.core.events import External
from noeta.core.extension import Command, CommandContext, Extension
from noeta.core.types import Image, Message, Text
from noeta.host.local import LocalApp
from noeta.plugins.checkpoints import Checkpoints
from noeta.plugins.fs import FileTools
from noeta.testing import ScriptedProvider, reply
from noeta.toolkit.signals import RESUMED_SIGNAL
from tests.images import png as a_png

MODEL = "claude-opus-5-5"


class Where(Extension):
    name = "where"

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return [Command(name="where", description="Where am I", local=True)]

    async def run_command(self, ctx: CommandContext, name: str, args: str) -> str:
        return f"you are in {ctx.cwd}"


def make_app(tmp_path: Path, provider: ScriptedProvider) -> NoetaApp:
    work = tmp_path / "w"
    work.mkdir(exist_ok=True)
    agent = Agent(name="main", model=MODEL, extensions=[FileTools(), Checkpoints(), Where()])
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                     env_for=lambda _s: LocalExecEnv(str(work)), own_tasks_only=True)
    return NoetaApp(local, agent="main", model=MODEL, cwd=str(work))


async def settle(pilot: Any, cond: Any, tries: int = 400) -> None:
    for _ in range(tries):
        await pilot.pause(0.05)
        if cond():
            return
    raise AssertionError("UI did not settle")


def shown(app: NoetaApp) -> str:
    out = [w.render().plain for w in app.query(Notice)]
    out += [m.source for m in app.query(Markdown)]
    return "\n".join(out)


async def type_line(pilot: Any, text: str) -> None:
    app = pilot.app
    app.query_one(PromptInput).text = text
    await pilot.press("enter")


async def test_model_cost_context_and_a_local_command(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("first"), reply("second")])
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(110, 40)) as pilot:
        await type_line(pilot, "hello")
        await settle(pilot, lambda: len(provider.requests) == 1 and not app.busy)
        await type_line(pilot, "/model sonnet")
        await settle(pilot, lambda: "model set to claude-sonnet-5-5" in shown(app))
        await type_line(pilot, "again")
        await settle(pilot, lambda: len(provider.requests) == 2 and not app.busy)
        assert [r.model for r in provider.requests] == [MODEL, "claude-sonnet-5-5"]
        await type_line(pilot, "/cost")
        await settle(pilot, lambda: "Total cost" in shown(app))
        assert f"{MODEL}:" in shown(app) and "claude-sonnet-5-5:" in shown(app)
        await type_line(pilot, "/context")
        await settle(pilot, lambda: "Free space" in shown(app))
        await type_line(pilot, "/where")
        await settle(pilot, lambda: "you are in" in shown(app))
        assert len(provider.requests) == 2  # nothing sent for a local command
        await type_line(pilot, "/memory")
        await settle(pilot, lambda: "user instructions" in shown(app))
        await type_line(pilot, "/config")
        await settle(pilot, lambda: "**Config**" in shown(app))


async def test_rewind_restores_files_and_forks_the_conversation(tmp_path: Path) -> None:
    target = tmp_path / "w" / "notes.txt"
    provider = ScriptedProvider([
        reply("", ("Write", {"file_path": str(target), "content": "one\n"})), reply("wrote"),
        reply("", ("Edit", {"file_path": str(target), "old_string": "one",
                            "new_string": "two"})), reply("edited"),
        reply("after the rewind")])
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(110, 40)) as pilot:
        await type_line(pilot, "write notes")
        await settle(pilot, lambda: len(provider.requests) == 2 and not app.busy)
        await type_line(pilot, "change them")
        await settle(pilot, lambda: len(provider.requests) == 4 and not app.busy)
        assert target.read_text() == "two\n"
        first = app.task_id
        await pilot.press("escape", "escape")
        await settle(pilot, lambda: "**Rewind**" in shown(app))
        assert "1 file(s) changed since" in shown(app)
        await type_line(pilot, "/rewind 2")
        await settle(pilot, lambda: app.task_id != first and target.read_text() == "one\n")
        assert app.query_one(PromptInput).text == "change them"
        await type_line(pilot, "something else")
        await settle(pilot, lambda: len(provider.requests) == 5 and not app.busy)
        texts = [m.text for m in provider.requests[-1].messages if m.text]
        assert "write notes" in texts and "change them" not in texts


async def test_mentions_images_clear_and_resume(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("seen"), reply("new task")])
    app = make_app(tmp_path, provider)
    work = tmp_path / "w"
    (work / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    png = tmp_path / "shot.png"
    png.write_bytes(a_png(4, 4))
    async with app.run_test(size=(110, 40)) as pilot:
        await app.add_image(Image(media_type="image/png", data=png.read_bytes()), "shot.png")
        await type_line(pilot, "look at @calc.py " + app.query_one(PromptInput).text)
        await settle(pilot, lambda: len(provider.requests) == 1 and not app.busy)
        # The attachment goes ahead of the person's text, as Claude Code sends it; the
        # pasted image is also saved as a file the model is told of, after it.
        *_, attached, sent, saved = provider.requests[0].messages
        assert saved.origin == "system" and "[Image source: " in saved.text
        [prompt] = [b for b in sent.content if isinstance(b, Text)]
        assert prompt.text.startswith("look at @calc.py")
        assert [b.media_type for b in sent.content if isinstance(b, Image)] == ["image/png"]
        # The file is attached inside the task, by the file tools.
        assert attached.origin == "system" and "1\tdef add(a, b):" in attached.text
        assert str(work / "calc.py") in attached.text
        first = app.task_id
        await type_line(pilot, "/clear")
        await settle(pilot, lambda: app.task_id != first)
        await type_line(pilot, "/resume")
        await settle(pilot, lambda: "/resume <n>" in shown(app))
        await type_line(pilot, f"/resume {app.listed.index(first) + 1}")
        await settle(pilot, lambda: app.task_id == first)
        # The reopened task is told a person follows it, and that it was
        # resumed (its plugins act on it).
        posted = [p.body for p in await app.local.store.inbox(first)
                  if isinstance(p.body, External)]
        assert [b.name for b in posted] == ["config", RESUMED_SIGNAL]
        assert posted[0].data == {"interactive": None, "permission_prompt_tool": None,
                                  "client_hooks": None}


async def test_reload_plugins_reports_what_changed(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]))
    local = app.local
    async with app.run_test(size=(110, 40)) as pilot:
        await type_line(pilot, "/reload-plugins")
        await settle(pilot, lambda: "restart them" in shown(app))
        local.reload_plugins = lambda: (local.replace_agents(
            [Agent(name="main", model=MODEL, extensions=[FileTools(), Checkpoints()])])
            or ["a notice"])
        await type_line(pilot, "/reload-plugins")
        await settle(pilot, lambda: "plugins reloaded" in shown(app))
        assert "removed where" in shown(app) and "a notice" in shown(app)


async def test_a_task_a_headless_run_resumed_is_interactive_again_in_the_tui(
        tmp_path: Path) -> None:
    """``noeta -p --resume`` marks the task not interactive for its run; the
    terminal reopening it takes that back, so ``AskUserQuestion`` is offered
    again (Claude Code decides per process)."""
    from noeta.host.stream_json import HeadlessOptions, StreamJson, drive
    from noeta.plugins.ask_user import AskUser

    provider = ScriptedProvider([reply("first"), reply("headless"), reply("in the terminal")])
    agent = Agent(name="main", model=MODEL, extensions=[AskUser()])

    def local_app() -> LocalApp:
        return LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                        env_for=lambda _s: None, own_tasks_only=True)

    local = local_app()
    async with local:
        tid = await local.client.create("main", "start", config={"cwd": str(tmp_path)})
        await local.client.wait(tid, timeout=10)
        options = HeadlessOptions(model=MODEL, cwd=str(tmp_path), resume=tid)

        async def emit(_m: Any) -> None:
            return None

        await drive(StreamJson(local, options, emit),
                    prompt=Message(role="user", content=[Text(text="go")], origin="human"))
    app = NoetaApp(local_app(), agent="main", model=MODEL, cwd=str(tmp_path), resume=tid)
    async with app.run_test(size=(110, 40)) as pilot:
        await settle(pilot, lambda: app.task_id == tid)
        await type_line(pilot, "and now?")
        await settle(pilot, lambda: len(provider.requests) == 3 and not app.busy)
    names = [[t.name for t in r.tools] for r in provider.requests]
    assert "AskUserQuestion" in names[0]
    assert "AskUserQuestion" not in names[1]
    assert "AskUserQuestion" in names[2]
