"""The ACP adapter against a fake editor: a prompt streams text, shows tool
calls start and finish, asks permission, and ends the turn."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("acp")

from acp import schema

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.extension import Ask, Extension, TaskView
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import ToolUse
from noeta.host.acp import NoetaAcpAgent
from noeta.host.local import LocalApp
from noeta.testing import ScriptedProvider, reply


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
        self.bodies: list[list[str]] = []
        self.options: list[list[str]] = []
        self.choice = choice

    async def session_update(self, session_id: str, update: Any, **kw: Any) -> None:
        self.updates.append(update)

    async def request_permission(self, session_id: str, tool_call: Any, options: Any,
                                 **kw: Any) -> Any:
        self.asked.append(tool_call.title)
        self.bodies.append([c.content.text for c in tool_call.content or ()])
        self.options.append([o.name for o in options])
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
    # The card names the call; the gate's prompt is its body.
    assert editor.asked == ["Read a.py"] and editor.bodies == [["run Read?"]]
    k = kinds(editor.updates)
    assert k.index("tool_call") < k.index("tool_call_update")
    assert "agent_message_chunk" in k
    statuses = [u.status for u in editor.updates if u.session_update == "tool_call_update"]
    assert statuses[-1] == "completed"
    assert editor.options == [["Yes", "Always allow", "No"]]
    await app.stop()


class AskNamingRule(Extension):
    name = "ask"

    def __init__(self, label: str | None) -> None:
        self.label = label

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        always = {"kind": "shell", "spec": {"rule": self.label}} if self.label else None
        return Ask(prompt="run it?", detail={"always": always,
                                             "always_label": self.label and f"Bash({self.label})"})


@pytest.mark.parametrize(("label", "shown"), [
    ("git push *", ["Yes", "Always allow Bash(git push *)", "No"]),
    (None, ["Yes", "No"]),
])
async def test_permission_options_name_the_remembered_rule(
        tmp_path: Path, label: str | None, shown: list[str]) -> None:
    provider = ScriptedProvider([reply("", ("Read", {"file_path": "a.py"})), reply("done")])
    agent = Agent(name="main", model="m", tools=[Echo()], extensions=[AskNamingRule(label)])
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                   env_for=lambda _s: None)
    acp_agent = NoetaAcpAgent(app)
    editor = FakeEditor()
    acp_agent.on_connect(editor)
    await acp_agent.initialize(protocol_version=1)
    session = await acp_agent.new_session(cwd=str(tmp_path))
    await acp_agent.prompt(session.session_id, [schema.TextContentBlock(type="text", text="go")])
    assert editor.options == [shown]
    await app.stop()


async def test_permission_modes_are_session_modes(tmp_path: Path) -> None:
    from noeta.plugins.permissions import Permissions

    provider = ScriptedProvider([reply("", ("Write", {"file_path": "x"})), reply("done")])
    write = Echo()
    write.name = "Write"
    agent = Agent(name="main", model="m", tools=[write],
                  extensions=[Permissions(mode="default")])
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                   env_for=lambda _s: None)
    acp_agent = NoetaAcpAgent(app)
    editor = FakeEditor(choice="mode:acceptEdits")
    acp_agent.on_connect(editor)
    await acp_agent.initialize(protocol_version=1)
    session = await acp_agent.new_session(cwd=str(tmp_path))
    assert session.modes is not None and session.modes.current_mode_id == "default"
    assert "auto" in [m.id for m in session.modes.available_modes]
    await acp_agent.prompt(session.session_id, [schema.TextContentBlock(type="text", text="go")])
    assert editor.options[0][1].startswith("Yes, and switch to accept edits")
    modes = [u.current_mode_id for u in editor.updates
             if u.session_update == "current_mode_update"]
    assert modes == ["acceptEdits"]
    # The mode config option follows the switch too.
    options = [{o.id: o.current_value for o in u.config_options} for u in editor.updates
               if u.session_update == "config_option_update"]
    assert options[-1]["mode"] == "acceptEdits"
    await acp_agent.set_session_mode(session_id=session.session_id, mode_id="plan")
    await app.stop()
