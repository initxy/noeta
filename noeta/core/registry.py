"""Plugins and backends by name.

Everything that is not the kernel is found through Python entry points, the
built-ins exactly like a third-party package's (see ``pyproject.toml``):

* ``noeta.plugins`` — a name -> an ``Extension`` class (or factory);
* ``noeta.providers`` — a provider family -> ``factory(**kw) -> Provider``;
* ``noeta.execenvs`` — a name -> ``factory(cwd, **kw) -> ExecEnv``;
* ``noeta.stores`` — a URL scheme -> ``factory(url) -> Store``;
* ``noeta.channels`` — a name -> a ``Channel`` class (``noeta.core.channel``);
* ``noeta.frontends`` — a name -> ``run(app, start)``, a front end the
  ``noeta`` command opens (``noeta.host.app.FrontEndStart``; ``tui``: noeta-tui).

The kernel imports no plugin or backend module statically; it loads one by
name here when it needs a default (the local exec env).

Plugins can also be plain files (:func:`load_plugin_files`): every
``Extension`` (or ``Channel``) subclass a ``*.py`` file (or a directory's
``__init__.py``) defines registers under its ``name``. An installed plugin
wins a name clash.

:func:`pin` says which plugin an extension instance is (name, where its
code comes from and which version, a hash of its arguments): what a task
records when it starts, so a worker that cannot run the same plugins never
runs the task without them.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import logging
import sys
import types
import weakref
from collections.abc import Callable, Iterable, Mapping
from functools import cache
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from noeta.core.execenv import ExecEnv
    from noeta.core.provider import Provider
    from noeta.core.store import Store

__all__ = [
    "CHANNELS",
    "EXECENVS",
    "FRONTENDS",
    "PLUGINS",
    "PROVIDERS",
    "STORES",
    "UnknownName",
    "added_by_files",
    "available",
    "broken_plugin_files",
    "execenv",
    "forget_plugin_files",
    "load",
    "load_plugin_files",
    "missing_modules",
    "note_arguments",
    "pin",
    "plugin_files",
    "plugin_root",
    "provider",
    "sources",
    "store",
]

log = logging.getLogger("noeta.registry")

PLUGINS = "noeta.plugins"
PROVIDERS = "noeta.providers"
EXECENVS = "noeta.execenvs"
STORES = "noeta.stores"
CHANNELS = "noeta.channels"
FRONTENDS = "noeta.frontends"


class UnknownName(LookupError):
    """No entry point of that name in the group."""


@cache
def _group(group: str) -> dict[str, EntryPoint]:
    return {ep.name: ep for ep in entry_points(group=group)}


#: Plugins from files: name -> (path, the Extension class).
_files: dict[str, tuple[str, Any]] = {}
#: Channels from files: name -> (path, the Channel class).
_channel_files: dict[str, tuple[str, Any]] = {}
#: Plugin files that could not be used: path -> why.
_broken: dict[str, str] = {}
#: Of those, the ones that import a module nobody installed: path -> module.
_missing: dict[str, str] = {}
_loaded_paths: set[str] = set()
_FILES_PACKAGE = "noeta_plugin_files"
#: Where a plugin class came from: class -> (source, version).
_origins: weakref.WeakKeyDictionary[Any, tuple[str, str]] = weakref.WeakKeyDictionary()
#: The hash of the arguments each plugin instance was made with.
_arguments: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()


def _from_files(group: str) -> dict[str, tuple[str, Any]]:
    """What plugin files registered in ``group``: name -> (path, class)."""
    return {PLUGINS: _files, CHANNELS: _channel_files}.get(group, {})


def available(group: str) -> list[str]:
    """The names registered in ``group``, sorted."""
    return sorted(set(_group(group)) | set(_from_files(group)))


@cache
def _source(ep: EntryPoint) -> str:
    # A distribution's name is read from its metadata file on every access.
    return f"package {ep.dist.name}" if ep.dist is not None else f"entry point {ep.value}"


def sources(group: str) -> dict[str, str]:
    """Where each name of ``group`` comes from: the installed distribution
    (``package <name>``) or the plugin file's path."""
    out = {name: _source(ep) for name, ep in _group(group).items()}
    out.update({name: path for name, (path, _cls) in _from_files(group).items()})
    return dict(sorted(out.items()))


def broken_plugin_files() -> dict[str, str]:
    """Plugin files that could not be used: path -> why."""
    return dict(_broken)


def missing_modules() -> dict[str, str]:
    """Broken plugin files that import a module not installed here: path ->
    its top-level name. Not the plugin's own modules, nor noeta's."""
    return dict(_missing)


def added_by_files() -> dict[str, list[str]]:
    """Per agent name (``"*"``: every agent), the plugin files that join it
    by themselves (``Extension.add_to``)."""
    out: dict[str, list[str]] = {}
    for name, (_path, cls) in sorted(_files.items()):
        for agent in getattr(cls, "add_to", ()) or ():
            out.setdefault(str(agent), []).append(name)
    return out


def plugin_files() -> dict[str, str]:
    """The plugins plugin files registered: name -> path."""
    return {name: path for name, (path, _cls) in sorted(_files.items())}


def plugin_root(name: str) -> Path | None:
    """The directory the plugin ``name``'s code is in: a plugin directory,
    or the package directory holding an installed plugin's entry-point
    module (found without importing that module). ``None`` for a
    single-file plugin, a top-level module, or an unknown name."""
    found = _files.get(name)
    if found is not None:
        path = Path(found[0])
        return path if path.is_dir() else None
    ep = _group(PLUGINS).get(name)
    if ep is None:
        return None
    try:
        spec = importlib.util.find_spec(ep.module)
    except (ImportError, ValueError):
        return None
    origin = spec.origin if spec is not None else None
    if not origin or (Path(origin).name != "__init__.py" and "." not in ep.module):
        return None
    return Path(origin).parent


def load(group: str, name: str) -> Any:
    """The object the entry point ``name`` in ``group`` points at (for
    plugins, else the class a plugin file defines)."""
    ep = _group(group).get(name)
    if ep is not None:
        obj = ep.load()
        if group == PLUGINS and isinstance(obj, type) and ep.dist is not None:
            _origins.setdefault(obj, (f"package {ep.dist.name}", ep.dist.version))
        return obj
    if name in _from_files(group):
        return _from_files(group)[name][1]
    known = ", ".join(available(group)) or "none"
    msg = f"no {group} entry point named {name!r} (available: {known})"
    if group in (PLUGINS, CHANNELS) and _broken:
        msg += "; plugin files that failed: " + "; ".join(
            f"{path}: {why}" for path, why in _broken.items())
    raise UnknownName(msg)


def _plugin_paths(directory: Path, *, given: bool = False) -> list[Path]:
    """``*.py`` files and directories with ``__init__.py``, one level down;
    with ``given`` (a ``--plugin-dir``), ``directory`` itself when it is one
    of those."""
    if given and directory.is_file():
        return [directory] if directory.suffix == ".py" else []
    if given and (directory / "__init__.py").is_file():
        return [directory / "__init__.py"]
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
    # Compiled from source every time, never from a cached .pyc: a file
    # edited within the same second at the same size would otherwise load
    # its old code on a reload.
    saved = sys.pycache_prefix, sys.dont_write_bytecode
    sys.pycache_prefix = str(path.parent / "__noeta_no_cache__")
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    finally:
        sys.pycache_prefix, sys.dont_write_bytecode = saved
    return module


def load_plugin_files(directories: Iterable[str | Path],
                      admit: Callable[[str], bool] | None = None, *,
                      given: Iterable[str | Path] = ()) -> list[str]:
    """Register the plugins (and channels) of the files in ``directories``
    (each loaded once per process), then of ``given`` (each one plugin
    file, one plugin directory, or a directory of them); returns the names
    added. A file that
    fails to import is remembered and named when its plugin is asked for; a
    name an installed plugin already has is skipped with a warning.
    ``admit(name)``: whether a file (by its stem, a directory by its name)
    may be imported at all."""
    from noeta.core.channel import Channel
    from noeta.core.extension import Extension

    added: list[str] = []
    places = [(Path(d).expanduser(), False) for d in directories]
    places += [(Path(d).expanduser(), True) for d in given]
    for directory, one in places:
        for path in _plugin_paths(directory, given=one):
            key = str(path.resolve())
            stem = path.parent.name if path.name == "__init__.py" else path.stem
            if key in _loaded_paths or (admit is not None and not admit(stem)):
                continue
            _loaded_paths.add(key)
            shown = str(path.parent if path.name == "__init__.py" else path)
            try:
                version = _file_version(path)
                module = _import(path)
            except Exception as exc:  # noqa: BLE001 — a user's file must not stop noeta
                _broken[shown] = f"{type(exc).__name__}: {exc}"
                top = (exc.name or "").split(".", 1)[0] if isinstance(
                    exc, ModuleNotFoundError) else ""
                if top and top not in (_FILES_PACKAGE, "noeta"):
                    _missing[shown] = top
                log.warning("plugin file %s failed to import: %s", shown, exc)
                continue
            found = [(PLUGINS if issubclass(cls, Extension) else CHANNELS, cls)
                     for _n, cls in inspect.getmembers(module, inspect.isclass)
                     if issubclass(cls, (Extension, Channel)) and cls not in (Extension, Channel)
                     and cls.__module__ == module.__name__ and getattr(cls, "name", None)]
            if not found:
                _broken[shown] = "defines no Extension or Channel subclass with a name"
                continue
            for group, cls in found:
                name, table = str(cls.name), _from_files(group)
                if name in _group(group) or name in table:
                    where = sources(group).get(name, "")
                    log.warning("plugin file %s: %r is already %s; skipped", shown, name, where)
                    _broken[shown] = f"{'plugin' if group == PLUGINS else 'channel'} " \
                        f"{name!r} is already {where}"
                    continue
                table[name] = (shown, cls)
                _origins[cls] = ("file", version)
                added.append(name)
    return added


def forget_plugin_files() -> None:
    """Forget every plugin file loaded (and its modules), so the next
    :func:`load_plugin_files` imports them afresh: a reload. Extensions
    already made from the old code keep running it."""
    _files.clear()
    _channel_files.clear()
    _broken.clear()
    _missing.clear()
    _loaded_paths.clear()
    for module in [m for m in sys.modules if m.startswith(f"{_FILES_PACKAGE}.")]:
        del sys.modules[module]


def _file_version(path: Path) -> str:
    """A plugin file's version: the hash of its bytes (a directory's: of
    every ``*.py`` below it, by relative path)."""
    digest = hashlib.sha256()
    if path.name == "__init__.py":
        root = path.parent
        for file in sorted(root.rglob("*.py")):
            if "__pycache__" not in file.parts:
                digest.update(str(file.relative_to(root)).encode() + b"\0" + file.read_bytes())
    else:
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()[:16]}"


def note_arguments(extension: Any, arguments: Mapping[str, Any]) -> None:
    """Remember the arguments ``extension`` was made with (as written, so a
    secret reference stays a reference), for :func:`pin`."""
    text = json.dumps(arguments, sort_keys=True, default=_stable_text)
    _arguments[extension] = hashlib.sha256(text.encode()).hexdigest()[:16]


def _stable_text(value: Any) -> str:
    """A value JSON cannot show, as text that is the same in every process
    (a function's or an object's type name, never its address)."""
    if isinstance(value, Path):
        return str(value)
    named = value if callable(value) and hasattr(value, "__qualname__") else type(value)
    return f"{getattr(named, '__module__', '')}.{named.__qualname__}"


def pin(extension: Any) -> dict[str, str]:
    """Which plugin ``extension`` is: its name, where its code comes from
    (``package <dist>``, ``file``, else ``class <module.qualname>``), that
    code's version (the distribution's, or a file's hash) and the hash of
    its arguments (empty when they are unknown)."""
    cls = type(extension)
    source, version = _origins.get(cls, (f"class {cls.__module__}.{cls.__qualname__}", ""))
    try:
        arguments = _arguments.get(extension, "")
    except TypeError:  # not weakly referable
        arguments = ""
    return {"name": str(getattr(extension, "name", "")), "source": source,
            "version": version, "arguments": arguments}


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
