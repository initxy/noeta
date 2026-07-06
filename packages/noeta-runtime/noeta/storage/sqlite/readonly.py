"""Read-only sqlite reader for list / inspect projections (CW5b P1).

`noeta code list` (and any future read-only consumer) must NEVER create,
migrate, or otherwise mutate the sqlite file. The live adapters cannot give
that guarantee: ``_open_connection`` issues ``PRAGMA journal_mode = WAL`` (a
header write even on a current-version DB) and the adapter constructors run
``apply_migrations`` (forward-migrating an older store).

:class:`SqliteReadOnlyStore` opens the file strictly read-only — a ``mode=ro``
URI connection, no ``journal_mode`` / ``synchronous`` writes, no migrations —
and verifies ``PRAGMA user_version == SCHEMA_VERSION`` **before** any read. An
older / newer schema is a typed :class:`SqliteSchemaVersionError` (never silently
read, never migrated). It satisfies ``EventLogReader`` + ``EventLogTaskIndex`` +
``ContentStore`` through plain ``SELECT``s, reusing the live adapter's
:func:`noeta.storage.sqlite.eventlog._row_to_envelope` so the read shape cannot
drift. The adapter is selected at the CLI wiring layer only — ``noeta.read_models``
and ``noeta.agent.sessions`` depend on the Protocols, never on this class.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from noeta.protocols.errors import ContentNotFound, NoetaError
from noeta.protocols.event_log import TaskStreamSummary
from noeta.protocols.events import EventEnvelope
from noeta.protocols.values import ContentRef
from noeta.storage.sqlite.eventlog import _row_to_envelope
from noeta.storage.sqlite.migrations import SCHEMA_VERSION


__all__ = [
    "SqliteReadOnlyError",
    "SqliteReadOnlyStore",
    "SqliteSchemaVersionError",
]


class SqliteSchemaVersionError(NoetaError):
    """The store's ``PRAGMA user_version`` is not the version this build reads.

    Raised up front by :class:`SqliteReadOnlyStore` so a read-only command
    surfaces a clear "different Noeta schema version" error instead of silently
    reading an unknown shape or (the live adapters' behaviour) migrating it.
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
        # mode=ro: the connection physically cannot write — no journal_mode /
        # synchronous PRAGMA writes, no migrations, no file creation.
        self._conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
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
        # TaskRewound / StepAttemptAbandoned are snapshot-shaped fold
        # baselines too — take whichever of the three has the higher seq.
        row = self._conn.execute(
            "SELECT * FROM events WHERE task_id = ? "
            "AND type IN ('TaskSnapshot', 'TaskRewound', 'StepAttemptAbandoned') "
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
