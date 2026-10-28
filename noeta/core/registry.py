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
from collections.abc import Iterable, Mapping
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
    "note_arguments",
    "pin",
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
#: Where a plugin class came from: class -> (source, version).
_origins: dict[Any, tuple[str, str]] = {}
#: The hash of the arguments each plugin instance was made with.
_arguments: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()


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
        obj = ep.load()
        if group == PLUGINS and isinstance(obj, type) and ep.dist is not None:
            _origins.setdefault(obj, (f"package {ep.dist.name}", ep.dist.version))
        return obj
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
                version = _file_version(path)
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
                _origins[cls] = ("file", version)
                added.append(name)
    return added


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
