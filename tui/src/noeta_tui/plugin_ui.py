"""The task's plugins on the screen: what they show (``AppAPI.ui``: status
items in a line under the footer, markdown widgets over or under the prompt,
notices in the transcript) and ``/plugins``, the menu that switches them for
this task or for new tasks, removes them and installs them from the
marketplaces. Everything a plugin says is shown as plain text, never markup."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from rich.text import Text

from noeta.core.events import External, InputTaken, TaskCreated
from noeta.core.loop.state import CONFIG_SIGNAL, PLUGINS_OFF, off_names
from noeta.host.app import change_config
from noeta_tui import render
from noeta_tui.widgets import Option, Picker

if TYPE_CHECKING:
    from noeta_tui.app import NoetaApp

__all__ = ["TONES", "logged_off", "menu", "status_line"]

#: A status item's colour per tone.
TONES = {"normal": "dim", "ok": "green", "warn": "yellow", "error": "red"}
#: The menu's line that opens Discover (no plugin name has a NUL).
DISCOVER = "\0discover"
#: What the actions picker says when a switch is in effect.
NEXT_TURN = "from the next turn on"


def status_line(items: Iterable[Mapping[str, Any]]) -> Text:
    """The plugins' status items in one line, each in its tone's colour."""
    out = Text(no_wrap=True, overflow="ellipsis")
    for n, item in enumerate(items):
        out.append("  ·  " if n else "", style="dim")
        out.append(render.clean(" ".join(str(item.get("text") or "").split())),
                   style=TONES.get(str(item.get("tone") or ""), "dim"))
    return out


def keyed(items: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """``items`` with one per plugin, type and key (the last), in
    first-seen order."""
    found: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for item in items:
        found[(str(item.get("plugin") or ""), str(item.get("type") or ""),
               str(item.get("key") or ""))] = item
    return list(found.values())


async def logged_off(app: NoetaApp) -> list[str]:
    """The task's ``plugins_off`` as its log has it (its start, then each
    config change it took)."""
    off: list[str] = []
    for rec in await app.local.client.events(app.task_id):
        ev = rec.event
        if isinstance(ev, TaskCreated):
            off = off_names(ev.config)
        elif isinstance(ev, InputTaken):
            for item in ev.items:
                body = item.body
                if isinstance(body, External) and body.name == CONFIG_SIGNAL \
                        and isinstance(body.data, dict) and PLUGINS_OFF in body.data:
                    off = off_names(body.data)
    return off


def _task_off(app: NoetaApp, infos: list[dict[str, Any]]) -> set[str]:
    """The plugins this screen's task runs without: its own switches, or on
    a screen with no task yet the ones it would start without."""
    if app.task_id:
        return set(app.plugins_off)
    return set(off_names(app.task_config)) | {
        p["name"] for p in infos if p.get("switchable") and not p.get("enabled")}


def _state(info: Mapping[str, Any], off: set[str]) -> str:
    if not info.get("switchable"):
        return f"✘ {info['broken']}" if info.get("broken") and not info.get("enabled") \
            else "always on"
    here = "off" if info["name"] in off else "on"
    later = "on" if info.get("enabled") else "off"
    return f"this task: {here} · new tasks: {later}"


def _detail(info: Mapping[str, Any], off: set[str]) -> str:
    bits = [_state(info, off)]
    if info.get("version"):
        bits.append(f"v{info['version']}")
    bits.append(", ".join(info.get("parts") or []) or "no parts")
    if info.get("broken") and info.get("enabled"):
        bits.append(f"✘ {info['broken']}")
    return " · ".join(bits)


async def _show(app: NoetaApp, picker: Picker) -> None:
    from noeta_tui.slash import _picker  # slash imports this module

    await _picker(app, picker, "/plugins")


def _cwd(app: NoetaApp) -> str:
    """The directory whose plugins are listed: on a server, the one the
    task runs in there (``""``: the server's own)."""
    return app.task_cwd if app.on_server else (app.task_cwd or app.cwd)


async def menu(app: NoetaApp, arg: str = "", *, at: str = "") -> None:
    """``/plugins``: every plugin, how it is switched for this task and for
    new tasks; picking one offers what can be done with it. ``at``: the
    plugin the cursor starts on."""
    if arg == "discover":
        await discover(app)
        return
    infos: list[dict[str, Any]] = [dict(p) for p in
                                   await app.local.plugins(app.agent, _cwd(app))]
    off = _task_off(app, infos)
    options = [Option(p["name"], p["name"], _detail(p, off)) for p in infos]
    options.append(Option(DISCOVER, "Discover plugins…", "from the configured marketplaces"))
    index = next((n for n, o in enumerate(options) if o.id == at), 0)

    async def picked(name: str) -> None:
        if name == DISCOVER:
            await discover(app)
        elif name:
            await actions(app, next(p for p in infos if p["name"] == name), off)
        else:
            app._box().focus()

    await _show(app, Picker("Plugins — Enter for what to do with one", options, picked,
                              index=index))


def _about(info: Mapping[str, Any]) -> str:
    lines = [f"{info['name']} {info.get('version') or ''}".rstrip()]
    if info.get("description"):
        lines.append(str(info["description"]))
    lines += ["", f"source: {info.get('source') or '?'}",
              f"parts: {', '.join(info.get('parts') or []) or 'none'}"]
    if info.get("broken"):
        lines.append(f"✘ {info['broken']}")
    return "\n".join(lines)


async def actions(app: NoetaApp, info: Mapping[str, Any], off: set[str]) -> None:
    """What can be done with one plugin: its two switches (a plugin every
    task needs has none), removing an installed one; Esc goes back."""
    name = str(info["name"])
    options: list[Option] = []
    if info.get("switchable"):
        here = name not in off
        options.append(Option("task", f"Turn {'off' if here else 'on'} for this task",
                              NEXT_TURN))
        later = bool(info.get("enabled"))
        options.append(Option("default", f"Turn {'off' if later else 'on'} for new tasks",
                              "this machine's default" if not app.on_server
                              else "the server's default"))
    if not info.get("builtin"):
        options.append(Option("remove", "Remove", "uninstall it"))
    options.append(Option("", "Back"))

    async def picked(choice: str) -> None:
        if choice == "task":
            await switch_task(app, name, on=name in off)
        elif choice == "default":
            await app.local.set_plugin_default(name, not info.get("enabled"))
            await app.say_notice(f"{name} {'on' if not info.get('enabled') else 'off'} for "
                                 "new tasks; this task keeps its own switch")
        elif choice == "remove":
            await confirm_remove(app, info)
            return
        await menu(app, at=name)

    await _show(app, Picker(_about(info), options, picked))


async def switch_task(app: NoetaApp, name: str, *, on: bool) -> None:
    """This task runs with ``name`` again, or without it, from its next
    turn on (the ``config`` signal's ``plugins_off``); a screen with no
    task yet starts its task first, with the machine's switches."""
    if not app.task_id:
        await app.ensure_task()
        current = set(await logged_off(app))
    else:
        current = set(app.plugins_off)
    off = sorted(current - {name} if on else current | {name})
    await change_config(app.local.client, app.task_id, {PLUGINS_OFF: off})
    app.plugins_off = off
    app._load_commands()
    await app.say_notice(f"{name} {'on' if on else 'off'} for this task, {NEXT_TURN}")


async def confirm_remove(app: NoetaApp, info: Mapping[str, Any]) -> None:
    name = str(info["name"])

    async def picked(choice: str) -> None:
        if choice != "remove":
            await menu(app, at=name)
            return
        await app.local.remove_plugin(name)
        app._load_commands()
        await app.say_notice(f"removed {name}; tasks stop loading it {NEXT_TURN}")
        await menu(app)

    await _show(app, Picker(f"Remove {name}?\n\nsource: {info.get('source') or '?'}",
                              [Option("remove", "Yes, remove it"), Option("", "No")], picked))


async def discover(app: NoetaApp) -> None:
    """The configured marketplaces' plugins; picking one asks before it is
    installed."""
    entries: list[dict[str, Any]] = [dict(e) for e in await app.local.market()]
    if not entries:
        await app.say_notice("no plugins found in the configured marketplaces")
        await menu(app, at=DISCOVER)
        return
    options = [Option(str(n), str(e.get("name") or "?"), " · ".join(
        bit for bit in (str(e.get("marketplace") or ""),
                        f"v{e['version']}" if e.get("version") else "",
                        "installed" if e.get("installed") else "",
                        str(e.get("description") or "")) if bit))
               for n, e in enumerate(entries)]

    async def picked(n: str) -> None:
        if not n:
            await menu(app, at=DISCOVER)
            return
        await confirm_install(app, entries[int(n)])

    await _show(app, Picker("Discover — Enter to install", options, picked))


async def confirm_install(app: NoetaApp, entry: Mapping[str, Any]) -> None:
    """Install ``entry`` once the person has seen where it comes from: its
    source, the commit it is pinned to and its parts."""
    name = str(entry.get("name") or "")
    about = "\n".join([
        f"Install {name} from {entry.get('marketplace') or '?'}?", "",
        f"source: {entry.get('source') or '?'}", f"commit: {entry.get('commit') or '(none)'}",
        f"parts: {', '.join(entry.get('parts') or []) or 'none'}", "",
        "A plugin runs with noeta's rights. Installing runs nothing from it; tasks load it "
        f"{NEXT_TURN}."])

    async def picked(choice: str) -> None:
        if choice != "install":
            await discover(app)
            return
        info = await app.local.install_plugin(name, entry.get("marketplace") or None,
                                              entry.get("commit") or None)
        app._load_commands()
        broken = f" — ✘ {info['broken']}" if info.get("broken") else ""
        await app.say_notice(f"installed {info.get('name') or name}{broken}")
        await menu(app, at=str(info.get("name") or name))

    await _show(app, Picker(about, [Option("install", "Yes, install it"), Option("", "No")],
                              picked))
