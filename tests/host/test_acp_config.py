"""Mode and model as ACP session config options: a session offers both
(and the modes), a pick switches the task, and every switch reaches the
editor, a loaded session included."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("acp")

import acp
from acp import schema

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.host.acp import NoetaAcpAgent
from noeta.host.local import LocalApp
from noeta.plugins.permissions import Permissions
from noeta.testing import ScriptedProvider, reply


class Editor:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, session_id: str, update: Any, **kw: Any) -> None:
        self.updates.append(update)

    async def request_permission(self, **kw: Any) -> Any:
        raise AssertionError("nothing asks here")

    def options(self) -> list[dict[str, str]]:
        return [current(u.config_options) for u in self.updates
                if u.session_update == "config_option_update"]


def current(options: Any) -> dict[str, str]:
    return {o.id: o.current_value for o in options or ()}


def text(content: str) -> list[Any]:
    return [schema.TextContentBlock(type="text", text=content)]


async def start(tmp_path: Path, provider: ScriptedProvider,
                **kw: Any) -> tuple[LocalApp, NoetaAcpAgent, Editor, Any]:
    agent = Agent(name="main", model="m", extensions=[Permissions(mode="default")])
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                   env_for=lambda _s: None)
    acp_agent = NoetaAcpAgent(app, **kw)
    editor = Editor()
    acp_agent.on_connect(editor)
    await acp_agent.initialize(protocol_version=1)
    return app, acp_agent, editor, await acp_agent.new_session(cwd=str(tmp_path))


async def test_a_session_offers_model_and_mode(tmp_path: Path) -> None:
    app, _acp, _editor, session = await start(tmp_path, ScriptedProvider(),
                                              model="claude-sonnet-5-5")
    model, mode = session.config_options
    assert (model.id, model.category, model.type) == ("model", "model", "select")
    assert model.current_value == "claude-sonnet-5-5"
    values = [o.value for o in model.options]
    assert values[0] == "default" and "claude-opus-5-5" in values
    assert (mode.id, mode.category, mode.current_value) == ("mode", "mode", "default")
    assert [o.value for o in mode.options] == [m.id for m in session.modes.available_modes]
    await app.stop()


async def test_picking_a_model_and_a_mode_switches_the_task(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("ok"), reply("again")])
    app, acp_agent, editor, session = await start(tmp_path, provider)
    sid = session.session_id
    assert current(session.config_options)["model"] == "default"
    picked = await acp_agent.set_config_option(config_id="model", session_id=sid,
                                               value="opus")
    assert current(picked.config_options)["model"] == "claude-opus-5-5"
    picked = await acp_agent.set_config_option(config_id="mode", session_id=sid,
                                               value="acceptEdits")
    assert current(picked.config_options) == {"model": "claude-opus-5-5",
                                              "mode": "acceptEdits"}
    await acp_agent.prompt(sid, text("hi"))
    assert provider.requests[-1].model == "claude-opus-5-5"

    with pytest.raises(acp.RequestError):
        await acp_agent.set_config_option(config_id="speed", session_id=sid, value="fast")
    with pytest.raises(acp.RequestError):
        await acp_agent.set_config_option(config_id="mode", session_id=sid, value="loud")

    editor.updates.clear()
    await acp_agent.prompt(sid, text("/model sonnet"))
    assert editor.options() == [{"model": "claude-sonnet-5-5", "mode": "acceptEdits"}]
    await acp_agent.prompt(sid, text("again"))
    assert provider.requests[-1].model == "claude-sonnet-5-5"

    again = NoetaAcpAgent(app)
    again.on_connect(Editor())
    loaded = await again.load_session(cwd=str(tmp_path), session_id=sid)
    assert loaded is not None and loaded.modes is not None
    assert loaded.modes.current_mode_id == "acceptEdits"
    assert current(loaded.config_options) == {"model": "claude-sonnet-5-5",
                                              "mode": "acceptEdits"}
    await app.stop()


async def test_loading_a_session_a_headless_run_had_makes_it_interactive_again(
        tmp_path: Path) -> None:
    """A task ``noeta -p --resume`` marked not interactive (no
    ``AskUserQuestion``) offers it again once an editor loads it."""
    from noeta.core.loop.state import CONFIG_SIGNAL
    from noeta.plugins.ask_user import AskUser

    provider = ScriptedProvider([reply("ok")])
    agent = Agent(name="main", model="m", extensions=[AskUser()])
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                   env_for=lambda _s: None)
    acp_agent = NoetaAcpAgent(app)
    acp_agent.on_connect(Editor())
    await acp_agent.initialize(protocol_version=1)
    sid = (await acp_agent.new_session(cwd=str(tmp_path))).session_id
    await app.client.signal(sid, CONFIG_SIGNAL, {"interactive": False})
    again = NoetaAcpAgent(app)
    again.on_connect(Editor())
    await again.load_session(cwd=str(tmp_path), session_id=sid)
    await again.prompt(sid, text("hi"))
    assert "AskUserQuestion" in [t.name for t in provider.requests[-1].tools]
    await app.stop()
