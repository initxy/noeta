"""The machine's plugin switches (``~/.noeta/plugin-switches.json``) and the
admin the ``noeta`` command hands its app: new tasks start with the
switches, install and remove go through ``noeta plugin``'s code."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from noeta.cli import home_dir, plugin_command, plugin_switches
from noeta.cli.config import from_settings, load_settings
from noeta.core.loop.state import PLUGINS_OFF


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(home_dir, "HOME", tmp_path / "home")
    return tmp_path / "home"


def test_switches_are_kept_in_the_home_directory(home: Path) -> None:
    assert plugin_switches.default_off() == []
    plugin_switches.set_default("web", False)
    plugin_switches.set_default("todos", False)
    plugin_switches.set_default("web", True)
    assert json.loads((home / "plugin-switches.json").read_text()) == {"off": ["todos"]}
    assert plugin_switches.default_off() == ["todos"]
    (home / "plugin-switches.json").write_text("{not json")
    assert plugin_switches.default_off() == []
    (home / "plugin-switches.json").write_text('{"off": "web"}')
    assert plugin_switches.default_off() == []


def test_install_and_remove_go_through_noeta_plugin(home: Path, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "wave.py"
    source.write_text("from noeta import Extension\nclass W(Extension):\n    name = 'wave'\n")
    admin = plugin_switches.make_admin(str(tmp_path), [], lambda: ())
    assert admin.install is not None and admin.remove is not None and admin.market is not None
    assert admin.install(str(source), None, None) == "wave"
    assert (home / "plugins" / "wave.py").is_file()
    with pytest.raises(ValueError, match="already"):
        admin.install(str(source), None, None)
    admin.remove("wave")
    assert not (home / "plugins" / "wave.py").exists()
    with pytest.raises(ValueError, match="no plugin"):
        admin.remove("wave")
    with pytest.raises(ValueError, match="no such file"):
        admin.install(str(tmp_path / "missing"), None, None)
    monkeypatch.setattr(plugin_command, "market_entries", lambda: [{"name": "x"}])
    assert admin.market() == [{"name": "x"}]


def test_a_plugin_with_options_is_not_installed_over_http(home: Path, tmp_path: Path) -> None:
    plugin = tmp_path / "opts"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / "skills").mkdir()
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "opts", "userConfig": {"key": {"type": "string", "title": "Key"}}}))
    admin = plugin_switches.make_admin(str(tmp_path), [], lambda: ())
    assert admin.install is not None
    with pytest.raises(ValueError, match="options"):
        admin.install(str(plugin), None, None)
    assert not (home / "plugins" / "opts").exists()


async def test_the_app_starts_new_tasks_with_the_switches_and_lists_claude_plugins(
        home: Path, tmp_path: Path) -> None:
    home.mkdir()
    (home / "config.toml").write_text(
        f'model = "m"\napi_key = "k"\nstore = "sqlite:///{tmp_path / "t.db"}"\n'
        '[plugins.title]\nenabled = false\n')
    cc = home / "plugins" / "helper"
    (cc / "skills" / "s").mkdir(parents=True)
    (cc / "skills" / "s" / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\nbody\n")
    plugin_switches.set_default("todos", False)
    plugin_switches.set_default("permissions", False)  # never joined: no task runs without it
    app = from_settings(load_settings(), cwd=str(tmp_path))
    try:
        assert "title" in app.plugin_admin.turned_off()
        tid = await app.client.create("main", None)
        first = (await app.client.events(tid))[0].event
        assert first.config[PLUGINS_OFF] == ["todos"]  # type: ignore[attr-defined]
        listed = {p["name"]: p for p in await app.plugins("main", str(tmp_path))}
        assert listed["helper"]["parts"] == ["skills"] and not listed["helper"]["switchable"]
        assert not listed["todos"]["enabled"] and listed["todos"]["switchable"]
        assert listed["permissions"]["enabled"] and not listed["permissions"]["switchable"]
    finally:
        await app.store.close()


async def test_a_plugin_the_settings_keep_on_cannot_be_switched_off(
        home: Path, tmp_path: Path) -> None:
    """``switchable = false`` in a plugin's table: no switch is offered, the
    machine's switch is ignored, and a request switching it off is refused."""
    import httpx

    from noeta.host.http import create_app

    home.mkdir()
    (home / "config.toml").write_text(
        f'model = "m"\napi_key = "k"\nstore = "sqlite:///{tmp_path / "t.db"}"\n'
        '[plugins.todos]\nswitchable = false\n')
    plugin_switches.set_default("todos", False)
    app = from_settings(load_settings(), cwd=str(tmp_path))
    try:
        assert "todos" in app.plugin_admin.fixed()
        tid = await app.client.create("main", None)
        assert PLUGINS_OFF not in (await app.client.events(tid))[0].event.config  # type: ignore[attr-defined]
        listed = {p["name"]: p for p in await app.plugins("main", str(tmp_path))}
        assert listed["todos"]["enabled"] and not listed["todos"]["switchable"]
        with pytest.raises(ValueError, match="cannot be switched off"):
            await app.set_plugin_default("todos", True)
        api = create_app(app.client, token="secret", app=app)
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1",
                                     headers={"authorization": "Bearer secret"}) as http:
            made = await http.post("/tasks", json={"agent": "main",
                                                   "config": {PLUGINS_OFF: ["todos"]}})
            assert made.status_code == 403
            sent = await http.post(f"/tasks/{tid}/signals", json={
                "name": "config", "data": {PLUGINS_OFF: ["todos"]}})
            assert sent.status_code == 403
            assert (await http.put("/plugins/todos", json={"enabled": False})).status_code \
                == 400
    finally:
        await app.store.close()


class _Stop(Exception):
    pass


@pytest.mark.parametrize(("argv", "allowed"), [
    (["--no-token"], True), (["--host", "10.1.2.3"], False),
    (["--host", "10.1.2.3", "--allow-plugin-install"], True),
    # A reverse proxy in front of the loopback bind: not a local server.
    (["--no-token", "--allow-host", "noeta.example.com"], False),
    (["--no-token", "--allow-origin", "https://noeta.example.com"], False),
    (["--no-token", "--allow-host", "noeta.example.com", "--allow-plugin-install"], True)])
def test_serve_installs_over_http_only_on_loopback_or_when_allowed(
        argv: list[str], allowed: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.cli import main as cli
    from noeta.host import http
    from noeta.host.plugins import PluginAdmin

    seen: dict[str, object] = {}

    class App:
        plugin_admin = PluginAdmin()

        def __init__(self) -> None:
            self.client = type("C", (), {"task_config": {}})()
            self.runtime = type("R", (), {"agents": {}})()

        async def start(self, worker: bool = True) -> None:
            return None

        async def stop(self) -> None:
            return None

        def deltas(self, sink: object) -> object:
            return lambda: None

    def create_app(_client: object, **kw: object) -> object:
        seen.update(kw)
        raise _Stop

    monkeypatch.setenv("NOETA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "_app", lambda _s, **_kw: App())
    monkeypatch.setattr(http, "create_app", create_app)
    with pytest.raises(_Stop):
        cli.main(["--model", "m", "--store", f"sqlite:///{tmp_path}/t.db", "serve", *argv])
    assert seen["allow_plugin_install"] is allowed
    assert isinstance(seen["plugin_admin"], PluginAdmin)
