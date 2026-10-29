"""Fold snapshots: a task's folded state as of one log seq, so a claim reads
the snapshot and folds only the events after it instead of the whole log.

A snapshot is only ever a fold of the log up to its seq, which never
changes, so any runner may write one and a newer one simply replaces an
older one. It is used only when its version matches: the fold code (the
kernel's and each of the agent's extensions', by source, with every module
of the same top-level package they import, so a reducer kept in a helper
module counts) and the agent's plugins (``registry.pin``). Anything else
folds the log from the start. Source is hashed once per process (:func:`pin`),
so a process keeps the version of the code it started with.

A state is only written when it reads back equal (:func:`encode`): an
extension state the encoding cannot carry faithfully (an object without
equality, a type no longer importable by name) means no snapshot for that
task, never a wrong one.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import hashlib
import importlib.util
import sys
from collections.abc import Sequence
from typing import Any

import msgspec

from noeta.core import registry
from noeta.core.loop.state import TaskState
from noeta.core.types import Message

#: Bumped when the encoding below changes.
FORMAT = 1

#: The modules whose code decides what a log folds to.
FOLD_MODULES = ("noeta.core.loop.state", "noeta.core.records", "noeta.core.events",
                "noeta.core.types")


def _source_path(module: str) -> str | None:
    """``module``'s ``.py`` file, found without importing it (its parent
    packages may be imported); ``None`` when it has none or is unknown."""
    mod = sys.modules.get(module)
    path = getattr(mod, "__file__", None) if mod is not None else None
    if path is None:
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ValueError):
            return None
        path = spec.origin if spec is not None else None
    return path if isinstance(path, str) and path.endswith(".py") else None


@functools.cache
def _module_source(module: str) -> bytes | None:
    path = _source_path(module)
    if path is None:
        return None
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


@functools.cache
def _module_digest(module: str) -> str:
    """The hash of ``module``'s source file (its name when it has none)."""
    source = _module_source(module)
    return hashlib.sha256(source).hexdigest()[:16] if source is not None else module


def _imported(module: str) -> set[str]:
    """The modules ``module``'s source imports (anywhere in it, also under
    ``TYPE_CHECKING`` or inside a function), by absolute name; a ``from m
    import n`` counts ``m.n`` when that is a module."""
    source = _module_source(module)
    if source is None:
        return set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    is_package = (_source_path(module) or "").endswith("__init__.py")
    package = module if is_package else module.rpartition(".")[0]
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            try:
                base = importlib.util.resolve_name("." * node.level + (node.module or ""),
                                                   package) if node.level else node.module
            except (ImportError, ValueError):
                continue
            if not base:
                continue
            out.add(base)
            out.update(f"{base}.{alias.name}" for alias in node.names
                       if _source_path(f"{base}.{alias.name}") is not None)
    return out


@functools.cache
def _code_digest(module: str) -> str:
    """The hash of ``module`` and every module of its top-level package it
    imports, directly or not: the code a fold reaches from it."""
    top = module.partition(".")[0]
    seen, todo = {module}, [module]
    while todo:
        for name in _imported(todo.pop()):
            if name.partition(".")[0] == top and name not in seen \
                    and _source_path(name) is not None:
                seen.add(name)
                todo.append(name)
    h = hashlib.sha256()
    for name in sorted(seen):
        h.update(f"{name}:{_module_digest(name)}\n".encode())
    return h.hexdigest()[:16]


def pin(extensions: Sequence[Any]) -> None:
    """Hash the fold code of ``extensions`` (and the kernel's) now: the hashes
    are kept for the process, so code upgraded on disk later does not change
    the version this process stamps on its snapshots (``Runtime`` pins its
    agents when it is made; an agent first seen later is hashed then)."""
    for module in FOLD_MODULES:
        _code_digest(module)
    for ext in extensions:
        for cls in type(ext).__mro__[:-1]:
            _code_digest(cls.__module__)


def version(agent: str, extensions: Sequence[Any]) -> str:
    """The version a snapshot of a task of ``agent`` running ``extensions``
    must carry to be used."""
    h = hashlib.sha256(f"{FORMAT}\n{agent}\n".encode())
    for module in FOLD_MODULES:
        h.update(_code_digest(module).encode())
    for ext in extensions:
        h.update(repr(sorted(registry.pin(ext).items())).encode())
        for cls in type(ext).__mro__[:-1]:
            h.update(f"{cls.__qualname__}:{_code_digest(cls.__module__)}".encode())
    return f"{FORMAT}:{h.hexdigest()[:32]}"


class _Payload(msgspec.Struct):
    #: Without ``messages`` and ``ext``, which follow.
    state: TaskState
    #: Extension name -> its state, tagged (:func:`_tag`).
    ext: dict[str, Any]
    #: ``state.messages``: each one that is also in ``state.history`` (all
    #: of them until a compaction) as its index there, so it is kept once.
    messages: list[int | Message] = []


#: The key of an object standing for a value JSON has no shape for.
_TAG = "\x00noeta"
_SCALARS = (str, int, float, bool, type(None))
_COLLECTIONS: dict[str, Any] = {"tuple": tuple, "set": set, "frozenset": frozenset}


def _tag(value: Any) -> Any:
    """``value`` as JSON-shaped data that :func:`_untag` turns back into it:
    tuples, sets, dataclasses and Structs are tagged by type. Exact types
    only: a subclass (an enum, an ordered dict) would come back as its base
    and still compare equal."""
    if type(value) in _SCALARS:
        return value
    if type(value) is list:
        return [_tag(v) for v in value]
    if type(value) is dict:
        if not all(isinstance(k, str) for k in value) or _TAG in value:
            raise TypeError("snapshot: a mapping JSON cannot carry")
        return {k: _tag(v) for k, v in value.items()}
    kind = type(value).__name__
    if type(value) in _COLLECTIONS.values():
        return {_TAG: kind, "v": [_tag(v) for v in value]}
    if isinstance(value, msgspec.Struct) or dataclasses.is_dataclass(value):
        cls = type(value)
        return {_TAG: f"{cls.__module__}:{cls.__qualname__}", "v": msgspec.to_builtins(value)}
    raise TypeError(f"snapshot: cannot carry a {kind}")


def _untag(value: Any) -> Any:
    if isinstance(value, list):
        return [_untag(v) for v in value]
    if not isinstance(value, dict):
        return value
    kind = value.get(_TAG)
    if kind is None:
        return {k: _untag(v) for k, v in value.items()}
    if kind in _COLLECTIONS:
        return _COLLECTIONS[kind](_untag(v) for v in value["v"])
    return msgspec.convert(value["v"], _resolve(kind))


def _resolve(name: str) -> Any:
    """The dataclass or Struct type ``module:qualname`` names, from a module
    already imported (nothing is imported by name from stored data)."""
    module, _, qualname = name.partition(":")
    target: Any = sys.modules.get(module)
    for part in qualname.split("."):
        target = getattr(target, part, None)
    if isinstance(target, type) and (issubclass(target, msgspec.Struct)
                                     or dataclasses.is_dataclass(target)):
        return target
    raise ValueError(f"snapshot: no state type {name!r}")


def decode(data: bytes) -> TaskState:
    payload = msgspec.json.decode(data, type=_Payload)
    state = payload.state
    state.ext = {name: _untag(value) for name, value in payload.ext.items()}
    state.messages = [state.history[m] if isinstance(m, int) else m for m in payload.messages]
    return state


def encode(state: TaskState) -> bytes | None:
    """``state`` as snapshot data, or ``None`` when it would not read back
    equal to ``state``."""
    try:
        ext = {name: _tag(value) for name, value in state.ext.items()}
        index = {id(m): i for i, m in enumerate(state.history)}
        messages: list[int | Message] = [index.get(id(m), m) for m in state.messages]
        data = msgspec.json.encode(_Payload(
            state=dataclasses.replace(state, ext={}, messages=[]), ext=ext, messages=messages))
        same = decode(data) == state
    except (msgspec.MsgspecError, TypeError, ValueError):
        return None
    return data if same else None
