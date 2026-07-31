"""sqlite3 adapters for the L0 storage Protocols — the file-backed backend.

``SqliteEventLog``, ``SqliteContentStore`` and ``SqliteDispatcher`` share the
**same** sqlite file and the one migration sequence in
:mod:`noeta.builtins.storage.impl.sqlite.migrations`;
:mod:`~noeta.builtins.storage.impl.sqlite.stack` wires the triple. The package
imports only ``noeta.protocols`` and the shared domain rules in
:mod:`noeta.storage.spi`, standing in for a third-party backend author.
Nothing static may import it back: production code depends on the L0 Protocols
and reaches an implementation through :mod:`noeta.sdk.storage`, which the host
injects via ``HostConfig``.
"""

from __future__ import annotations

from noeta.builtins.storage.impl.sqlite.contentstore import SqliteContentStore
from noeta.builtins.storage.impl.sqlite.dispatcher import SqliteDispatcher
from noeta.builtins.storage.impl.sqlite.eventlog import SqliteEventLog
from noeta.builtins.storage.impl.sqlite.readonly import (
    SqliteReadOnlyError,
    SqliteReadOnlyStore,
    SqliteSchemaVersionError,
)


__all__ = [
    "SqliteContentStore",
    "SqliteDispatcher",
    "SqliteEventLog",
    "SqliteReadOnlyError",
    "SqliteReadOnlyStore",
    "SqliteSchemaVersionError",
]
