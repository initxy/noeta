"""``noeta.sdk.storage`` — the single public doorway for storage wiring.

A host picks a storage backend and injects the resulting
``(EventLogFull, ContentStore, Dispatcher)`` triple into the engine
(``Client`` / ``HostConfig``). This module is where that happens:
:func:`build_storage_stack` builds a named backend's triple through its
``build_stack`` factory, :func:`open_storage_stack` adds the value-shape
dispatch its hosts share (a storage path / DSN string), and the concrete
adapter classes are re-exported for hosts that construct adapters directly.

The durable backends live in the ``storage`` built-in
(``noeta.builtins.storage.impl`` — declaration-only manifest, never
activated); the InMemory reference backend stays in the kernel wheel
(``noeta.storage.memory``). The re-exports are **lazy** (PEP 562 module
``__getattr__``, the ``noeta.sdk.providers`` discipline): the universal
microkernel rule says nothing statically imports ``noeta.builtins``, and
``noeta.sdk`` is deliberately not a source of the ``sdk-core-not-builtins``
contract, so the backend modules load on first attribute access — only a
host that actually chose Postgres pays for ``psycopg``.

A third-party backend needs no registration here: implement the
``noeta.protocols`` storage Protocols, route the shared domain rules
through ``noeta.storage.spi``, ship a ``build_stack(**config)`` factory,
and inject the triple through ``HostConfig``.
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
    "PostgresContentStore",
    "PostgresDispatcher",
    "PostgresEventLog",
    "PostgresReadOnlyError",
    "PostgresReadOnlyStore",
    "PostgresSchemaVersionError",
    "SqliteContentStore",
    "SqliteDispatcher",
    "SqliteEventLog",
    "SqliteReadOnlyError",
    "SqliteReadOnlyStore",
    "SqliteSchemaVersionError",
]

#: Backend name → the module whose ``build_stack(**config)`` builds its triple.
_BACKENDS = {
    "memory": "noeta.storage.memory",  # runtime wheel — the reference backend
    "sqlite": "noeta.builtins.storage.impl.sqlite.stack",
    "postgres": "noeta.builtins.storage.impl.postgres.stack",
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
    """Treat a ``postgresql://`` / ``postgres://`` URL as the Postgres backend.

    Anything else is a sqlite file path (the historical shape of the
    parameter, kept as the fallback so existing configs stay valid).
    """
    return storage_path.startswith(("postgresql://", "postgres://"))


def open_storage_stack(
    storage_path: Optional[str],
) -> tuple[EventLogFull, ContentStore, Dispatcher]:
    """Value-shape dispatch over :func:`build_storage_stack`.

    ``storage_path`` is ``None`` / ``":memory:"`` for the InMemory stack,
    a ``postgresql://`` DSN for the Postgres stack, and a file path for
    the sqlite stack (the parameter's historical meaning). A convenience
    over :func:`build_storage_stack` — not a second mechanism — collapsing
    the branch its hosts (the reference-host example,
    ``noeta.testing.profile``-style assemblies, storage-URL configs) would
    otherwise repeat.
    """
    if is_memory_path(storage_path):
        return build_storage_stack("memory")
    assert storage_path is not None  # narrowed by is_memory_path
    if is_postgres_url(storage_path):
        return build_storage_stack("postgres", dsn=storage_path)
    return build_storage_stack("sqlite", path=storage_path)


#: Public adapter name → the backend package (its ``__init__``) defining it.
_EXPORTS = {
    "PostgresContentStore": "noeta.builtins.storage.impl.postgres",
    "PostgresDispatcher": "noeta.builtins.storage.impl.postgres",
    "PostgresEventLog": "noeta.builtins.storage.impl.postgres",
    "PostgresReadOnlyError": "noeta.builtins.storage.impl.postgres",
    "PostgresReadOnlyStore": "noeta.builtins.storage.impl.postgres",
    "PostgresSchemaVersionError": "noeta.builtins.storage.impl.postgres",
    "SqliteContentStore": "noeta.builtins.storage.impl.sqlite",
    "SqliteDispatcher": "noeta.builtins.storage.impl.sqlite",
    "SqliteEventLog": "noeta.builtins.storage.impl.sqlite",
    "SqliteReadOnlyError": "noeta.builtins.storage.impl.sqlite",
    "SqliteReadOnlyStore": "noeta.builtins.storage.impl.sqlite",
    "SqliteSchemaVersionError": "noeta.builtins.storage.impl.sqlite",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    return sorted(__all__)
