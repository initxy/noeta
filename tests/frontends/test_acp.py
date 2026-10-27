"""The ACP adapter against a fake editor: a prompt streams text, shows tool
calls start and finish, asks permission, and ends the turn."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("acp")

from acp import schema

from noeta.acp import NoetaAcpAgent
from noeta.agent import Agent
from noeta.ext.base import Ask, Extension, TaskView
from noeta.local import LocalApp
from noeta.store.sqlite import SqliteStore
from noeta.testing import ScriptedProvider, reply
from noeta.tools.base import ToolContext, ToolOutput, text_output
from noeta.types import ToolUse


class Echo:
    name = "Read"
    description = "Read."
    input_schema = {"type": "object"}
    read_only = True
    clearable = True

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("file body")


class AskAll(Extension):
    name = "ask"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Ask(prompt=f"run {call.name}?")


class FakeEditor:
    def __init__(self, choice: str = "allow") -> None:
        self.updates: list[Any] = []
        self.asked: list[str] = []
        self.choice = choice

    async def session_update(self, session_id: str, update: Any, **kw: Any) -> None:
        self.updates.append(update)

    async def request_permission(self, session_id: str, tool_call: Any, options: Any,
                                 **kw: Any) -> Any:
        self.asked.append(tool_call.title)
        return schema.RequestPermissionResponse(
            outcome=schema.AllowedOutcome(outcome="selected", option_id=self.choice))


def kinds(updates: list[Any]) -> list[str]:
    return [u.session_update for u in updates]


async def test_prompt_streams_tools_and_asks(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("looking", ("Read", {"file_path": "a.py"})),
                                 reply("done")])
    agent = Agent(name="main", model="m", tools=[Echo()], extensions=[AskAll()])
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                   env_for=lambda _s: None)
    acp_agent = NoetaAcpAgent(app)
    editor = FakeEditor()
    acp_agent.on_connect(editor)
    await acp_agent.initialize(protocol_version=1)
    session = await acp_agent.new_session(cwd=str(tmp_path))
    resp = await acp_agent.prompt(session.session_id, [schema.TextContentBlock(type="text",
                                                                               text="read a.py")])
    assert resp.stop_reason == "end_turn"
    assert editor.asked == ["run Read?"]
    k = kinds(editor.updates)
    assert k.index("tool_call") < k.index("tool_call_update")
    assert "agent_message_chunk" in k
    statuses = [u.status for u in editor.updates if u.session_update == "tool_call_update"]
    assert statuses[-1] == "completed"
    await app.stop()
