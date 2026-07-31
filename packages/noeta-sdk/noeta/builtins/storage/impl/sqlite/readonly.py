"""Strictly read-only sqlite reader for list / inspect projections.

A read-only consumer must NEVER create, migrate, or otherwise mutate the
sqlite file, and the live adapters cannot promise that: ``_open_connection``
issues ``PRAGMA journal_mode = WAL`` — a header write even on an up-to-date
file — and every adapter constructor runs ``apply_migrations``. This module's
store therefore opens a ``mode=ro`` URI connection, writes no PRAGMAs, runs no
migrations, and checks ``PRAGMA user_version == SCHEMA_VERSION`` **before** any
read, so a mismatched schema surfaces as a typed
:class:`SqliteSchemaVersionError` instead of a silent read or an implicit
upgrade. It satisfies ``EventLogReader`` + ``EventLogTaskIndex`` +
``ContentStore`` through plain ``SELECT``s, reusing the live adapter's
:func:`noeta.builtins.storage.impl.sqlite.eventlog._row_to_envelope` so the
read shape cannot drift from it.
"""

from __future__ import annotations

import sqlite3
from typing import Optional
from urllib.parse import quote

from noeta.protocols.errors import ContentNotFound, NoetaError
from noeta.protocols.event_log import (
    SNAPSHOT_BASELINE_EVENT_TYPES,
    TaskStreamSummary,
)
from noeta.protocols.events import EventEnvelope
from noeta.protocols.values import ContentRef

from noeta.builtins.storage.impl.sqlite.eventlog import _row_to_envelope
from noeta.builtins.storage.impl.sqlite.migrations import SCHEMA_VERSION

# The ``find_latest_snapshot`` predicate, rendered once from the protocol
# constant so the query cannot drift from the contract set. The
# ``ix_events_snapshot`` partial index must keep matching this text exactly or
# sqlite stops choosing it — see the migration notes.
_BASELINE_TYPES_SQL = "(" + ", ".join(
    f"'{t}'" for t in SNAPSHOT_BASELINE_EVENT_TYPES
) + ")"



__all__ = [
    "SqliteReadOnlyError",
    "SqliteReadOnlyStore",
    "SqliteSchemaVersionError",
]


class SqliteSchemaVersionError(NoetaError):
    """The store's ``PRAGMA user_version`` is not the version this build reads.

    Raised before the first read so a read-only caller gets a clear
    schema-version error rather than rows of an unknown shape.
    """

    def __init__(self, *, path: str, found: int, expected: int) -> None:
        self.path = path
        self.found = found
        self.expected = expected
        super().__init__(
            f"sqlite store {path!r} is at schema version {found}, but this "
            f"build reads version {expected}; a read-only command will not "
            "migrate it (run a writable command to upgrade)."
        )


class SqliteReadOnlyError(NoetaError):
    """A write was attempted on a read-only store (e.g. ``put``)."""


class SqliteReadOnlyStore:
    """Strictly read-only view over a sqlite Noeta store.

    Satisfies ``EventLogReader`` + ``EventLogTaskIndex`` + ``ContentStore``.
    Never writes: opened ``mode=ro``; ``put`` raises.
    """

    def __init__(self, path: str) -> None:
        # The path is spliced into a ``file:`` URI, so it must be
        # percent-encoded first: an unescaped ``?``, ``#``, or ``%`` would be
        # parsed as the query-string delimiter, the fragment delimiter, or a
        # broken escape — silently changing which file is opened, or with which
        # flags, and defeating the read-only guarantee this class exists for.
        # ``safe="/"`` leaves the path separators sqlite's URI parser expects.
        self._conn = sqlite3.connect(f"file:{quote(path, safe='/')}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row
        found = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if found != SCHEMA_VERSION:
            self._conn.close()
            raise SqliteSchemaVersionError(
                path=path, found=found, expected=SCHEMA_VERSION
            )

    # -- EventLogReader ---------------------------------------------------

    def read(
        self, task_id: str, *, after_seq: Optional[int] = None
    ) -> list[EventEnvelope]:
        if after_seq is None:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE task_id = ? ORDER BY seq",
                (task_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE task_id = ? AND seq > ? "
                "ORDER BY seq",
                (task_id, after_seq),
            ).fetchall()
        return [_row_to_envelope(row) for row in rows]

    def find_latest_snapshot(self, task_id: str) -> Optional[EventEnvelope]:
        # Every baseline type is snapshot-shaped, so the highest-seq row of any
        # of them re-bases the fold.
        row = self._conn.execute(
            "SELECT * FROM events WHERE task_id = ? "
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
                task_id=row[0],
                last_seq=int(row[1]),
                last_event_time=float(row[2]),
            )
            for row in rows
        ]

    # -- ContentStore -----------------------------------------------------

    def get(self, ref: ContentRef) -> bytes:
        row = self._conn.execute(
            "SELECT body FROM content WHERE hash = ?", (ref.hash,)
        ).fetchone()
        if row is None:
            raise ContentNotFound(ref.hash)
        return bytes(row["body"])

    def put(self, body: bytes, *, media_type: str) -> ContentRef:
        raise SqliteReadOnlyError(
            "content store opened read-only; put() is not allowed"
        )

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        self._conn.close()
