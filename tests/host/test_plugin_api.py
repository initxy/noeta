"""Plugins as front ends see them: the listing (built-in, switchable, a
manifest's web part), a task's UI items, calls from a plugin's own front-end
code, its web files served without a token, the machine's default switches
for new tasks, and install and remove behind their guard."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("starlette")

import httpx

from noeta import UnknownUiCall
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import registry
from noeta.core.agent import Agent
from noeta.core.extension import CommandContext, Extension
from noeta.core.loop.state import CONFIG_SIGNAL, PLUGINS_OFF
from noeta.host.app import change_config
from noeta.host.http import create_app
from noeta.host.local import LocalApp
from noeta.host.plugins import UI_API, MarketEntry, PluginAdmin, plugin_infos, switchable
from noeta.host.remote import RemoteApp
from noeta.plugins.title import Title
from noeta.testing import ScriptedProvider, reply

H = {"authorization": "Bearer secret"}

PANEL = '''
from noeta import Command, Extension, Notice, StatusItem, Widget


class Panel(Extension):
    """Shows a panel."""

    name = "panel"

    def initial_state(self):
        return 0

    def reduce(self, state, kind, data):
        return state + 1 if kind == "clicked" else state

    async def ui(self, view):
        clicks = view.ext_state(self.name) or 0
        return [StatusItem(key="clicks", text=f"{clicks} clicks", tone="ok"),
                Widget(key="w", markdown="**hi**", placement="below"),
                Notice(key="n", text="welcome")]

    async def ui_call(self, ctx, name, args):
        if name == "echo":
            return {"task": ctx.task_id, "args": args}
        if name == "buggy":
            return {}["missing"]
        return await super().ui_call(ctx, name, args)

    async def commands(self, ctx):
        return [Command(name="panel", description="Open the panel", template="panel")]
'''


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    saved = (dict(registry._files), dict(registry._broken), set(registry._loaded_paths),
             dict(registry._missing))
    yield
    registry._files.clear()
    registry._files.update(saved[0])
    registry._broken.clear()
    registry._broken.update(saved[1])
    registry._missing.clear()
    registry._missing.update(saved[3])
    registry._loaded_paths.clear()
    registry._loaded_paths.update(saved[2])


def _panel_plugin(root: Path, *, ui_api: int = 1) -> Extension:
    plugin = root / "plugins" / "panel"
    (plugin / ".noeta-plugin").mkdir(parents=True)
    (plugin / "web").mkdir()
    (plugin / "__init__.py").write_text(PANEL)
    (plugin / ".noeta-plugin" / "plugin.json").write_text(json.dumps({
        "name": "panel", "version": "0.2.0", "description": "A panel", "ui_api": ui_api,
        "web": "web/index.js"}))
    (plugin / "web" / "index.js").write_text("export function activate(ctx) {}\n")
    (plugin / "web" / "style.css").write_text("p {}\n")
    (root / "secret.txt").write_text("not for the web")
    assert registry.load_plugin_files([root / "plugins"]) == ["panel"]
    made: Extension = registry.load(registry.PLUGINS, "panel")()
    return made


class Admin:
    """A ``PluginAdmin`` keeping its switches in memory."""

    def __init__(self) -> None:
        self.off: set[str] = set()
        self.installed: list[tuple[str, str | None, str | None]] = []
        self.removed: list[str] = []

    def admin(self) -> PluginAdmin:
        def install(source: str, market: str | None, commit: str | None) -> str:
            self.installed.append((source, market, commit))
            return "panel"

        def market(notices: list[str]) -> list[MarketEntry]:
            notices.append("marketplace team could not be read")
            return [{"marketplace": "official", "name": "panel", "description": "",
                     "version": "1", "source": "x", "commit": "abc", "parts": [],
                     "ui_api": 1, "installed": False}]

        return PluginAdmin(
            default_off=lambda: sorted(self.off),
            set_default=lambda n, on: self.off.discard(n) if on else self.off.add(n),
            turned_off=lambda: {"gone"}, install=install, remove=self.removed.append,
            market=market)


@pytest.fixture
async def local(tmp_path: Path) -> AsyncIterator[tuple[LocalApp, ScriptedProvider, Admin]]:
    provider = ScriptedProvider()
    agent = Agent(name="main", model="m", extensions=[Title(), _panel_plugin(tmp_path)])
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                   env_for=lambda _s: None)
    admin = Admin()
    app.plugin_admin = admin.admin()
    await app.start()
    yield app, provider, admin
    await app.stop()


def _http(app: object, base: str = "http://127.0.0.1") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
                             base_url=base, headers=H, timeout=10)


async def _idle(app: LocalApp, task_id: str) -> None:
    for _ in range(500):
        if (await app.client.get(task_id)).status in ("waiting", "ended"):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the task never settled")


async def test_the_listing_names_parts_switches_and_the_web_entry(
        local: tuple[LocalApp, ScriptedProvider, Admin]) -> None:
    app, _, admin = local
    admin.off.add("panel")
    title, panel = await app.plugins("main")
    assert title["builtin"] and title["switchable"] and title["enabled"]
    assert title["source"] == "package noeta" and title["parts"] == ["backend"]
    assert panel["name"] == "panel" and not panel["builtin"] and panel["switchable"]
    assert not panel["enabled"]  # off for new tasks
    assert panel["version"] == "0.2.0" and panel["description"] == "A panel"
    assert panel["parts"] == ["backend", "ui", "web"] and panel["ui_api"] == 1
    assert panel["web"] is not None and panel["broken"] is None
    assert panel["web"]["entry"].startswith("/plugins/panel/web/index.js?v=")
    assert not switchable("permissions") and switchable("title") and switchable("panel")


def test_a_web_part_for_another_ui_api_is_left_unloaded(tmp_path: Path) -> None:
    ext = _panel_plugin(tmp_path, ui_api=7)
    [info] = plugin_infos(Agent(name="a", model="m", extensions=[ext]), PluginAdmin())
    assert info["web"] is None and "ui_api 7" in str(info["broken"])


def test_plugins_in_no_agent_and_broken_files_are_listed(tmp_path: Path) -> None:
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "bad.py").write_text("raise RuntimeError('nope')\n")
    (tmp_path / "plugins" / "lonely.py").write_text(
        "from noeta import Extension\nclass L(Extension):\n    name = 'lonely'\n")
    registry.load_plugin_files([tmp_path / "plugins"])
    admin = PluginAdmin(turned_off=lambda: {"lonely"}, others=lambda cwd: [{
        "name": "cc", "version": "1", "description": "", "source": cwd, "builtin": False,
        "enabled": True, "switchable": False, "broken": None, "parts": ["skills"],
        "ui_api": None, "web": None}])
    found = {p["name"]: p for p in plugin_infos(None, admin, "/here")}
    assert found["lonely"]["broken"] == "switched off in the settings"
    assert "nope" in str(found["bad"]["broken"]) and not found["bad"]["enabled"]
    assert found["cc"]["source"] == "/here"


async def test_ui_items_and_calls_in_process_over_http_and_remote(
        local: tuple[LocalApp, ScriptedProvider, Admin], tmp_path: Path) -> None:
    app, provider, _ = local
    provider.add(reply("hi"))
    tid = await app.client.create("main", "hi", config={"interactive": False})
    await _idle(app, tid)
    items = await app.ui(tid)
    assert items == [
        {"plugin": "panel", "type": "status", "key": "clicks", "text": "0 clicks", "tone": "ok"},
        {"plugin": "panel", "type": "widget", "key": "w", "markdown": "**hi**",
         "placement": "below"},
        {"plugin": "panel", "type": "notice", "key": "n", "text": "welcome", "level": "info"}]
    api = create_app(app.client, token="secret", app=app, cwd=str(tmp_path))
    async with _http(api) as http:
        assert (await http.get(f"/tasks/{tid}/ui")).json() == {"items": items}
        called = await http.post("/plugins/panel/call",
                                 json={"task_id": tid, "name": "echo", "args": [1]})
        assert called.json() == {"result": {"task": tid, "args": [1]}}
        missing = await http.post("/plugins/panel/call", json={"task_id": tid, "name": "nope"})
        assert missing.status_code == 404
        assert (await http.post("/plugins/title/call", json={
            "task_id": tid, "name": "x"})).status_code == 404
        # A plugin's own bug is no unknown call: a 500, its traceback logged.
        buggy = await http.post("/plugins/panel/call", json={"task_id": tid, "name": "buggy"})
        assert buggy.status_code == 500 and "KeyError" in buggy.json()["error"]
        listed =(await http.get("/plugins", params={"agent": "main"})).json()
        assert listed["ui_api"] == list(UI_API)
        assert [p["name"] for p in listed["plugins"]] == ["title", "panel"]
    remote = RemoteApp("http://127.0.0.1", token="secret",
                       transport=httpx.ASGITransport(app=api))  # type: ignore[arg-type]
    try:
        assert await remote.ui(tid) == items
        assert await remote.plugin_call(tid, "panel", "echo", {"a": 1}) == {
            "task": tid, "args": {"a": 1}}
        with pytest.raises(UnknownUiCall):
            await remote.plugin_call(tid, "panel", "nope", None)
        with pytest.raises(httpx.HTTPStatusError):
            await remote.plugin_call(tid, "panel", "buggy", None)
        assert [p["name"] for p in await remote.plugins("main")] == ["title", "panel"]
        await remote.set_plugin_default("panel", False)
        assert app.plugin_admin.default_off() == ["panel"]
        notices: list[str] = []
        assert [e["name"] for e in await remote.market(notices)] == ["panel"]
        assert notices == ["marketplace team could not be read"]
    finally:
        await remote.stop()


async def test_a_task_switched_off_shows_no_ui_and_lists_no_commands(
        local: tuple[LocalApp, ScriptedProvider, Admin]) -> None:
    app, provider, _ = local
    provider.add(reply("hi"))
    tid = await app.client.create("main", "hi", config={"interactive": False})
    await _idle(app, tid)
    ctx = CommandContext(cwd="", task_id=tid)
    assert "panel" in [c.name for c in await app.commands("main", ctx)]
    await change_config(app.client, tid, {PLUGINS_OFF: ["panel"]})
    for _ in range(200):  # the signal is taken in by the worker
        if await app.ui(tid) == []:
            break
        await asyncio.sleep(0.01)
    assert await app.ui(tid) == []
    assert "panel" not in [c.name for c in await app.commands("main", ctx)]
    assert await app.run_command("main", ctx, "/panel") is None
    with pytest.raises(UnknownUiCall):
        await app.plugin_call(tid, "panel", "echo", None)


async def test_new_tasks_start_with_the_machines_switches(
        local: tuple[LocalApp, ScriptedProvider, Admin], tmp_path: Path) -> None:
    app, provider, admin = local
    await app.set_plugin_default("panel", False)
    assert admin.off == {"panel"}
    with pytest.raises(ValueError, match="permissions"):
        await app.set_plugin_default("permissions", False)
    provider.add(reply("hi"))
    tid = await app.client.create("main", "hi", config={PLUGINS_OFF: ["title"],
                                                        "interactive": False})
    first = (await app.client.events(tid))[0].event
    assert first.config[PLUGINS_OFF] == ["panel", "title"]  # type: ignore[attr-defined]
    api = create_app(app.client, token="secret", app=app, cwd=str(tmp_path))
    async with _http(api) as http:
        put = await http.put("/plugins/panel", json={"enabled": True})
        assert put.status_code == 200 and admin.off == set()
        assert (await http.put("/plugins/panel", json={"enabled": "yes"})).status_code == 400
        refused = await http.post("/tasks", json={"agent": "main", "config": {
            PLUGINS_OFF: ["permissions"]}})
        assert refused.status_code == 403
        signal = await http.post(f"/tasks/{tid}/signals", json={
            "name": CONFIG_SIGNAL, "data": {PLUGINS_OFF: ["fs", "web"]}})
        assert signal.status_code == 403 and "fs" in signal.json()["error"]
        ok = await http.post(f"/tasks/{tid}/signals", json={
            "name": CONFIG_SIGNAL, "data": {PLUGINS_OFF: ["web"]}})
        assert ok.status_code == 200


async def test_web_files_are_served_without_a_token_and_only_inside(
        local: tuple[LocalApp, ScriptedProvider, Admin], tmp_path: Path) -> None:
    app, _, _ = local
    api = create_app(app.client, token="secret", app=app, cwd=str(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),  # type: ignore[arg-type]
                                 base_url="http://127.0.0.1") as http:
        js = await http.get("/plugins/panel/web/index.js")
        assert js.status_code == 200 and js.headers["content-type"].startswith(
            "text/javascript")
        assert js.headers["x-content-type-options"] == "nosniff"
        assert (await http.get("/plugins/panel/web/style.css")).headers[
            "content-type"].startswith("text/css")
        for path in ("/plugins/panel/web/../../../secret.txt",
                     "/plugins/panel/web/%2e%2e/%2e%2e/%2e%2e/secret.txt",
                     "/plugins/panel/web/nope.js", "/plugins/title/web/index.js",
                     "/plugins/unknown/web/index.js"):
            assert (await http.get(path)).status_code == 404, path
        assert (await http.get("/plugins")).status_code == 401  # the rest needs the token


async def test_install_and_remove_only_where_allowed(
        local: tuple[LocalApp, ScriptedProvider, Admin], tmp_path: Path) -> None:
    app, _, admin = local
    reloads: list[int] = []
    app.reload_plugins = lambda: reloads.append(1) or ["no plugin changed"]  # type: ignore[func-returns-value]
    shut = create_app(app.client, token="secret", app=app, cwd=str(tmp_path))
    async with _http(shut) as http:
        assert (await http.post("/plugins/install", json={"source": "x"})).status_code == 403
        assert (await http.delete("/plugins/panel")).status_code == 403
        market = (await http.get("/market")).json()
        assert market["entries"][0]["name"] == "panel"
        assert market["notices"] == ["marketplace team could not be read"]
    open_ = create_app(app.client, token="secret", app=app, cwd=str(tmp_path),
                       allow_plugin_install=True)
    async with _http(open_) as http:
        made = await http.post("/plugins/install", json={"source": "x", "commit": "c"})
        assert made.status_code == 201 and made.json()["name"] == "panel"
        assert (await http.post("/plugins/install", json={})).status_code == 400
        assert (await http.delete("/plugins/panel")).status_code == 200
    assert admin.installed == [("x", None, "c")] and admin.removed == ["panel"]
    assert reloads == [1, 1]


async def test_an_app_without_an_admin_says_it_cannot(tmp_path: Path) -> None:
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [Agent(name="main", model="m")],
                   ScriptedProvider(), env_for=lambda _s: None)
    try:
        with pytest.raises(NotImplementedError):
            await app.set_plugin_default("title", False)
        with pytest.raises(NotImplementedError):
            await app.install_plugin("x")
        with pytest.raises(NotImplementedError):
            await app.remove_plugin("x")
        assert await app.market() == []
        api = create_app(app.client, token="secret", app=app, allow_plugin_install=True)
        async with _http(api) as http:
            assert (await http.put("/plugins/title", json={"enabled": False})).status_code \
                == 501
            assert (await http.post("/plugins/install", json={"source": "x"})).status_code \
                == 501
            assert (await http.delete("/plugins/x")).status_code == 501
    finally:
        await app.store.close()


async def test_an_api_only_server_lists_plugins_and_shows_no_ui(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    from noeta.core.client import Client

    client = Client(store)
    agents: dict[str, Agent] = {"main": Agent(name="main", model="m", extensions=[Title()])}
    api = create_app(client, token="secret", agents=agents)
    tid = await client.create("main", None)
    async with _http(api) as http:
        assert [p["name"] for p in (await http.get("/plugins")).json()["plugins"]] == ["title"]
        assert (await http.get(f"/tasks/{tid}/ui")).json() == {"items": []}
        assert (await http.post("/plugins/title/call", json={
            "task_id": tid, "name": "x"})).status_code == 404
        assert (await http.put("/plugins/title", json={"enabled": False})).status_code == 501
        assert (await http.get("/market")).json() == {"entries": [], "notices": []}
    await store.close()


async def test_an_api_only_server_offers_no_commands_of_a_plugin_a_task_runs_without(
        tmp_path: Path) -> None:
    from noeta.core.client import Client
    from noeta.core.extension import Command

    class Panel(Extension):
        name = "panel"

        async def commands(self, ctx: CommandContext) -> list[Command]:
            return [Command(name="panel", description="Open the panel", template="panel")]

    store = SqliteStore(str(tmp_path / "t.db"))
    client = Client(store)
    agents: dict[str, Agent] = {"main": Agent(name="main", model="m", extensions=[Panel()])}
    api = create_app(client, token="secret", agents=agents)
    on = await client.create("main", None)
    off = await client.create("main", None, config={PLUGINS_OFF: ["panel"]})
    async with _http(api) as http:
        def listed(tid: str) -> Any:
            return http.get("/commands", params={"agent": "main", "task_id": tid})

        def run(tid: str) -> Any:
            return http.post("/commands/run", json={"agent": "main", "task_id": tid,
                                                    "text": "/panel"})
        assert [c["name"] for c in (await listed(on)).json()] == ["panel"]
        assert (await run(on)).json() is not None
        assert (await listed(off)).json() == []
        assert (await run(off)).json() is None
    await store.close()

