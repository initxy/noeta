"""psycopg-backed adapters for the L0 storage Protocols.

All three adapters share one Postgres database behind a single DSN;
:mod:`noeta.builtins.storage.impl.postgres.migrations` owns its schema.
Postgres is MVCC, so each adapter takes a transaction-scoped advisory lock
over the state it read-modify-writes (per task stream for the EventLog, one
global lock for the Dispatcher state machine) to keep the serial semantics
the storage contract suite pins.
"""

from __future__ import annotations

from noeta.builtins.storage.impl.postgres.contentstore import PostgresContentStore
from noeta.builtins.storage.impl.postgres.dispatcher import PostgresDispatcher
from noeta.builtins.storage.impl.postgres.eventlog import PostgresEventLog
from noeta.builtins.storage.impl.postgres.readonly import (
    PostgresReadOnlyError,
    PostgresReadOnlyStore,
    PostgresSchemaVersionError,
)


__all__ = [
    "PostgresContentStore",
    "PostgresDispatcher",
    "PostgresEventLog",
    "PostgresReadOnlyError",
    "PostgresReadOnlyStore",
    "PostgresSchemaVersionError",
]
