"""Plugin trust store + shared error surface.

The 0.4.0 typed contribution-bundle mechanism (``noeta_plugin(api)`` factories,
the ``PluginAPI`` accumulator, ``merge_plugins`` and the host-plane
``merged_*`` accessors) has been **retired** in favour of the manifest-declared
mechanism in :mod:`noeta.client.plugin_manifest` / :mod:`noeta.client.plugin_set`
(``docs/adr/plugin-contribution-bundles.md``). What remains here are the
primitives that mechanism still stands on:

* :class:`PluginError` / :class:`UntrustedPluginDirWarning` — the loud-failure
  and skip-with-warning signals every loader path raises.
* the **trust store** (:func:`grant_trust` / :func:`is_trusted` /
  :data:`DEFAULT_TRUST_STORE`) — operator-approved workspace plugin directories,
  matched on a canonical path so spelling never decides trust.
* :data:`PLUGIN_ENTRY_POINT_GROUP` — the entry-point group the SDK owns.

These are deliberately kept in one small, dependency-free module: the loader
imports them, but they carry no knowledge of surfaces or contributions.
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


#: The entry-point group the SDK owns for runtime-plane plugins.
PLUGIN_ENTRY_POINT_GROUP = "noeta.plugins"

#: The default trust store recording operator-approved workspace plugin
#: directories (JSON ``{"trusted": [abs path, ...]}``).
DEFAULT_TRUST_STORE = Path.home() / ".noeta" / "trust.json"


class PluginError(RuntimeError):
    """A plugin failed to load, or its contributions collided at merge.

    Raised loudly — never swallowed — so a bad plugin fails the client build
    at startup rather than a mid-session turn. The single documented exception
    is an *untrusted* workspace directory, which is skipped with an
    :class:`UntrustedPluginDirWarning` instead of raising.
    """


class UntrustedPluginDirWarning(UserWarning):
    """A workspace plugin directory was skipped because it is not trusted."""


# ---------------------------------------------------------------------------
# Trust store
# ---------------------------------------------------------------------------


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

    Both sides of the trust comparison go through this, so a grant written as
    ``~/ws/../ws/plugins`` still matches a lookup of ``/home/me/ws/plugins``.
    Symlinks are resolved too — the key names the *directory the code is read
    from*, which is what an operator grants. Non-existent paths resolve
    lexically (``strict=False``) rather than raising.
    """
    return str(Path(path).expanduser().resolve())


def is_trusted(path: Any, store: Optional[Path] = None) -> bool:
    """Return whether ``path``'s canonical form is recorded in the trust store.

    ``store`` defaults to :data:`DEFAULT_TRUST_STORE`. Stored entries are
    canonicalised on read as well, so a store written by an older version (or
    by hand) still matches. A missing store is treated as "nothing trusted"
    (returns ``False``), never an error.
    """
    store = Path(store) if store is not None else DEFAULT_TRUST_STORE
    target = _canonical(path)
    return any(_canonical(p) == target for p in _read_trusted(store))


def grant_trust(path: Any, store: Optional[Path] = None) -> None:
    """Record ``path``'s canonical form as trusted (idempotent).

    Creates the store (and its parent directory) if absent. Safe to call
    repeatedly — a path that is already trusted under any spelling is left
    unchanged.
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
