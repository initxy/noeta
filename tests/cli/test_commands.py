"""Plugin slash commands: listed and expanded by the shared helper, offered
by the TUI and over ACP; the skills and MCP plugins' own commands."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from acp import schema

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.commands import expand_command, list_commands, parse_command
from noeta.core.extension import Command, CommandContext, Extension
from noeta.host.acp import NoetaAcpAgent
from noeta.host.local import LocalApp
from noeta.plugins.mcp import Mcp, McpPool, McpServer
from noeta.plugins.skills import Skills
from noeta.testing import ScriptedProvider, reply

FAKE_MCP = str(Path(__file__).parents[1] / "plugins" / "mcp" / "fake_server.py")


class Greeter(Extension):
    name = "greeter"

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return [Command(name="greet", description="Say hello", template="Say hello to {args}."),
                Command(name="shout", description="Shout", argument_hint="<word>")]

    async def run_command(self, ctx: CommandContext, name: str, args: str) -> str:
        if args == "boom":
            raise RuntimeError("no voice")
        return f"{args.upper()}! (in {ctx.cwd})"


class Broken(Extension):
    name = "broken"

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        raise RuntimeError("down")


class Shadow(Extension):
    name = "shadow"

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return [Command(name="greet", description="other", template="never")]


AGENT = Agent(name="main", model="m", extensions=[Broken(), Greeter(), Shadow()])
CTX = CommandContext(cwd="/w")


def test_parse() -> None:
    assert parse_command("/greet  bob ") == ("greet", "bob")
    assert parse_command("/x") == ("x", "")
    assert parse_command("//not") is None and parse_command("hi") is None
    assert parse_command("/ x") is None


async def test_list_and_expand() -> None:
    assert [c.name for c in await list_commands(AGENT, CTX)] == ["greet", "shout"]
    assert await expand_command(AGENT, CTX, "/greet bob") == "Say hello to bob."
    assert await expand_command(AGENT, CTX, "/shout hey") == "HEY! (in /w)"
    assert await expand_command(AGENT, CTX, "/nope") is None
    assert await expand_command(AGENT, CTX, "plain") is None
    with pytest.raises(RuntimeError, match="no voice"):
        await expand_command(AGENT, CTX, "/shout boom")
    with pytest.raises(NotImplementedError):
        await Extension().run_command(CTX, "x", "")


def _app(tmp_path: Path, provider: ScriptedProvider) -> LocalApp:
    return LocalApp(SqliteStore(str(tmp_path / "t.db")), [AGENT], provider,
                    env_for=lambda _s: None)


async def test_tui_help_and_plugin_commands(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Markdown

    from noeta.cli.tui.app import NoetaApp

    provider = ScriptedProvider([reply("hi bob"), reply("ok")])
    app = NoetaApp(_app(tmp_path, provider), agent="main", model="m", cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(200):
            await pilot.pause(0.05)
            if app.task_id:
                break
        await app.submit("/help")
        await pilot.pause(0.1)
        helps = [m for m in app.query(Markdown) if "From plugins" in m.source]
        assert helps and "`/shout` <word> — Shout" in helps[-1].source
        await app.submit("/greet bob")
        for _ in range(200):
            await pilot.pause(0.05)
            if provider.requests:
                break
        assert provider.requests[0].messages[-1].text.endswith("Say hello to bob.")
        await app.submit("/nope")
        await app.submit("/shout boom")
        await pilot.pause(0.2)
        notices = " ".join(str(w.render()) for w in app.query(".notice"))
        assert "unknown command /nope" in notices and "/shout failed: no voice" in notices


class FakeEditor:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, session_id: str, update: Any, **kw: Any) -> None:
        self.updates.append(update)


async def test_acp_announces_and_expands(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("hello")])
    app = _app(tmp_path, provider)
    agent = NoetaAcpAgent(app)
    editor = FakeEditor()
    agent.on_connect(editor)
    await agent.initialize(protocol_version=1)
    session = await agent.new_session(cwd=str(tmp_path))
    await agent._announce_commands(session.session_id)
    [update, *_] = [u for u in editor.updates
                    if u.session_update == "available_commands_update"]
    assert [c.name for c in update.available_commands] == [
        "greet", "shout", "model", "context", "cost", "rewind"]
    assert update.available_commands[1].input.root.hint == "<word>"

    def block(text: str) -> list[Any]:
        return [schema.TextContentBlock(type="text", text=text)]

    resp = await agent.prompt(session.session_id, block("/shout hi"))
    assert resp.stop_reason == "end_turn"
    assert provider.requests[0].messages[-1].text.endswith(f"HI! (in {tmp_path})")
    resp = await agent.prompt(session.session_id, block("/shout boom"))
    assert resp.stop_reason == "end_turn" and len(provider.requests) == 1
    assert any(getattr(getattr(u, "content", None), "text", "") == "/shout failed: no voice"
               for u in editor.updates)

    def said() -> str:
        return getattr(getattr(editor.updates[-1], "content", None), "text", "")

    for typed, expected in [("/cost", "Total cost"), ("/context", "Free space"),
                            ("/model opus", "model set to claude-opus-5-5"),
                            ("/rewind", "1. HI!"), ("/rewind 1 code", "nothing to restore")]:
        resp = await agent.prompt(session.session_id, block(typed))
        assert resp.stop_reason == "end_turn" and expected in said(), (typed, said())
    assert len(provider.requests) == 1
    await app.stop()


async def test_skills_commands_follow_the_cwd(tmp_path: Path) -> None:
    skill = tmp_path / ".agents" / "skills" / "deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: deploy\ndescription: Ship it.\n---\nSteps.\n")
    [c] = await Skills(global_dir=None, global_agents_dir=None).commands(
        CommandContext(cwd=str(tmp_path)))
    assert (c.name, c.description) == ("deploy", "Ship it.")
    assert c.template == ("<command-message>deploy</command-message>\n<command-name>/deploy"
                          "</command-name>\n<command-args>{args}</command-args>")


async def test_mcp_prompts_are_commands(tmp_path: Path) -> None:
    pool = McpPool()
    mcp = Mcp([McpServer(alias="fake", command=[sys.executable, FAKE_MCP])], pool=pool)
    agent = Agent(name="a", model="m", extensions=[mcp])
    try:
        [c] = await list_commands(agent, CTX)
        assert (c.name, c.argument_hint) == ("mcp__fake__review", "<path>")
        assert await expand_command(agent, CTX, "/mcp__fake__review a.py") == "Review a.py"
        fresh = Mcp([McpServer(alias="fake", command=[sys.executable, FAKE_MCP])], pool=pool)
        assert await fresh.run_command(CTX, "mcp__fake__review", "b.py") == "Review b.py"
        dead = Mcp([McpServer(alias="dead", command=["/nonexistent/server"])], pool=pool)
        assert await dead.commands(CTX) == []
    finally:
        await pool.close()


class Status(Extension):
    name = "status"

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return [Command(name="where", description="Where am I", local=True)]

    async def run_command(self, ctx: CommandContext, name: str, args: str) -> str:
        return f"in {ctx.cwd}"


async def test_a_local_command_runs_but_sends_nothing() -> None:
    from noeta.core.commands import Typed, run_typed

    agent = Agent(name="main", model="m", extensions=[Status(), Greeter()])
    assert await run_typed(agent.all_extensions(), CTX, "/where") == Typed("in /w", local=True)
    assert await run_typed(agent.all_extensions(), CTX, "/greet x") == Typed("Say hello to x.")
    assert await expand_command(agent, CTX, "/where") == ""
