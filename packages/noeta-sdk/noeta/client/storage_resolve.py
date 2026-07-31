"""Storage-backend resolution: named backend + value-shape dispatch.

The **implementation** behind :mod:`noeta.sdk.storage` (which re-exports every
name here — that module stays the documented public doorway). It lives in
``noeta.client`` because ``HostConfig.storage_path`` resolves through it, and
``noeta.sdk`` sits *above* ``noeta.client`` in the import bands: keeping the
implementation here is what lets one host-config field share the loader every
other caller uses instead of growing a second copy of the dispatch.

Nothing here statically imports a backend. The concrete adapters live in the
``storage`` built-in (``noeta.builtins.storage.impl``) and are reached through
:func:`importlib.import_module`, the sanctioned microkernel doorway — so a host
that chose sqlite never pays for ``psycopg``.
"""

from __future__ import annotations

import importlib
from typing import Any, Optional

from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.event_log import EventLogFull


__all__ = [
    "build_storage_stack",
    "is_memory_path",
    "is_postgres_url",
    "open_storage_stack",
]


#: Backend name → the module whose ``build_stack(**config)`` builds its triple.
_BACKENDS = {
    "memory": "noeta.storage.memory",  # runtime wheel — the reference backend
    "sqlite": "noeta.builtins.storage.impl.sqlite.stack",
    "postgres": "noeta.builtins.storage.impl.postgres.stack",
}

#: URL schemes :func:`open_storage_stack` understands, mapped to their backend.
#: A ``storage_path`` that carries *some other* scheme is a typo, not a file
#: name — see the rejection in :func:`open_storage_stack`.
_URL_SCHEMES = {
    "postgresql": "postgres",
    "postgres": "postgres",
}


def build_storage_stack(
    backend: str, **config: Any
) -> tuple[EventLogFull, ContentStore, Dispatcher]:
    """Build the named backend's triple via its ``build_stack`` factory.

    ``config`` is passed through to the factory (``memory``: none;
    ``sqlite``: ``path=``; ``postgres``: ``dsn=``). The one internal
    invariant — the event log takes the dispatcher as ``lease_validator``
    — is wired by the factory itself. Unknown names raise ``ValueError``
    naming the known set.
    """
    module_name = _BACKENDS.get(backend)
    if module_name is None:
        known = ", ".join(sorted(_BACKENDS))
        raise ValueError(
            f"unknown storage backend {backend!r}; expected one of: {known}"
        )
    stack: tuple[EventLogFull, ContentStore, Dispatcher] = importlib.import_module(
        module_name
    ).build_stack(**config)
    return stack


def is_memory_path(storage_path: Optional[str]) -> bool:
    """Treat ``None`` / ``":memory:"`` as the InMemory backend."""
    return storage_path is None or storage_path == ":memory:"


def is_postgres_url(storage_path: str) -> bool:
    """Whether ``storage_path`` is a ``postgresql://`` / ``postgres://`` DSN."""
    return storage_path.startswith(("postgresql://", "postgres://"))


def open_storage_stack(
    storage_path: Optional[str],
) -> tuple[EventLogFull, ContentStore, Dispatcher]:
    """Value-shape dispatch over :func:`build_storage_stack`.

    ``storage_path`` is ``None`` / ``":memory:"`` for the InMemory stack, a
    ``postgresql://`` DSN for the Postgres stack, and otherwise a file path for
    the sqlite stack (the parameter's historical meaning). A convenience over
    :func:`build_storage_stack` — not a second mechanism — collapsing the
    branch its hosts (``HostConfig.storage_path``, the reference-host example,
    storage-URL configs) would otherwise repeat.

    A value that *looks* like a URL but carries an unrecognised scheme
    (``postgres1://``, ``mysql://``, a typo'd ``postgesql://``) raises
    ``ValueError`` naming the accepted schemes. It used to fall through to the
    sqlite branch, which silently created a database file **named after the
    DSN** — a misconfiguration that surfaced much later as a confusing empty
    store. Only a scheme-less string is a file path.
    """
    if is_memory_path(storage_path):
        return build_storage_stack("memory")
    assert storage_path is not None  # narrowed by is_memory_path
    scheme, sep, _rest = storage_path.partition("://")
    if sep:
        backend = _URL_SCHEMES.get(scheme.lower())
        if backend is None:
            accepted = ", ".join(sorted({f"{s}://" for s in _URL_SCHEMES}))
            raise ValueError(
                f"unrecognised storage URL scheme {scheme + '://'!r} in "
                f"{storage_path!r}; expected one of: {accepted}, ':memory:', "
                f"or a plain sqlite file path (no scheme)"
            )
        return build_storage_stack(backend, dsn=storage_path)
    return build_storage_stack("sqlite", path=storage_path)
