"""Plugins and backends by name.

Everything that is not the kernel is found through Python entry points, the
built-ins exactly like a third-party package's (see ``pyproject.toml``):

* ``noeta.plugins`` — a name -> an ``Extension`` class (or factory);
* ``noeta.providers`` — a provider family -> ``factory(**kw) -> Provider``;
* ``noeta.execenvs`` — a name -> ``factory(cwd, **kw) -> ExecEnv``;
* ``noeta.stores`` — a URL scheme -> ``factory(url) -> Store``.

The kernel imports no plugin or backend module statically; it loads one by
name here when it needs a default (the local exec env).
"""

from __future__ import annotations

from functools import cache
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from noeta.core.execenv import ExecEnv
    from noeta.core.provider import Provider
    from noeta.core.store.base import Store

__all__ = [
    "EXECENVS",
    "PLUGINS",
    "PROVIDERS",
    "STORES",
    "UnknownName",
    "available",
    "execenv",
    "load",
    "provider",
    "store",
]

PLUGINS = "noeta.plugins"
PROVIDERS = "noeta.providers"
EXECENVS = "noeta.execenvs"
STORES = "noeta.stores"


class UnknownName(LookupError):
    """No entry point of that name in the group."""


@cache
def _group(group: str) -> dict[str, EntryPoint]:
    return {ep.name: ep for ep in entry_points(group=group)}


def available(group: str) -> list[str]:
    """The names registered in ``group``, sorted."""
    return sorted(_group(group))


def load(group: str, name: str) -> Any:
    """The object the entry point ``name`` in ``group`` points at."""
    ep = _group(group).get(name)
    if ep is None:
        known = ", ".join(available(group)) or "none"
        raise UnknownName(f"no {group} entry point named {name!r} (available: {known})")
    return ep.load()


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
