"""psycopg connection construction shared by the Postgres storage adapters.

``autocommit=True`` because each adapter owns its transaction boundaries
explicitly (``BEGIN`` / ``COMMIT`` / ``ROLLBACK``); ``synchronous_commit=on``
is Postgres' default asserted here on purpose, because the EventLog is the
decision and causality source of truth and no committed event may be lost on
a crash.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import DictRow, dict_row


__all__ = [
    "_ADVISORY_CLASS_DISPATCHER",
    "_ADVISORY_CLASS_EVENTS",
    "_ADVISORY_CLASS_MIGRATIONS",
    "_DB_NOW_SQL",
    "_open_connection",
]


#: ``pg_advisory_xact_lock(classid, objid)`` class ids, one per adapter
#: family so an EventLog stream lock can never collide with the
#: Dispatcher's global lock. Arbitrary but fixed 31-bit constants. Advisory
#: locks are database-wide rather than schema-scoped, so two schemas sharing
#: one database serialise against each other.
_ADVISORY_CLASS_MIGRATIONS = 0x6E5F6D69  # "n_mi"
_ADVISORY_CLASS_EVENTS = 0x6E5F6576  # "n_ev"
_ADVISORY_CLASS_DISPATCHER = 0x6E5F6469  # "n_di"

#: SQL expression for the database clock "now", shared by the Dispatcher
#: (lease expiry / stale detection / timer firing) and the EventLog (in-tx
#: fence probe) so the time reference cannot drift between the two adapters.
#: ``clock_timestamp()`` rather than ``now()`` / ``current_timestamp``
#: because it advances within a transaction: a long emit transaction must
#: not compare against a stale statement-start instant.
_DB_NOW_SQL = "EXTRACT(EPOCH FROM clock_timestamp())::double precision"


def _open_connection(dsn: str) -> psycopg.Connection[DictRow]:
    """Each adapter shares one connection across threads behind its own
    :class:`threading.Lock`, so there is no pool.
    """
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    conn.execute("SET synchronous_commit = on")
    return conn
