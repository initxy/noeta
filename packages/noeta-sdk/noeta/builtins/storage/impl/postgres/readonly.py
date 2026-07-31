"""Read-only Postgres reader for list / inspect projections.

A read-only consumer must NEVER create, migrate, or otherwise mutate the
store, which is a guarantee the live adapters cannot give: their constructors
run ``apply_migrations``. :class:`PostgresReadOnlyStore` therefore opens the
connection with ``default_transaction_read_only = on`` so the server itself
rejects every write, and verifies the recorded ``noeta_schema_version``
equals :data:`SCHEMA_VERSION` **before** any read — a mismatched or
uninitialised schema raises :class:`PostgresSchemaVersionError` rather than
being silently read. Reads reuse the live adapter's
:func:`noeta.builtins.storage.impl.postgres.eventlog._row_to_envelope` so the
two read shapes cannot drift, and a single consumer needs no adapter lock.
"""

from __future__ import annotations

from typing import Optional

import psycopg
from psycopg.rows import dict_row

from noeta.protocols.errors import ContentNotFound, NoetaError
from noeta.protocols.event_log import (
    SNAPSHOT_BASELINE_EVENT_TYPES,
    TaskStreamSummary,
)
from noeta.protocols.events import EventEnvelope
from noeta.protocols.values import ContentRef

from noeta.builtins.storage.impl.postgres.eventlog import _row_to_envelope
from noeta.builtins.storage.impl.postgres.migrations import SCHEMA_VERSION

# The ``find_latest_snapshot`` predicate, rendered once from the protocol
# constant so the query can never drift from the contract set. The
# ``ix_events_snapshot`` partial index must keep matching it textually, so
# widening the constant means writing a new migration.
_BASELINE_TYPES_SQL = "(" + ", ".join(
    f"'{t}'" for t in SNAPSHOT_BASELINE_EVENT_TYPES
) + ")"


__all__ = [
    "PostgresReadOnlyError",
    "PostgresReadOnlyStore",
    "PostgresSchemaVersionError",
]


class PostgresSchemaVersionError(NoetaError):
    """The store's recorded schema version is not the version this build reads.

    Raised up front so a read-only caller gets a clear schema-version error
    instead of reading an unknown row shape. ``found == 0`` also covers a
    database with no ``noeta_schema_version`` table at all.
    """

    def __init__(self, *, dsn: str, found: int, expected: int) -> None:
        self.dsn = dsn
        self.found = found
        self.expected = expected
        super().__init__(
            f"postgres store is at schema version {found}, but this "
            f"build reads version {expected}; a read-only command will not "
            "migrate it (run a writable command to upgrade)."
        )


class PostgresReadOnlyError(NoetaError):
    """A write was attempted on a read-only store (e.g. ``put``)."""


class PostgresReadOnlyStore:
    """Strictly read-only view over a Postgres Noeta store.

    Satisfies ``EventLogReader`` + ``EventLogTaskIndex`` + ``ContentStore``.
    Never writes: the session forces read-only transactions; ``put`` raises.
    """

    def __init__(self, dsn: str) -> None:
        self._conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        try:
            # Server-side write rejection for every subsequent statement, so
            # no DDL, migration, or version bump can slip through.
            self._conn.execute("SET default_transaction_read_only = on")
            found = self._read_version()
            if found != SCHEMA_VERSION:
                raise PostgresSchemaVersionError(
                    dsn=dsn, found=found, expected=SCHEMA_VERSION
                )
        except Exception:
            self._conn.close()
            raise

    def _read_version(self) -> int:
        try:
            row = self._conn.execute(
                "SELECT version FROM noeta_schema_version"
            ).fetchone()
        except psycopg.errors.UndefinedTable:
            # An uninitialised database reads as version 0 rather than an
            # error, so the caller sees one schema-version failure mode.
            return 0
        return 0 if row is None else int(row["version"])

    # -- EventLogReader ---------------------------------------------------

    def read(
        self, task_id: str, *, after_seq: Optional[int] = None
    ) -> list[EventEnvelope]:
        if after_seq is None:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE task_id = %s ORDER BY seq",
                (task_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE task_id = %s AND seq > %s "
                "ORDER BY seq",
                (task_id, after_seq),
            ).fetchall()
        return [_row_to_envelope(row) for row in rows]

    def find_latest_snapshot(self, task_id: str) -> Optional[EventEnvelope]:
        # Every type in the baseline set re-bases the fold, so the highest
        # seq among them wins regardless of which type it is.
        row = self._conn.execute(
            "SELECT * FROM events WHERE task_id = %s "
            f"AND type IN {_BASELINE_TYPES_SQL} "
            "ORDER BY seq DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return _row_to_envelope(row) if row is not None else None

    # -- EventLogTaskIndex ------------------------------------------------

    def list_task_streams(self) -> list[TaskStreamSummary]:
        rows = self._conn.execute(
            "SELECT task_id, MAX(seq) AS last_seq, "
            "MAX(occurred_at) AS last_event_time "
            "FROM events GROUP BY task_id "
            "ORDER BY last_event_time DESC, task_id ASC"
        ).fetchall()
        return [
            TaskStreamSummary(
                task_id=row["task_id"],
                last_seq=int(row["last_seq"]),
                last_event_time=float(row["last_event_time"]),
            )
            for row in rows
        ]

    # -- ContentStore -----------------------------------------------------

    def get(self, ref: ContentRef) -> bytes:
        row = self._conn.execute(
            "SELECT body FROM content WHERE hash = %s", (ref.hash,)
        ).fetchone()
        if row is None:
            raise ContentNotFound(ref.hash)
        return bytes(row["body"])

    def put(self, body: bytes, *, media_type: str) -> ContentRef:
        raise PostgresReadOnlyError(
            "content store opened read-only; put() is not allowed"
        )

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        self._conn.close()
