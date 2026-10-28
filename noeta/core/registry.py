"""Plugins and backends by name.

Everything that is not the kernel is found through Python entry points, the
built-ins exactly like a third-party package's (see ``pyproject.toml``):

* ``noeta.plugins`` — a name -> an ``Extension`` class (or factory);
* ``noeta.providers`` — a provider family -> ``factory(**kw) -> Provider``;
* ``noeta.execenvs`` — a name -> ``factory(cwd, **kw) -> ExecEnv``;
* ``noeta.stores`` — a URL scheme -> ``factory(url) -> Store``.

The kernel imports no plugin or backend module statically; it loads one by
name here when it needs a default (the local exec env).

Plugins can also be plain files (:func:`load_plugin_files`): every
``Extension`` subclass a ``*.py`` file (or a directory's ``__init__.py``)
defines registers under its ``name``. An installed plugin wins a name clash.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import logging
import sys
import types
from collections.abc import Iterable
from functools import cache
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from noeta.core.execenv import ExecEnv
    from noeta.core.provider import Provider
    from noeta.core.store import Store

__all__ = [
    "EXECENVS",
    "PLUGINS",
    "PROVIDERS",
    "STORES",
    "UnknownName",
    "available",
    "broken_plugin_files",
    "execenv",
    "load",
    "load_plugin_files",
    "provider",
    "sources",
    "store",
]

log = logging.getLogger("noeta.registry")

PLUGINS = "noeta.plugins"
PROVIDERS = "noeta.providers"
EXECENVS = "noeta.execenvs"
STORES = "noeta.stores"


class UnknownName(LookupError):
    """No entry point of that name in the group."""


@cache
def _group(group: str) -> dict[str, EntryPoint]:
    return {ep.name: ep for ep in entry_points(group=group)}


#: Plugins from files: name -> (path, the Extension class).
_files: dict[str, tuple[str, Any]] = {}
#: Plugin files that could not be used: path -> why.
_broken: dict[str, str] = {}
_loaded_paths: set[str] = set()
_FILES_PACKAGE = "noeta_plugin_files"


def available(group: str) -> list[str]:
    """The names registered in ``group``, sorted."""
    names = set(_group(group))
    if group == PLUGINS:
        names |= set(_files)
    return sorted(names)


def sources(group: str) -> dict[str, str]:
    """Where each name of ``group`` comes from: the installed distribution
    (``package <name>``) or the plugin file's path."""
    out = {name: f"package {ep.dist.name}" if ep.dist is not None else f"entry point {ep.value}"
           for name, ep in _group(group).items()}
    if group == PLUGINS:
        out.update({name: path for name, (path, _cls) in _files.items()})
    return dict(sorted(out.items()))


def broken_plugin_files() -> dict[str, str]:
    """Plugin files that could not be used: path -> why."""
    return dict(_broken)


def load(group: str, name: str) -> Any:
    """The object the entry point ``name`` in ``group`` points at (for
    plugins, else the class a plugin file defines)."""
    ep = _group(group).get(name)
    if ep is not None:
        return ep.load()
    if group == PLUGINS and name in _files:
        return _files[name][1]
    known = ", ".join(available(group)) or "none"
    msg = f"no {group} entry point named {name!r} (available: {known})"
    if group == PLUGINS and _broken:
        msg += "; plugin files that failed: " + "; ".join(
            f"{path}: {why}" for path, why in _broken.items())
    raise UnknownName(msg)


def _plugin_paths(directory: Path) -> list[Path]:
    """``*.py`` files and directories with ``__init__.py``, one level down."""
    if not directory.is_dir():
        return []
    out = []
    for entry in sorted(directory.iterdir()):
        if entry.name.startswith((".", "_")):
            continue
        if entry.is_file() and entry.suffix == ".py":
            out.append(entry)
        elif entry.is_dir() and (entry / "__init__.py").is_file():
            out.append(entry / "__init__.py")
    return out


def _import(path: Path) -> Any:
    digest = hashlib.sha1(str(path).encode()).hexdigest()[:8]
    stem = path.parent.name if path.name == "__init__.py" else path.stem
    module_name = f"{_FILES_PACKAGE}.{stem}_{digest}"
    search = [str(path.parent)] if path.name == "__init__.py" else None
    spec = importlib.util.spec_from_file_location(module_name, path,
                                                  submodule_search_locations=search)
    if spec is None or spec.loader is None:
        raise ImportError("not an importable Python file")
    if _FILES_PACKAGE not in sys.modules:  # the parent a directory's relative imports need
        parent = types.ModuleType(_FILES_PACKAGE)
        parent.__path__ = []
        sys.modules[_FILES_PACKAGE] = parent
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def load_plugin_files(directories: Iterable[str | Path]) -> list[str]:
    """Register the plugins of the files in ``directories`` (each loaded
    once per process); returns the names added. A file that fails to import
    is remembered and named when its plugin is asked for; a name an
    installed plugin already has is skipped with a warning."""
    from noeta.core.extension import Extension

    added: list[str] = []
    for directory in directories:
        for path in _plugin_paths(Path(directory).expanduser()):
            key = str(path.resolve())
            if key in _loaded_paths:
                continue
            _loaded_paths.add(key)
            shown = str(path.parent if path.name == "__init__.py" else path)
            try:
                module = _import(path)
            except Exception as exc:  # noqa: BLE001 — a user's file must not stop noeta
                _broken[shown] = f"{type(exc).__name__}: {exc}"
                log.warning("plugin file %s failed to import: %s", shown, exc)
                continue
            found = [cls for _n, cls in inspect.getmembers(module, inspect.isclass)
                     if issubclass(cls, Extension) and cls is not Extension
                     and cls.__module__ == module.__name__ and getattr(cls, "name", None)]
            if not found:
                _broken[shown] = "defines no Extension subclass with a name"
                continue
            for cls in found:
                name = str(cls.name)
                if name in _group(PLUGINS) or name in _files:
                    where = sources(PLUGINS).get(name, "")
                    log.warning("plugin file %s: %r is already %s; skipped", shown, name, where)
                    _broken[shown] = f"plugin {name!r} is already {where}"
                    continue
                _files[name] = (shown, cls)
                added.append(name)
    return added


def provider(family: str, **kw: Any) -> Provider:
    """The provider adapter for ``family``; keywords go to its factory."""
    result: Provider = load(PROVIDERS, family)(**kw)
    return result


def execenv(name: str, cwd: str, **kw: Any) -> ExecEnv:
    result: ExecEnv = load(EXECENVS, name)(cwd, **kw)
    return result


def store(url: str) -> Store:
    """The store for ``url``, chosen by its scheme (``sqlite:///path``,
    ``postgres://...``)."""
    scheme = url.split(":", 1)[0] if ":" in url else ""
    try:
        factory = load(STORES, scheme)
    except UnknownName:
        raise ValueError(f"unknown store url {url!r} (sqlite:///path or postgres://...)") \
            from None
    result: Store = factory(url)
    return result
