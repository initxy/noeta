"""``noeta.sdk.storage`` — the single public doorway for storage wiring.

A host picks a backend and injects the resulting ``(EventLogFull, ContentStore,
Dispatcher)`` triple into the engine. The concrete adapter classes and stack
builders are re-exported **lazily** (PEP 562 module ``__getattr__``) because
nothing statically imports ``noeta.builtins`` and only a host that actually chose
Postgres should pay for ``psycopg``.

A third-party backend needs no registration here: implement the
``noeta.protocols`` storage Protocols, route the shared domain rules
through ``noeta.storage.spi``, ship a ``build_stack(**config)`` factory,
and inject the triple through ``HostConfig``.
"""

from __future__ import annotations

import importlib
from typing import Any

# The stack builders live one band down (``noeta.client.storage_resolve``) so
# ``HostConfig.storage_path`` can share this exact dispatch — ``noeta.sdk`` sits
# above ``noeta.client``, so the host config could not import them from here.
from noeta.client.storage_resolve import (
    build_storage_stack,
    is_memory_path,
    is_postgres_url,
    open_storage_stack,
)


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
