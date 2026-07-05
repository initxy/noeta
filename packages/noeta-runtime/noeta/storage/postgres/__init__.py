"""Postgres storage adapters.

This sub-package houses the psycopg-backed adapters for the L0 storage
Protocols defined in ``noeta.protocols`` — the second persistent backend
after ``noeta.storage.sqlite``, behaviour pinned by the same
storage-backend-neutral contract suites. All three adapters share one
Postgres database (one DSN), mirroring the sqlite adapters sharing one
file; schema setup lives in :mod:`noeta.storage.postgres.migrations`.

Where sqlite serialises writers with the file-wide ``BEGIN IMMEDIATE``
lock, Postgres is MVCC — each adapter takes a transaction-scoped
advisory lock instead (per task stream for the EventLog, one global
lock for the Dispatcher state machine) so the read-modify-write blocks
(``MAX(seq)+1`` allocation, FIFO ``ready_order`` assignment, wake
matching) keep the exact serial semantics the contract pins.

``psycopg`` ships as a regular noeta-runtime dependency (the
``[binary]`` flavor, bundling libpq, so Postgres works out of the box);
this sub-package is still only imported by wiring that chose Postgres,
keeping cold imports cheap.

Production code must depend on the L0 Protocols only; the
``storage-adapters-isolated`` import-linter contract blocks kernel
layers from importing ``noeta.storage`` at all. Tests / wiring shims
that explicitly inject an adapter are the only legitimate callers here.
"""

from __future__ import annotations

from noeta.storage.postgres.contentstore import PostgresContentStore
from noeta.storage.postgres.dispatcher import PostgresDispatcher
from noeta.storage.postgres.eventlog import PostgresEventLog
from noeta.storage.postgres.readonly import (
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
