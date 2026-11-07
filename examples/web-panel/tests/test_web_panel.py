"""The web-panel example as a plugin directory: it joins main by itself,
shows a status item, answers its web part's call, and its manifest names a
built web part the server lists and serves."""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta import Agent, CommandContext, Runtime
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import registry
from noeta.core.client import Client
from noeta.host.plugins import PluginAdmin, plugin_infos, task_ui, web_dir
from noeta.testing import ScriptedProvider, reply

HERE = Path(__file__).parents[1]


def _plugin() -> type:
    registry.load_plugin_files([], given=[HERE])
    cls: type = registry.load(registry.PLUGINS, "web-panel")
    return cls


async def test_it_shows_the_conversations_length_and_answers_its_web_part(tmp_path: Path) -> None:
    cls = _plugin()
    assert cls.add_to == ("main",)
    plugin = cls()
    agent = Agent(name="main", model="m", extensions=(plugin,))
    store = SqliteStore(str(tmp_path / "t.db"))
    rt = Runtime(store=store, agents={"main": agent},
                 provider_for=lambda _m: ScriptedProvider([reply("hi")]))
    tid = await Client(store).create("main", "hello")
    await rt.run_until_idle()
    [item] = await task_ui(rt, tid)
    assert item == {"plugin": "web-panel", "type": "status", "key": "messages",
                    "text": "2 messages", "tone": "normal"}

    (tmp_path / "a.txt").write_text("a")
    (tmp_path / ".hidden").write_text("h")
    got = await plugin.ui_call(CommandContext(cwd=str(tmp_path), task_id=tid), "workspace", None)
    assert got["cwd"] == str(tmp_path) and "a.txt" in got["entries"]
    assert ".hidden" not in got["entries"] and got["count"] == len(got["entries"])
    with pytest.raises(LookupError):
        await plugin.ui_call(CommandContext(cwd=str(tmp_path)), "nope", None)
    await store.close()


def test_its_built_web_part_is_listed_and_served() -> None:
    cls = _plugin()
    agent = Agent(name="main", model="m", extensions=(cls(),))
    info = next(p for p in plugin_infos(agent, PluginAdmin()) if p["name"] == "web-panel")
    assert info["parts"] == ["backend", "ui", "web"] and info["broken"] is None
    assert info["web"] is not None
    assert info["web"]["entry"].startswith("/plugins/web-panel/web/index.js?v=")
    served = web_dir("web-panel")
    assert served == HERE / "web" / "dist"
    assert "export {\n  activate\n};" in (served / "index.js").read_text("utf-8")
