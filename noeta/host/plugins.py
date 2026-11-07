"""A host's plugins as front ends show and switch them: the listing
(:func:`plugin_infos`), each plugin's manifest (``.noeta-plugin/plugin.json``
beside its code) and web files, a task's UI items (:func:`task_ui`) and calls
from a plugin's front-end code (:func:`call_plugin`).

What lives on the machine (the default switches for new tasks, installing,
removing, the marketplaces) is the ``noeta`` command's: it hands those over
as a :class:`PluginAdmin`.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from noeta.core import registry
from noeta.core.agent import Agent
from noeta.core.extension import (
    CommandContext,
    Extension,
    Notice,
    StatusItem,
    TaskView,
    UiItem,
    UnknownUiCall,
    Widget,
    task_cwd,
)
from noeta.core.loop.state import fold
from noeta.core.runner import Runtime, TaskRun
from noeta.core.store import Claim, Store

__all__ = ["MANIFEST", "PARTS", "SWITCHABLE_BUILTINS", "UI_API", "MarketEntry", "PluginAdmin",
           "PluginInfo", "UiEntry", "call_plugin", "command_off", "plugin_infos", "read_manifest",
           "switchable", "task_off", "task_ui", "ui_entry", "web_dir"]

log = logging.getLogger("noeta.host.plugins")

#: The ``ui_api`` numbers this host runs a plugin's front-end parts for.
UI_API = (1, 1)
#: The built-in plugins a task runs well without: the only built-ins front
#: ends offer a switch for (every installed plugin has one).
SWITCHABLE_BUILTINS = ("web", "workflow", "schedule", "todos", "findings", "title",
                       "quiet_nudge", "memory", "mcp", "checkpoints", "skills", "ask_user")
#: A plugin's manifest, relative to the directory its code is in.
MANIFEST = Path(".noeta-plugin") / "plugin.json"
#: What a plugin may carry.
PARTS = ("backend", "ui", "web", "skills", "agents", "commands", "hooks", "mcp")
_SHIPPED = "package noeta"


class PluginInfo(TypedDict):
    name: str
    version: str
    description: str
    source: str
    builtin: bool
    #: In the agent and on by the machine's default for new tasks.
    enabled: bool
    #: Front ends may switch it on and off (per task, and for new tasks).
    switchable: bool
    #: Why it, or its web part, could not be used; ``None`` when nothing is wrong.
    broken: str | None
    parts: list[str]
    ui_api: int | None
    #: ``{"entry": "/plugins/<name>/web/<file>?v=<hash>"}``, or ``None``.
    web: dict[str, str] | None


class UiEntry(TypedDict, total=False):
    plugin: str
    type: str
    key: str
    text: str
    markdown: str
    tone: str
    placement: str
    level: str


class MarketEntry(TypedDict):
    marketplace: str
    name: str
    description: str
    version: str
    source: str
    commit: str
    parts: list[str]
    ui_api: int | None
    installed: bool


@dataclass
class PluginAdmin:
    """What the ``noeta`` command lets front ends do with the machine's
    plugins. Each callable may block (files, git): call it off the loop."""

    #: The plugins new tasks start switched off (``plugins_off``).
    default_off: Callable[[], list[str]] = lambda: []
    #: Switch a plugin on or off for new tasks.
    set_default: Callable[[str, bool], None] | None = None
    #: The plugins the settings turn off for good (``enabled = false``).
    turned_off: Callable[[], Collection[str]] = lambda: ()
    #: The plugins the settings keep in every task (``switchable = false``):
    #: no task may run without them, whatever a front end asks.
    fixed: Callable[[], Collection[str]] = lambda: ()
    #: Plugins that are no ``Extension`` (Claude Code plugins) found for a
    #: directory, already as listed.
    others: Callable[[str], list[PluginInfo]] = lambda _cwd: []
    #: Install ``(source, marketplace, commit)``; returns the plugin's name.
    install: Callable[[str, str | None, str | None], str] | None = None
    #: Remove an installed plugin by name.
    remove: Callable[[str], None] | None = None
    #: The configured marketplaces' entries.
    market: Callable[[], list[MarketEntry]] | None = None


def switchable(name: str, fixed: Collection[str] = ()) -> bool:
    """Whether a task may run without the plugin ``name``: every plugin but
    the built-ins outside :data:`SWITCHABLE_BUILTINS` (the permission gate,
    the file and shell tools a task is built on) and the ones in ``fixed``
    (:attr:`PluginAdmin.fixed`)."""
    if name in fixed:
        return False
    return name in SWITCHABLE_BUILTINS \
        or registry.sources(registry.PLUGINS).get(name) != _SHIPPED


def read_manifest(root: Path | None) -> dict[str, Any] | None:
    """The manifest in ``root`` (:data:`MANIFEST`) as a JSON object; ``None``
    when there is none or it is unreadable (logged)."""
    if root is None:
        return None
    path = root / MANIFEST
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("plugin manifest %s is unreadable: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _web_entry(root: Path, manifest: Mapping[str, Any]) -> Path | None:
    """The built ES module the manifest's ``web`` names, inside ``root``."""
    web = manifest.get("web")
    if not isinstance(web, str) or not web:
        return None
    entry = (root / web).resolve()
    if not entry.is_relative_to(root.resolve()) or not entry.is_file():
        return None
    return entry


def web_dir(name: str) -> Path | None:
    """The directory the plugin ``name``'s web files are served from: the
    one its manifest's ``web`` module is in, when the plugin's ``ui_api``
    is one this host runs."""
    root = registry.plugin_root(name)
    manifest = read_manifest(root)
    if root is None or manifest is None or not _ui_api_ok(manifest.get("ui_api")):
        return None
    entry = _web_entry(root, manifest)
    return entry.parent if entry is not None else None


def _ui_api_ok(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) \
        and UI_API[0] <= value <= UI_API[1]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _description(cls: type) -> str:
    doc = inspect.getdoc(cls) if cls.__doc__ else None
    return doc.strip().splitlines()[0] if doc else ""


def _overrides(cls: type, method: str) -> bool:
    return getattr(cls, method, None) is not getattr(Extension, method)


def _builtin(ext: Extension, source: str) -> bool:
    return source == _SHIPPED or type(ext).__module__.split(".")[0] == "noeta"


def _info(ext: Extension, *, off: Collection[str], fixed: Collection[str]) -> PluginInfo:
    pin = registry.pin(ext)
    source = registry.sources(registry.PLUGINS).get(ext.name) or pin["source"]
    builtin = _builtin(ext, source)
    root = registry.plugin_root(ext.name)
    manifest = read_manifest(root) or {}
    parts = ["backend"]
    if _overrides(type(ext), "ui") or _overrides(type(ext), "ui_call"):
        parts.append("ui")
    broken: str | None = None
    web: dict[str, str] | None = None
    entry = _web_entry(root, manifest) if root is not None else None
    ui_api = manifest.get("ui_api") if isinstance(manifest.get("ui_api"), int) else None
    if entry is not None:
        parts.append("web")
        if _ui_api_ok(manifest.get("ui_api")):
            web = {"entry": f"/plugins/{ext.name}/web/{entry.name}?v={_digest(entry)}"}
        else:
            broken = (f"its web part needs ui_api {manifest.get('ui_api')!r}; this noeta runs "
                      f"{UI_API[0]} to {UI_API[1]}")
    elif manifest.get("web"):
        broken = f"its manifest's web file {manifest['web']!r} is missing"
    if root is not None and not builtin:
        parts += [p for p in ("skills", "agents", "commands") if (root / p).is_dir()]
    can_switch = (not builtin or ext.name in SWITCHABLE_BUILTINS) and ext.name not in fixed
    return PluginInfo(
        name=ext.name, version=str(manifest.get("version") or pin["version"]),
        description=str(manifest.get("description") or _description(type(ext))),
        source=source, builtin=builtin, enabled=not (can_switch and ext.name in off),
        switchable=can_switch, broken=broken, parts=parts, ui_api=ui_api, web=web)


def _absent(name: str, source: str, broken: str | None) -> PluginInfo:
    """A plugin in no agent here: switched off by the settings, joining no
    agent, or failing to load."""
    manifest = read_manifest(registry.plugin_root(name)) or {}
    return PluginInfo(name=name, version=str(manifest.get("version") or ""),
                      description=str(manifest.get("description") or ""), source=source,
                      builtin=False, enabled=False, switchable=False, broken=broken,
                      parts=["backend"], ui_api=None, web=None)


def plugin_infos(agent: Agent | None, admin: PluginAdmin, cwd: str = "") -> list[PluginInfo]:
    """Every plugin ``agent`` runs, in its order, then the installed ones it
    does not (each saying why), the plugin files that failed to load and
    ``admin``'s others for ``cwd``."""
    off, fixed = set(admin.default_off()), set(admin.fixed())
    out = [_info(e, off=off, fixed=fixed)
           for e in (agent.all_extensions() if agent is not None else [])]
    seen = {p["name"] for p in out}
    hard = set(admin.turned_off())
    for name, source in registry.sources(registry.PLUGINS).items():
        if name in seen or source == _SHIPPED:
            continue
        seen.add(name)
        out.append(_absent(name, source, "switched off in the settings" if name in hard
                           else "joins no agent"))
    for path, why in registry.broken_plugin_files().items():
        name = Path(path).stem
        if name not in seen:
            seen.add(name)
            out.append(_absent(name, path, why))
    for other in admin.others(cwd):
        if other["name"] not in seen:
            seen.add(other["name"])
            out.append(other)
    return out


def ui_entry(plugin: str, item: UiItem) -> UiEntry:
    """``item`` as front ends get it over the wire."""
    if isinstance(item, StatusItem):
        return UiEntry(plugin=plugin, type="status", key=item.key, text=item.text,
                       tone=item.tone)
    if isinstance(item, Widget):
        return UiEntry(plugin=plugin, type="widget", key=item.key, markdown=item.markdown,
                       placement=item.placement)
    if isinstance(item, Notice):
        return UiEntry(plugin=plugin, type="notice", key=item.key, text=item.text,
                       level=item.level)
    raise TypeError(f"plugin {plugin}: not a UI item: {item!r}")


async def _loaded(runtime: Runtime, task_id: str) -> TaskRun | None:
    """The task folded as a worker would claim it (nothing is written);
    ``None`` when ``runtime`` lacks its agent."""
    run = TaskRun(runtime, Claim(task_id=task_id, worker=runtime.worker, epoch=0,
                                 lease_until=0.0))
    await run.load()
    return run if run.agent is not None else None


def _view(run: TaskRun) -> TaskView:
    """A view of the folded task without its exec env or model calls: what
    a front end's question may read, never start."""
    agent = run.agent
    return TaskView(state=run.state, extensions=run.exts, model=run.model,
                    agents=run.rt.agents, identity=run.identity, effort=run.effort,
                    allowed_tools=agent.allowed_tools if agent else None,
                    denied_tools=agent.denied_tools if agent else ())


async def task_off(store: Store, task_id: str) -> list[str]:
    """The plugins the task runs without now (``TaskState.plugins_off``),
    from the kernel's fold of its log alone."""
    return fold(await store.read(task_id), {}).plugins_off


async def command_off(store: Store, ctx: CommandContext) -> list[str]:
    """The plugins whose commands ``ctx``'s task does not offer: the ones it
    runs without now (none before it has a task)."""
    if not ctx.task_id or await store.get(ctx.task_id) is None:
        return []
    return await task_off(store, ctx.task_id)


async def task_ui(runtime: Runtime, task_id: str) -> list[UiEntry]:
    """Every UI item the task's plugins (the ones it runs with now) show,
    in the agent's order. A plugin that fails is logged and left out."""
    run = await _loaded(runtime, task_id)
    if run is None:
        return []
    view = _view(run)
    out: list[UiEntry] = []
    for ext in run.exts:
        try:
            out += [ui_entry(ext.name, item) for item in await ext.ui(view)]
        except Exception:
            log.exception("plugin %s: ui failed", ext.name)
    return out


async def call_plugin(runtime: Runtime, task_id: str, plugin: str, name: str,
                      args: Any) -> Any:
    """``plugin``'s answer to its front-end code's call ``name``
    (``Extension.ui_call``) for the task. Raises :class:`UnknownUiCall`
    when the task does not run that plugin now, or the plugin has no such
    call."""
    run = await _loaded(runtime, task_id)
    ext = next((e for e in run.exts if e.name == plugin), None) if run is not None else None
    if run is None or ext is None:
        raise UnknownUiCall(f"task {task_id} runs no plugin {plugin!r}")
    ctx = CommandContext(cwd=task_cwd(run.state, None), task_id=task_id)
    return await ext.ui_call(ctx, name, args)
