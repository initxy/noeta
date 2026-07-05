"""psycopg connection construction for the Postgres storage adapters.

Centralises connection settings so the EventLog / ContentStore /
Dispatcher adapters all see identical semantics, mirroring
:mod:`noeta.storage.sqlite._connection` for the sqlite backend:

* ``autocommit=True``       — the psycopg driver never opens implicit
                              transactions; each adapter owns its
                              transaction boundaries explicitly
                              (``BEGIN`` / ``COMMIT`` / ``ROLLBACK``),
                              exactly like the sqlite adapters run with
                              ``isolation_level=None``. Plain reads
                              outside a ``BEGIN`` see latest committed
                              state (the WAL concurrent-reader analogue).
* ``row_factory=dict_row``  — rows read by column name, mirroring
                              ``sqlite3.Row`` access in the sqlite
                              adapters.
* ``synchronous_commit=on`` — Postgres' default, asserted explicitly:
                              the EventLog is the "decision and
                              causality source of truth"; no committed
                              event may be lost on a crash (the
                              ``synchronous=FULL`` analogue).

Advisory-lock key space: sqlite serialises every writer behind the
file-wide ``BEGIN IMMEDIATE`` lock; Postgres is MVCC, so each adapter
takes a transaction-scoped advisory lock (``pg_advisory_xact_lock``)
over the state it read-modify-writes. The two-int form partitions the
key space by adapter class below; locks auto-release at COMMIT /
ROLLBACK. Advisory locks are database-wide (not schema-scoped), so two
schemas in one database serialise against each other — harmless for
correctness, and the per-task EventLog class keeps the hot append path
per-stream anyway.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import DictRow, dict_row


__all__ = [
    "_ADVISORY_CLASS_DISPATCHER",
    "_ADVISORY_CLASS_EVENTS",
    "_ADVISORY_CLASS_MIGRATIONS",
    "_open_connection",
]


#: ``pg_advisory_xact_lock(classid, objid)`` class ids, one per adapter
#: family so an EventLog stream lock can never collide with the
#: Dispatcher's global lock. Arbitrary but fixed 31-bit constants.
_ADVISORY_CLASS_MIGRATIONS = 0x6E5F6D69  # "n_mi"
_ADVISORY_CLASS_EVENTS = 0x6E5F6576  # "n_ev"
_ADVISORY_CLASS_DISPATCHER = 0x6E5F6469  # "n_di"


def _open_connection(dsn: str) -> psycopg.Connection[DictRow]:
    """Open a psycopg connection configured for the adapter suite.

    The connection is shared across threads by each adapter behind its
    own :class:`threading.Lock` (the same single-connection model as the
    sqlite adapters), so no pool is used.
    """
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    conn.execute("SET synchronous_commit = on")
    return conn
