"""A third-party plugin asks the person (``noeta.core.questions``) and tells
them something (``notify``); the TUI and ACP handle both with no code for
that plugin. MCP servers given as data, in Claude Code's shape."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.events import ToolFinished
from noeta.core.extension import Extension, TaskView
from noeta.core.questions import AnswerValidationError, answer_question, ask, read_answer
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.host.acp import EditorServers
from noeta.host.local import LocalApp
from noeta.plugins.mcp import Mcp, McpPool
from noeta.testing import ScriptedProvider, reply
from tests.host.test_acp_review import acp_session, text_block

FAKE_MCP = str(Path(__file__).parents[1] / "plugins" / "mcp" / "fake_server.py")


WHERE = ask(
    {"question": "Which environment?",
     "options": [{"label": "staging"}, {"label": "prod", "description": "careful"}]},
    {"question": "Any note for the changelog?"})


class Deploy:
    name = "Deploy"
    description = "Deploy the service."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        ctx.notify("deploy: asking where to")
        return ToolOutput(park=WHERE)

    async def resume(self, input: dict[str, Any], ctx: ToolContext, outcome: Any) -> ToolOutput:
        answers = read_answer(outcome, WHERE)
        if answers is None:
            return text_output("not deployed", is_error=True)
        where = answers["0"]["selected"] or [answers["0"]["other"]]
        return text_output(f"deployed to {where[0]}: {answers['1']['other']}")


class Deployer(Extension):
    name = "deployer"

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return [Deploy()]


async def _no_sleep(_: float) -> None:
    return None


def _local(tmp_path: Path, provider: Any) -> LocalApp:
    agent = Agent(name="main", model="m", extensions=(Deployer(),))
    return LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                    env_for=lambda _s: None, sleep=_no_sleep)


async def _result(app: LocalApp, task_id: str) -> str:
    [done] = [r.event for r in await app.client.events(task_id)
              if isinstance(r.event, ToolFinished)]
    return done.result.text


def test_ask_checks_the_questions() -> None:
    park = ask({"question": "Name?"})
    assert park.on == "question"
    assert park.detail == {"questions": [{"id": "0", "question": "Name?", "options": [],
                                          "multiSelect": False}]}
    for bad in ({"question": ""}, {"question": "x", "options": [{"label": ""}]},
                {"question": "x", "options": [{"label": "a"}, {"label": "a"}]}):
        with pytest.raises(ValueError):
            ask(bad)
    with pytest.raises(ValueError):
        ask()


def test_read_answer() -> None:
    detail = ask({"question": "Name?"})
    assert read_answer(None, detail) is None
    assert read_answer("Ada", detail) == {"0": {"selected": [], "other": "Ada"}}
    with pytest.raises(AnswerValidationError, match="missing"):
        read_answer({"answers": {}}, detail)


async def test_the_tui_asks_for_a_plugin(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    from noeta.cli.tui.app import NoetaApp
    from tests.cli.test_frontends_rereview import settle

    provider = ScriptedProvider([reply("", ("Deploy", {})), reply("done")])
    app = NoetaApp(_local(tmp_path, provider), agent="main", model="m", cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"ship it", "enter")
        await settle(pilot, lambda: bool(app.waiting_question))
        notices = " ".join(str(w.render()) for w in app.query(".notice"))
        assert "deploy: asking where to" in notices
        await pilot.press(*"Prod", "enter")
        await pilot.press(*"first release", "enter")
        await settle(pilot, lambda: provider.steps == [])
        await settle(pilot, lambda: not app.busy)
        assert await _result(app.local, app.task_id) == "deployed to prod: first release"
        assert all("deploy: asking" not in m.text for m in provider.requests[-1].messages)


async def test_acp_asks_for_a_plugin(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Deploy", {})), reply("done")])
    app = _local(tmp_path, provider)
    agent, editor, sid = await acp_session(app, tmp_path)
    await asyncio.wait_for(agent.prompt(sid, text_block("ship it")), 10)
    said = "".join(editor.texts())
    assert "[deploy: asking where to]" in said and "Which environment?" in said
    await asyncio.wait_for(agent.prompt(sid, text_block("staging")), 10)
    assert "Any note for the changelog?" in editor.texts()[-1]
    await asyncio.wait_for(agent.prompt(sid, text_block("none")), 10)
    assert editor.texts()[-1] == "done"
    assert await _result(app, sid) == "deployed to staging: none"
    await app.stop()


async def test_answer_question_checks_a_plugins_question(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Deploy", {})), reply("done")])
    app = _local(tmp_path, provider)
    async with app:
        tid = await app.client.create("main", "ship it")
        await app.client.wait(tid, timeout=5)
        [call] = [r.event.result for r in await app.client.events(tid)
                  if isinstance(r.event, ToolFinished)] or [None]
        assert call is None  # parked, no result yet
        uses = [r.event for r in await app.client.events(tid)
                if type(r.event).__name__ == "ModelReplied"]
        call_id = uses[0].response.message.tool_uses[0].id
        with pytest.raises(AnswerValidationError, match="not an option"):
            await answer_question(app.client, tid, call_id,
                                  {"answers": {"0": {"selected": ["dev"]}, "1": {"other": "x"}}})
        await answer_question(app.client, tid, call_id,
                              {"answers": {"0": {"selected": ["prod"]}, "1": {"other": "x"}}})
        await app.client.wait(tid, timeout=5)
        assert await _result(app, tid) == "deployed to prod: x"


async def test_mcp_servers_in_claude_codes_shape() -> None:
    pool = McpPool()
    editor = EditorServers()

    class Server:
        name = "fake server"
        command = sys.executable
        args = [FAKE_MCP]
        env: list[Any] = []

    editor.add("t", [Server()])
    assert editor.by_task["t"] == {"fake server": {"command": sys.executable,
                                                   "args": [FAKE_MCP], "env": {}}}
    from noeta.core.extension import CommandContext

    try:
        mcp = Mcp({"fake": {"command": sys.executable, "args": [FAKE_MCP]}}, pool=pool)
        [command] = await mcp.commands(CommandContext(cwd="/"))
        assert command.name == "mcp__fake__review"
    finally:
        await pool.close()
