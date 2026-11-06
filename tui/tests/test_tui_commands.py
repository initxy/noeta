"""The TUI's slash commands with a mechanism behind them (/model, /cost,
/context, /memory, /config, /rewind and Esc Esc, /clear, /resume and its list),
``@`` mentions, pasted images and local plugin commands."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.events import External, TaskCreated
from noeta.core.extension import Command, CommandContext, Extension
from noeta.core.store import NewTask
from noeta.core.types import Image, Message, Text
from noeta.host.local import LocalApp
from noeta.plugins.checkpoints import Checkpoints
from noeta.plugins.fs import FileTools
from noeta.testing import ScriptedProvider, reply
from noeta.toolkit.signals import RESUMED_SIGNAL
from noeta_tui.app import NoetaApp
from noeta_tui.widgets import Choices, Picker, PromptInput

from support import png as a_png
from support import settle, shown, type_line

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
        await settle(pilot, lambda: "Config" in shown(app) and "working directory" in shown(app))


async def test_model_lists_every_providers_models(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("first")])
    app = make_app(tmp_path, provider)
    app.local.list_models = lambda: [("gw-chat", "gateway"), ("claude-opus-5-5", "anthropic")]
    prepared: list[str] = []
    app.local.prepare_model = lambda m: prepared.append(m) or "about gw-chat"
    async with app.run_test(size=(110, 40)) as pilot:
        await type_line(pilot, "hello")
        await settle(pilot, lambda: len(provider.requests) == 1 and not app.busy)
        await type_line(pilot, "/model")
        await settle(pilot, lambda: len(app.query(Picker)) == 1)
        choices = app.query_one(Choices)
        ids = [o.id for o in choices.options]
        assert "claude-opus-5-5" in ids and "gw-chat" in ids and "sonnet" in ids
        assert "claude-opus-5-5  anthropic" in shown(app)
        choices.index = ids.index("gw-chat")
        await pilot.press("enter")
        await settle(pilot, lambda: "model set to gw-chat" in shown(app))
        assert not app.query(Picker) and app.focused is app.query_one(PromptInput)
        assert prepared == ["gw-chat"] and "about gw-chat" in shown(app)


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
        await settle(pilot, lambda: len(app.query(Picker)) == 1)
        assert "1 file(s) changed since" in shown(app)
        await pilot.press("2")  # the second prompt
        await settle(pilot, lambda: "Restore code and conversation" in shown(app))
        await pilot.press("enter")
        await settle(pilot, lambda: app.task_id != first and target.read_text() == "one\n")
        assert app.query_one(PromptInput).text == "change them"
        await type_line(pilot, "something else")
        await settle(pilot, lambda: len(provider.requests) == 5 and not app.busy)
        texts = [m.text for m in provider.requests[-1].messages if m.text]
        assert "write notes" in texts and "change them" not in texts


async def test_rewind_on_a_server(tmp_path: Path) -> None:
    """With ``--server`` the rewind is the server's (``POST /tasks/{id}/rewind``):
    the task's config holds keys the server wrote (``prompts``) that a
    request may not set, so a fork the client made itself was refused."""
    import asyncio

    pytest.importorskip("starlette")
    uvicorn = pytest.importorskip("uvicorn")
    from noeta.host.http import create_app
    from noeta.host.remote import RemoteApp

    work = tmp_path / "w"
    work.mkdir()
    target = work / "notes.txt"
    provider = ScriptedProvider([
        reply("", ("Write", {"file_path": str(target), "content": "one\n"})), reply("wrote"),
        reply("", ("Edit", {"file_path": str(target), "old_string": "one",
                            "new_string": "two"})), reply("edited")])
    agent = Agent(name="main", model=MODEL, extensions=[FileTools(), Checkpoints()])
    server = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                      env_for=lambda _s: LocalExecEnv(str(work)),
                      task_config={"prompts": {"system/main": "You help."}})
    await server.start()
    api = create_app(server.client, token="secret", app=server, cwd=str(work))
    served = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=0, log_level="error"))
    serving = asyncio.create_task(served.serve())
    while not served.started:
        await asyncio.sleep(0.01)
    port = served.servers[0].sockets[0].getsockname()[1]
    app = NoetaApp(RemoteApp(f"http://127.0.0.1:{port}", token="secret"), agent="main",
                   model=MODEL, cwd=str(work), task_config={}, on_server=True)
    try:
        async with app.run_test(size=(110, 40)) as pilot:
            await type_line(pilot, "write notes")
            await settle(pilot, lambda: len(provider.requests) == 2 and not app.busy)
            await type_line(pilot, "change them")
            await settle(pilot, lambda: len(provider.requests) == 4 and not app.busy)
            first = app.task_id
            created = (await server.client.events(first))[0].event
            assert isinstance(created, TaskCreated) and "prompts" in created.config
            await type_line(pilot, "/rewind 2")
            await settle(pilot, lambda: app.task_id != first and target.read_text() == "one\n")
            await settle(pilot, lambda: "rewound to before prompt 2; 1 file(s) restored"
                         in shown(app))
            assert app.query_one(PromptInput).text == "change them"
            fork = (await server.client.events(app.task_id))[0].event
            assert isinstance(fork, TaskCreated) and fork.history_from == first
            assert fork.config["continued_as"] == "rewind"
    finally:
        served.should_exit = True
        await serving
        await server.stop()


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
        await settle(pilot, lambda: len(app.query(Picker)) == 1)
        assert "look at @calc.py" in shown(app)  # a task shows its first prompt
        await pilot.press(str(app.listed.index(first) + 1))
        await settle(pilot, lambda: app.task_id == first)
        await type_line(pilot, "/clear")
        await settle(pilot, lambda: app.task_id != first)
        await type_line(pilot, f"/resume {app.listed.index(first) + 1}")
        await settle(pilot, lambda: app.task_id == first)
        # The reopened task is told a person follows it, and that it was
        # resumed (its plugins act on it).
        posted = [p.body for p in await app.local.store.inbox(first)
                  if isinstance(p.body, External)]
        assert [b.name for b in posted] == ["config", RESUMED_SIGNAL] * 2
        assert posted[-2].data == {"interactive": None, "permission_prompt_tool": None,
                                  "client_hooks": None}


async def test_reload_plugins_reports_what_changed(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]))
    local = app.local
    async with app.run_test(size=(110, 40)) as pilot:
        await type_line(pilot, "/reload-plugins")
        await settle(pilot, lambda: "restart them" in shown(app))
        local.reload_plugins = lambda: (local.replace_agents(
            [Agent(name="main", model=MODEL, extensions=[FileTools(), Checkpoints()])])
            or ["removed where", "a notice"])
        await type_line(pilot, "/reload-plugins")
        await settle(pilot, lambda: "plugins reloaded" in shown(app))
        assert "removed where" in shown(app) and "a notice" in shown(app)
        # The slash menu offers what the plugins offer now.
        await settle(pilot, lambda: all(c.name != "where" for c in app.plugin_commands))


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


async def test_resume_lists_the_roots_past_many_sub_agents(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([reply("one"), reply("two")]))
    async with app.run_test(size=(100, 30)) as pilot:
        await type_line(pilot, "first task")
        await settle(pilot, lambda: not app.busy and "one" in shown(app))
        first = app.task_id
        await type_line(pilot, "/clear")
        await type_line(pilot, "second task")
        await settle(pilot, lambda: not app.busy and "two" in shown(app))
        second = app.task_id
        for n in range(30):  # its sub-agents, all updated after both roots
            await app.local.store.create(NewTask(task_id=f"{second}.{n}", created=TaskCreated(
                agent="main", parent_id=second, root_id=second)))
        await type_line(pilot, "/resume")
        await settle(pilot, lambda: len(app.query(Picker)) == 1)
        assert app.listed == [second, first]
