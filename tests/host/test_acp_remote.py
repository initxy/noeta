"""The ACP adapter driving a ``noeta serve`` server through ``RemoteApp``:
the same session, prompt, approval, plugin commands and ``/context`` as in
process, with the tasks running on the server's side."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("acp")
pytest.importorskip("starlette")
uvicorn = pytest.importorskip("uvicorn")

from acp import schema  # noqa: E402

from noeta.backends.stores.sqlite import SqliteStore  # noqa: E402
from noeta.core.agent import Agent  # noqa: E402
from noeta.core.extension import Ask, Command, CommandContext, Extension, TaskView  # noqa: E402
from noeta.core.tool import ToolContext, ToolOutput, text_output  # noqa: E402
from noeta.core.types import ToolUse  # noqa: E402
from noeta.host.acp import NoetaAcpAgent  # noqa: E402
from noeta.host.http import create_app  # noqa: E402
from noeta.host.local import LocalApp  # noqa: E402
from noeta.host.remote import RemoteApp  # noqa: E402
from noeta.testing import ScriptedProvider, reply  # noqa: E402

MODEL = "claude-opus-5-5"


class Echo:
    name = "Read"
    description = "Read."
    input_schema = {"type": "object"}
    read_only = True
    clearable = True

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("file body")


class Gate(Extension):
    name = "gate"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Ask(prompt=f"run {call.name}?")

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return [Command(name="where", description="Where the server is", local=True)]

    async def run_command(self, ctx: CommandContext, name: str, args: str) -> str:
        return f"on the server, in {ctx.cwd}"


class FakeEditor:
    def __init__(self) -> None:
        self.updates: list[Any] = []
        self.asked: list[str] = []

    async def session_update(self, session_id: str, update: Any, **kw: Any) -> None:
        self.updates.append(update)

    async def request_permission(self, session_id: str, tool_call: Any, options: Any,
                                 **kw: Any) -> Any:
        self.asked.append(tool_call.title)
        return schema.RequestPermissionResponse(
            outcome=schema.AllowedOutcome(outcome="selected", option_id="allow"))


@pytest.fixture
async def server(tmp_path: Path) -> AsyncIterator[tuple[str, ScriptedProvider, LocalApp]]:
    provider = ScriptedProvider()
    agent = Agent(name="main", model=MODEL, tools=[Echo()], extensions=[Gate()])
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                     env_for=lambda _s: None)
    await local.start()
    api = create_app(local.client, token="secret", deltas=local.deltas, app=local,
                     cwd=str(tmp_path))
    served = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=0, log_level="error"))
    serving = asyncio.create_task(served.serve())
    while not served.started:
        await asyncio.sleep(0.01)
    port = served.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", provider, local
    served.should_exit = True
    await serving
    await local.stop()


def _text(editor: FakeEditor) -> str:
    return "".join(u.content.text for u in editor.updates
                   if u.session_update == "agent_message_chunk")


async def test_acp_drives_a_served_app(
        server: tuple[str, ScriptedProvider, LocalApp], tmp_path: Path) -> None:
    url, provider, local = server
    provider.add(reply("looking", ("Read", {"file_path": "a.py"})), reply("done reading"))
    acp_agent = NoetaAcpAgent(RemoteApp(url, token="secret"))
    editor = FakeEditor()
    acp_agent.on_connect(editor)
    await acp_agent.initialize(protocol_version=1)
    session = await acp_agent.new_session(cwd=str(tmp_path))
    # The task lives in the server's store; the editor's process has none.
    assert (await local.client.get(session.session_id)).agent == "main"
    resp = await acp_agent.prompt(session.session_id, [
        schema.TextContentBlock(type="text", text="read a.py")])
    assert resp.stop_reason == "end_turn"
    assert editor.asked == ["Read a.py"]
    assert "looking" in _text(editor) and "done reading" in _text(editor)
    statuses = [u.status for u in editor.updates if u.session_update == "tool_call_update"]
    assert statuses[-1] == "completed"

    # The server's plugin commands are the editor's slash commands, run there.
    for _ in range(100):
        announced = [u for u in editor.updates
                     if u.session_update == "available_commands_update"]
        if announced:
            break
        await asyncio.sleep(0.02)
    names = {c.name for c in announced[0].available_commands}
    assert {"where", "context", "cost"} <= names
    editor.updates.clear()
    await acp_agent.prompt(session.session_id, [schema.TextContentBlock(type="text",
                                                                        text="/where")])
    assert _text(editor) == f"on the server, in {tmp_path}"
    editor.updates.clear()
    await acp_agent.prompt(session.session_id, [schema.TextContentBlock(type="text",
                                                                        text="/context")])
    assert "Messages" in _text(editor)
    await acp_agent.app.stop()


async def _created_config(local: LocalApp, task_id: str) -> Any:
    from noeta.core.events import TaskCreated

    first = (await local.client.events(task_id))[0].event
    assert isinstance(first, TaskCreated)
    return first.config


async def test_acp_on_a_server_sends_neither_the_editors_directory_nor_a_model(
        server: tuple[str, ScriptedProvider, LocalApp], tmp_path: Path) -> None:
    """The editor's directory is a path on its own machine (outside the
    server's: 403) and its model may be one the server lacks: the task runs
    in the server's directory, on the agent's model, unless asked."""
    url, _provider, local = server
    acp_agent = NoetaAcpAgent(RemoteApp(url, token="secret"), editor_cwd=False)
    acp_agent.on_connect(FakeEditor())
    await acp_agent.initialize(protocol_version=1)
    session = await acp_agent.new_session(cwd="/somewhere/only/the/editor/has")
    assert await _created_config(local, session.session_id) == {
        "cwd": os.path.realpath(tmp_path)}

    (tmp_path / "sub").mkdir()
    named = NoetaAcpAgent(RemoteApp(url, token="secret"), editor_cwd=False, cwd="sub")
    named.on_connect(FakeEditor())
    session = await named.new_session(cwd="/somewhere/only/the/editor/has")
    assert await _created_config(local, session.session_id) == {
        "cwd": os.path.realpath(tmp_path / "sub")}
    await acp_agent.app.stop()
    await named.app.stop()
