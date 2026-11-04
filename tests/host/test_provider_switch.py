"""Switching a task's model across providers: an ACP pick and a
stream-json ``set_model`` send the next request to the other provider,
the model lists name every provider's models, and a task resumed in a new
app (another machine, the same configuration) routes the same way."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("acp")

from acp import schema

from noeta.backends.providers.listing import ListedModel
from noeta.backends.providers.routing import Endpoint, ProviderRouter
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import models
from noeta.core.agent import Agent
from noeta.host.acp import NoetaAcpAgent
from noeta.host.local import LocalApp
from noeta.host.stream_json import HeadlessOptions, StreamJson, drive
from noeta.testing import ScriptedProvider, reply
from tests.host.test_stream_json import Client

GATEWAY_MODEL = "deepseek-chat"


def routed_app(tmp_path: Path, gateway: ScriptedProvider, claude: ScriptedProvider
               ) -> LocalApp:
    """The default provider is a gateway; Claude models are on another."""
    router = ProviderRouter([
        Endpoint(name="default", family="openai_compat", make=lambda: gateway),
        Endpoint(name="anthropic", family="anthropic", make=lambda: claude,
                 listing=lambda: [ListedModel("claude-sonnet-5-5"),
                                  ListedModel("claude-opus-5-5")]),
    ], explicit={GATEWAY_MODEL: "default"})
    agent = Agent(name="main", model=GATEWAY_MODEL)
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], router.provider_for,
                   env_for=lambda _s: None)

    def prepare(model: str) -> str | None:
        router.prepare(model)
        return models.unknown_model_notice(model)

    app.list_models = router.available
    app.prepare_model = prepare
    return app


def text(content: str) -> list[Any]:
    return [schema.TextContentBlock(type="text", text=content)]


class Editor:
    async def session_update(self, session_id: str, update: Any, **kw: Any) -> None:
        return None


async def test_an_acp_pick_moves_the_task_to_the_other_provider(tmp_path: Path) -> None:
    gateway, claude = ScriptedProvider([reply("from the gateway")]), \
        ScriptedProvider([reply("from claude")])
    app = routed_app(tmp_path, gateway, claude)
    agent = NoetaAcpAgent(app)
    agent.on_connect(Editor())
    await agent.initialize(protocol_version=1)
    session = await agent.new_session(cwd=str(tmp_path))
    sid = session.session_id
    [model_option] = [o for o in session.config_options if o.id == "model"]
    names = {o.value: o.name for o in model_option.options}
    assert names[GATEWAY_MODEL] == f"{GATEWAY_MODEL} (default)"
    assert names["claude-sonnet-5-5"] == "claude-sonnet-5-5 (sonnet, anthropic)"
    await agent.prompt(sid, text("hi"))
    assert [r.model for r in gateway.requests] == [GATEWAY_MODEL]

    await agent.set_config_option(config_id="model", session_id=sid, value="sonnet")
    await agent.prompt(sid, text("again"))
    assert [r.model for r in claude.requests] == ["claude-sonnet-5-5"]
    # The model is the bare id; the history the gateway wrote goes along.
    assert [m.text for m in claude.requests[0].messages if m.role == "assistant"][-1] \
        == "from the gateway"
    await app.stop()

    # Another app on the same store, the same configuration, nothing remembered.
    gateway2, claude2 = ScriptedProvider(), ScriptedProvider([reply("resumed on claude")])
    again = routed_app(tmp_path, gateway2, claude2)
    resumed = NoetaAcpAgent(again)
    resumed.on_connect(Editor())
    await resumed.initialize(protocol_version=1)
    await resumed.load_session(cwd=str(tmp_path), session_id=sid)
    await resumed.prompt(sid, text("and again"))
    assert [r.model for r in claude2.requests] == ["claude-sonnet-5-5"] and not gateway2.requests
    await again.stop()


async def test_stream_json_set_model_switches_provider_and_lists_models(
        tmp_path: Path) -> None:
    gateway, claude = ScriptedProvider([reply("gw")]), ScriptedProvider([reply("claude")])
    app = routed_app(tmp_path, gateway, claude)
    c = Client()
    async with app:
        conv = StreamJson(app, HeadlessOptions(model=GATEWAY_MODEL, cwd=str(tmp_path)),
                          c.emit)
        running = asyncio.create_task(drive(conv, lines=c.lines()))
        c.control("r0", subtype="initialize")
        c.say("hi")
        await c.until(lambda m: m["type"] == "result")
        c.control("r1", subtype="supported_models")
        c.control("r2", subtype="set_model", model="claude-opus-5-5")
        await c.until(lambda m: m.get("response", {}).get("request_id") == "r2")
        c.say("next")
        await c.until(lambda m: m["type"] == "result" and m["result"] == "claude")
        assert [r.model for r in gateway.requests] == [GATEWAY_MODEL]
        assert [r.model for r in claude.requests] == ["claude-opus-5-5"]
        init = next(m["response"]["response"] for m in c.out
                    if m["type"] == "control_response"
                    and m["response"]["request_id"] == "r0")
        described = {m["value"]: m["description"] for m in init["models"]}
        assert described["claude-sonnet-5-5"] == "on provider anthropic"
        await c.inbox.put(None)
        await asyncio.wait_for(running, 10)
