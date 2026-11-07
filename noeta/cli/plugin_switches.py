"""The machine's plugins as front ends manage them: which plugins new tasks
start without (``~/.noeta/plugin-switches.json``, ``{"off": [...]}``), and
installing and removing over HTTP as ``noeta plugin add`` / ``remove`` do
(:class:`noeta.host.plugins.PluginAdmin`, which the ``noeta`` command sets
on its app)."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Collection, Sequence
from pathlib import Path
from typing import Any, cast

from noeta.cli import home_dir, plugin_command
from noeta.host.plugins import MarketEntry, PluginAdmin, PluginInfo

__all__ = ["SWITCHES_FILE", "default_off", "make_admin", "set_default"]

SWITCHES_FILE = "plugin-switches.json"


def _path() -> Path:
    return home_dir.HOME / SWITCHES_FILE


def default_off() -> list[str]:
    """The plugins new tasks start without; none when the file is missing
    or unreadable."""
    try:
        data = json.loads(_path().read_text("utf-8"))
    except (OSError, ValueError):
        return []
    off = data.get("off") if isinstance(data, dict) else None
    return sorted({n for n in off if isinstance(n, str) and n}) \
        if isinstance(off, list) else []


def set_default(name: str, enabled: bool) -> None:
    """Switch ``name`` on or off for new tasks (written whole, atomically)."""
    off = set(default_off())
    if enabled:
        off.discard(name)
    else:
        off.add(name)
    home_dir.ensure_home()
    fd, tmp = tempfile.mkstemp(dir=home_dir.HOME, prefix=".plugin-switches-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"off": sorted(off)}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, _path())


def _install(source: str, marketplace: str | None, commit: str | None) -> str:
    """``noeta plugin add`` without a terminal: a plugin whose options must
    be asked for is refused (and left uninstalled)."""
    try:
        name, path = plugin_command.install(source, marketplace=marketplace, commit=commit)
    except (plugin_command.PluginInstallError, OSError) as exc:
        raise ValueError(str(exc)) from exc
    fields = getattr(plugin_command, "_fields", None)
    if path.is_dir() and fields is not None and fields(path):
        _remove(name)
        raise ValueError(f"plugin {name!r} has options to set: install it from a terminal "
                         f"(noeta plugin add)")
    return name


def _remove(name: str) -> None:
    plugins = home_dir.HOME / "plugins"
    known = name in plugin_command._records() or (plugins / name).exists() \
        or (plugins / f"{name}.py").exists()
    if not known:
        raise ValueError(f"no plugin {name} in {plugins}")
    plugin_command._remove(name, quiet=True)


def _market() -> list[MarketEntry]:
    return [cast(MarketEntry, e) for e in plugin_command.market_entries()]


def _claude_info(plugin: Any) -> PluginInfo:
    parts = [part for part, has in (
        ("skills", plugin.skill_dirs), ("agents", plugin.agents),
        ("commands", plugin.commands), ("hooks", plugin.hooks),
        ("mcp", plugin.mcp_servers)) if has]
    return PluginInfo(name=plugin.name, version=plugin.version, description="",
                      source=str(plugin.root), builtin=False, enabled=True, switchable=False,
                      broken=None, parts=parts, ui_api=None, web=None)


def make_admin(cwd: str | None, plugin_dirs: Sequence[str | Path],
               turned_off: Callable[[], Collection[str]],
               fixed: Callable[[], Collection[str]] = lambda: ()) -> PluginAdmin:
    """The admin of the plugins a ``noeta`` process started in ``cwd``
    loads; ``turned_off()``: the plugins its settings switch off;
    ``fixed()``: the ones they keep in every task (``switchable = false``)."""
    from noeta.cli.config import claude_plugins

    def others(where: str) -> list[PluginInfo]:
        found, _notices = claude_plugins(where or cwd, plugin_dirs)
        return [_claude_info(p) for p in found]

    return PluginAdmin(default_off=default_off, set_default=set_default,
                       turned_off=turned_off, fixed=fixed, others=others, install=_install,
                       remove=_remove, market=_market)
