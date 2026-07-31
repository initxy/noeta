"""sqlite storage adapters — the ``storage`` built-in's file-backed backend.

This sub-package houses the sqlite3-backed adapters for the L0 storage
Protocols defined in ``noeta.protocols``: ``SqliteEventLog``,
``SqliteContentStore``, and ``SqliteDispatcher`` share the **same** sqlite
file and the migration sequence in
:mod:`noeta.builtins.storage.impl.sqlite.migrations`;
:mod:`~noeta.builtins.storage.impl.sqlite.stack` wires the triple. The
shared backend domain rules come from :mod:`noeta.storage.spi` — this
package imports only ``noeta.protocols`` and that SPI, standing in for a
third-party backend author.

Production code must depend on the L0 Protocols only; the universal
``sdk-core-not-builtins`` import-linter contract keeps every static import
path out of ``noeta.builtins``. The only doorway is :mod:`noeta.sdk.storage`
(lazy re-exports + stack builders); the host injects the built triple
through ``HostConfig``.
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
