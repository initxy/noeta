"""``/plugins`` and what plugins show: the menu switches a plugin for this
task (from its next turn on) and for new tasks, removes one and installs
one from Discover after showing where it comes from; a plugin's status
items, widgets and notices follow the task's log; a server that refuses to
install says why."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.events import External
from noeta.core.extension import Extension, Notice, StatusItem, TaskView, UiItem, Widget
from noeta.host.local import LocalApp
from noeta.host.plugins import MarketEntry, PluginAdmin
from noeta.testing import ScriptedProvider, reply
from noeta_tui import plugin_ui
from noeta_tui.app import NoetaApp
from noeta_tui.widgets import Picker

from support import make_app, plain, ready, settle, shown, type_line


class Panel(Extension):
    """Counts the turns."""

    name = "panel"

    async def ui(self, view: TaskView) -> Sequence[UiItem]:
        turns = sum(1 for m in view.state.messages if m.role == "user")
        return [StatusItem(key="turns", text=f"{turns} turns", tone="ok"),
                StatusItem(key="turns", text=f"turn {turns}", tone="warn"),  # same key: last
                Widget(key="w", markdown="**over the prompt**"),
                Widget(key="b", markdown="under the prompt", placement="below"),
                Notice(key="n", text="welcome", level="warn")]


class Admin:
    """The machine's plugins, in memory."""

    def __init__(self) -> None:
        self.off: set[str] = set()
        self.installed: list[tuple[str, str | None, str | None]] = []
        self.removed: list[str] = []

    def admin(self) -> PluginAdmin:
        def install(source: str, market: str | None, commit: str | None) -> str:
            self.installed.append((source, market, commit))
            return "panel"

        entry = MarketEntry(marketplace="official", name="lint-bar", description="Lint status",
                            version="0.3.0", source="https://git.example/lint-bar.git",
                            commit="0123abcd", parts=["backend", "ui"], ui_api=1,
                            installed=False)
        return PluginAdmin(default_off=lambda: sorted(self.off),
                           set_default=lambda n, on: self.off.discard(n) if on
                           else self.off.add(n),
                           install=install, remove=self.removed.append,
                           market=lambda notices: [entry])


def _picker_title(app: NoetaApp) -> str:
    pickers = app.query(Picker)
    return pickers.first(Picker).title_text if pickers else ""


async def _pick(pilot: Any, app: NoetaApp, title: str, label: str) -> None:
    """In the picker whose title starts with ``title``, pick ``label``."""
    await settle(pilot, lambda: _picker_title(app).startswith(title))
    options = app.query(Picker).first(Picker).options
    n = next(i for i, o in enumerate(options, 1) if o.label == label)
    await pilot.press(str(n))


async def test_status_items_widgets_and_notices_follow_the_task(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("one"), reply("two")])
    app = make_app(tmp_path, provider, Panel())
    async with app.run_test(size=(110, 40)) as pilot:
        await settle(pilot, lambda: ready(app))
        assert not app.query_one("#plugin-status").display
        await type_line(pilot, "hi")
        status = app.query_one("#plugin-status")
        await settle(pilot, lambda: status.display and plain(status) == "turn 1")
        assert app.query_one("#widgets-above").display and app.query_one("#widgets-below").display
        assert "**over the prompt**" in shown(app) and "under the prompt" in shown(app)
        await settle(pilot, lambda: "panel: welcome" in shown(app))
        await settle(pilot, lambda: not app.busy)
        await type_line(pilot, "again")
        await settle(pilot, lambda: plain(status) == "turn 2")
        assert shown(app).count("panel: welcome") == 1  # a notice shows once
        # The menu under the prompt takes the plugins' line's place.
        app.query_one("#prompt").text = "/"
        await settle(pilot, lambda: not status.display and not app.query_one("#footer").display)
        app.query_one("#prompt").text = ""
        await settle(pilot, lambda: status.display)
        # Another task's screen shows none of it.
        await type_line(pilot, "/new")
        await settle(pilot, lambda: not status.display
                     and not app.query_one("#widgets-above").display)


async def test_a_failing_ui_fetch_is_logged_not_raised(tmp_path: Path,
                                                       caplog: pytest.LogCaptureFixture) -> None:
    app = make_app(tmp_path, ScriptedProvider([reply("one")]), Panel())

    async def broken(task_id: str) -> list[Any]:
        raise RuntimeError("the server is gone")

    app.local.ui = broken
    async with app.run_test(size=(110, 40)) as pilot:
        await type_line(pilot, "hi")
        await settle(pilot, lambda: "fetching the plugins' UI" in caplog.text)
        await settle(pilot, lambda: not app.busy)
        assert not app.query_one("#plugin-status").display


async def test_plugins_switches_removes_and_installs(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("one"), reply("two")])
    app = make_app(tmp_path, provider, Panel())
    admin = Admin()
    app.local.plugin_admin = admin.admin()
    async with app.run_test(size=(120, 44)) as pilot:
        await type_line(pilot, "hi")
        status = app.query_one("#plugin-status")
        await settle(pilot, lambda: plain(status) == "turn 1" and not app.busy)
        await type_line(pilot, "/plugins")
        await settle(pilot, lambda: _picker_title(app).startswith("Plugins"))
        options = {o.label: o.detail for o in app.query(Picker).first(Picker).options}
        assert options["panel"].startswith("this task: on · new tasks: on")
        assert "backend, ui" in options["panel"]
        assert options["fs"].startswith("always on")  # every task needs it: no switch
        assert "Discover plugins…" in options
        # This task's switch: from the next turn on.
        await _pick(pilot, app, "Plugins", "panel")
        await _pick(pilot, app, "panel", "Turn off for this task")
        await settle(pilot, lambda: "panel off for this task, from the next turn on"
                     in shown(app))
        inbox = [i.body for i in await app.local.store.inbox(app.task_id)]
        assert External(name="config", data={"plugins_off": ["panel"]}) in inbox
        assert app.plugins_off == ["panel"]
        await settle(pilot, lambda: _picker_title(app).startswith("Plugins"))
        options = {o.label: o.detail for o in app.query(Picker).first(Picker).options}
        assert options["panel"].startswith("this task: off · new tasks: on")
        # The machine's default for new tasks.
        await _pick(pilot, app, "Plugins", "panel")
        await _pick(pilot, app, "panel", "Turn off for new tasks")
        await settle(pilot, lambda: "panel off for new tasks" in shown(app))
        assert admin.off == {"panel"}
        # The task's next turn runs without it: its UI is gone.
        await pilot.press("escape")
        await settle(pilot, lambda: not app.query(Picker))
        await type_line(pilot, "again")
        await settle(pilot, lambda: not status.display and not app.busy)
        # Back, and Esc, go back to the list.
        await type_line(pilot, "/plugins")
        await _pick(pilot, app, "Plugins", "panel")
        await _pick(pilot, app, "panel", "Back")
        await _pick(pilot, app, "Plugins", "panel")
        await _pick(pilot, app, "panel", "Remove")
        await _pick(pilot, app, "Remove panel?", "No")
        assert admin.removed == []
        await _pick(pilot, app, "Plugins", "panel")
        await _pick(pilot, app, "panel", "Remove")
        await _pick(pilot, app, "Remove panel?", "Yes, remove it")
        await settle(pilot, lambda: admin.removed == ["panel"])
        # Discover: the source, commit and parts are shown before installing.
        await _pick(pilot, app, "Plugins", "Discover plugins…")
        await _pick(pilot, app, "Discover", "lint-bar")
        await settle(pilot, lambda: _picker_title(app).startswith("Install lint-bar"))
        about = _picker_title(app)
        assert "https://git.example/lint-bar.git" in about and "commit: 0123abcd" in about
        assert "parts: backend, ui" in about
        await _pick(pilot, app, "Install lint-bar", "No")
        await _pick(pilot, app, "Discover", "lint-bar")
        await _pick(pilot, app, "Install lint-bar", "Yes, install it")
        await settle(pilot, lambda: admin.installed == [("lint-bar", "official", "0123abcd")])
        await settle(pilot, lambda: "installed panel" in shown(app))
        await pilot.press("escape")
        await settle(pilot, lambda: not app.query(Picker))


async def test_a_new_screen_switches_its_task_with_the_machines_defaults(
        tmp_path: Path) -> None:
    """With no task yet the menu shows the switches the task would start
    with; switching one starts the task, keeping the machine's others."""
    app = make_app(tmp_path, ScriptedProvider([]), Panel())
    admin = Admin()
    admin.off = {"where"}
    app.local.plugin_admin = admin.admin()
    async with app.run_test(size=(120, 44)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "/plugins")
        await settle(pilot, lambda: _picker_title(app).startswith("Plugins"))
        options = {o.label: o.detail for o in app.query(Picker).first(Picker).options}
        assert options["where"].startswith("this task: off · new tasks: off")
        assert app.task_id == ""
        await _pick(pilot, app, "Plugins", "panel")
        await _pick(pilot, app, "panel", "Turn off for this task")
        await settle(pilot, lambda: bool(app.task_id) and app.plugins_off == ["panel", "where"])
        # The task started without the machine's ``where`` and took the change.
        await settle(pilot, lambda: _logged(app, ["panel", "where"]))


async def _logged(app: NoetaApp, off: list[str]) -> bool:
    return await plugin_ui.logged_off(app) == off


async def test_a_switch_a_host_cannot_keep_is_shown(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]), Panel())  # no admin: no switches
    async with app.run_test(size=(120, 44)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "/plugins discover")
        await settle(pilot, lambda: "no plugins found in the configured marketplaces"
                     in shown(app))
        await _pick(pilot, app, "Plugins", "panel")
        await _pick(pilot, app, "panel", "Turn off for new tasks")
        await settle(pilot, lambda: "/plugins failed: this host keeps no plugin switches"
                     in shown(app))


async def test_a_server_that_refuses_to_remove_says_why(tmp_path: Path) -> None:
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("starlette")
    from noeta.host.http import create_app
    from noeta.host.remote import RemoteApp

    agent = Agent(name="main", model="claude-sonnet-4-5", extensions=[Panel()])
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], ScriptedProvider([]),
                     env_for=lambda _s: None)
    admin = Admin()
    local.plugin_admin = admin.admin()
    api = create_app(local.client, token="secret", app=local, cwd=str(tmp_path),
                     plugin_admin=local.plugin_admin)
    remote = RemoteApp("http://127.0.0.1", token="secret",
                       transport=httpx.ASGITransport(app=api))
    app = NoetaApp(remote, agent="main", model="the server's model", cwd=str(tmp_path),
                   task_config={}, on_server=True)
    async with app.run_test(size=(120, 44)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "/plugins")
        await _pick(pilot, app, "Plugins", "panel")
        await settle(pilot, lambda: any(o.detail == "the server's default"
                                        for o in app.query(Picker).first(Picker).options))
        await _pick(pilot, app, "panel", "Remove")
        await _pick(pilot, app, "Remove panel?", "Yes, remove it")
        await settle(pilot, lambda: "/plugins failed: this server does not install or remove "
                     "plugins over HTTP" in shown(app))
        assert admin.removed == []
