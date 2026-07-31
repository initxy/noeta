"""Plugin trust store and the shared plugin error surface.

Deliberately small and dependency-free: the loader stands on these primitives,
and they carry no knowledge of surfaces or contributions. Trust is matched on a
canonical path, so how a directory is spelled never decides whether its plugins
load.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional


__all__ = [
    "PluginError",
    "UntrustedPluginDirWarning",
    "grant_trust",
    "is_trusted",
    "PLUGIN_ENTRY_POINT_GROUP",
    "DEFAULT_TRUST_STORE",
]


PLUGIN_ENTRY_POINT_GROUP = "noeta.plugins"

#: Operator-approved workspace plugin directories, as JSON
#: ``{"trusted": [abs path, ...]}``.
DEFAULT_TRUST_STORE = Path.home() / ".noeta" / "trust.json"


class PluginError(RuntimeError):
    """A plugin failed to load, or its contributions collided at merge.

    Raised loudly — never swallowed — so a bad plugin fails the client build at
    startup rather than a mid-session turn. The single exception is an
    *untrusted* workspace directory, which is skipped with an
    :class:`UntrustedPluginDirWarning` instead of raising.
    """


class UntrustedPluginDirWarning(UserWarning):
    """A workspace plugin directory was skipped because it is not trusted."""


def _read_trusted(store: Path) -> list[str]:
    try:
        raw = store.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PluginError(
            f"trust store {store} is not valid JSON: {exc}"
        ) from exc
    trusted = data.get("trusted", []) if isinstance(data, Mapping) else []
    return [str(p) for p in trusted]


def _canonical(path: Any) -> str:
    """The canonical trust key for ``path``: ``~`` expanded, absolute, resolved.

    Both sides of every trust comparison go through this, so a grant written as
    ``~/ws/../ws/plugins`` matches a lookup of ``/home/me/ws/plugins``. Symlinks
    resolve too — the key names the *directory the code is read from*, which is
    what an operator grants. A non-existent path resolves lexically
    (``strict=False``) rather than raising.
    """
    return str(Path(path).expanduser().resolve())


def is_trusted(path: Any, store: Optional[Path] = None) -> bool:
    """Return whether ``path``'s canonical form is recorded in the trust store.

    Stored entries are canonicalised on read too, so a hand-written store still
    matches. A missing store means "nothing trusted" (``False``), never an error.
    """
    store = Path(store) if store is not None else DEFAULT_TRUST_STORE
    target = _canonical(path)
    return any(_canonical(p) == target for p in _read_trusted(store))


def grant_trust(path: Any, store: Optional[Path] = None) -> None:
    """Record ``path``'s canonical form as trusted, creating the store if absent.

    Idempotent: a path already trusted under any spelling is left unchanged.
    """
    store = Path(store) if store is not None else DEFAULT_TRUST_STORE
    target = _canonical(path)
    trusted = _read_trusted(store)
    if all(_canonical(p) != target for p in trusted):
        trusted.append(target)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps({"trusted": trusted}, indent=2) + "\n", encoding="utf-8"
    )
