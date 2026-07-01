"""Phase 1 sqlite storage adapters.

This sub-package houses the sqlite3-backed adapters for the L0 storage
Protocols defined in ``noeta.protocols``. Issue 15 lands ``SqliteEventLog``;
issues 16 / 17 will add ``SqliteContentStore`` and ``SqliteDispatcher``
into the **same** sqlite file, sharing the migration sequence in
:mod:`noeta.storage.sqlite.migrations`.

Production code must depend on the L0 Protocols only; the
``storage-adapters-isolated`` import-linter contract blocks
``noeta.{core,runtime,context,policies,tools,providers,protocols}``
from importing ``noeta.storage`` at all. Tests / wiring shims that
explicitly inject an adapter are the only legitimate callers here.
"""

from __future__ import annotations

from noeta.storage.sqlite.contentstore import SqliteContentStore
from noeta.storage.sqlite.dispatcher import SqliteDispatcher
from noeta.storage.sqlite.eventlog import SqliteEventLog
from noeta.storage.sqlite.readonly import (
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
