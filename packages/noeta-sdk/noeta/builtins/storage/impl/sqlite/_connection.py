"""One PRAGMA set for every sqlite3 connection the adapters open.

The three adapters share a single file, so they must share identical
durability and locking semantics; divergent PRAGMAs would give the same
database different crash guarantees depending on which adapter opened it.
Two values are load-bearing: ``synchronous = FULL``, because the event log is
the decision and causality source of truth and snapshots only accelerate the
fold rather than recover it, so no committed event may be lost to an OS crash;
and ``journal_mode = WAL``, so read paths can run while a writer holds the
file. ``mmap_size`` is deliberately left unset — macOS cgroup accounting for
mmap regions is hostile and no benefit has been measured against it.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional, Union


_JOURNAL_MODE_RETRY_DELAYS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0)


def _set_journal_mode_wal(conn: sqlite3.Connection) -> None:
    """Switch the connection to WAL mode, retrying on lock.

    ``PRAGMA journal_mode = WAL`` rewrites the database header under an
    exclusive lock and does **not** honour ``busy_timeout`` reliably across
    sqlite versions, so two threads opening a fresh database at once can both
    get ``database is locked`` even with the timeout set — hence the manual
    back-off. A silently non-WAL result is accepted rather than fatal:
    ``:memory:`` databases are forced to the ``memory`` journal and ignore the
    request, and nothing that runs on them depends on file-locking semantics.
    """
    current = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(current).lower() == "wal":
        return

    last_error: sqlite3.OperationalError | None = None
    for delay in _JOURNAL_MODE_RETRY_DELAYS:
        try:
            result = conn.execute("PRAGMA journal_mode = WAL").fetchone()
            _accept_journal_mode_result(result)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            last_error = exc
            time.sleep(delay)

    try:
        result = conn.execute("PRAGMA journal_mode = WAL").fetchone()
        _accept_journal_mode_result(result)
    except sqlite3.OperationalError as exc:
        raise sqlite3.OperationalError(
            f"PRAGMA journal_mode = WAL still locked after retries: {exc}"
        ) from last_error


def _accept_journal_mode_result(row: Optional[sqlite3.Row]) -> None:
    """Accept ``wal`` and ``memory``; anything else means another connection
    forced the file into a non-WAL mode, which must surface loudly instead of
    passing for a successful switch.
    """
    if row is None:
        return
    mode = str(row[0]).lower()
    if mode in {"wal", "memory"}:
        return
    raise sqlite3.OperationalError(
        f"PRAGMA journal_mode = WAL produced unexpected mode: {mode!r}"
    )


def _open_connection(path: Union[str, Path]) -> sqlite3.Connection:
    """Open a sqlite3 connection with the shared PRAGMA set applied.

    ``check_same_thread=False`` lets one connection be used from any thread;
    each adapter serialises real access through its own
    :class:`threading.Lock`, so the sqlite3 re-entrancy guard would only add
    noise. ``isolation_level=None`` puts transaction boundaries entirely in the
    adapter's hands (``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK``) so the
    driver never injects an implicit ``COMMIT`` between statements.
    """
    target = str(path)
    conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # ``busy_timeout`` must be set BEFORE any statement that takes a write lock
    # — including ``PRAGMA journal_mode = WAL``, which rewrites the file header
    # and contends with concurrent initialisers. Set later, two threads opening
    # a fresh database at once can fail their journal-mode change with
    # ``database is locked`` before the timeout applies at all.
    conn.execute("PRAGMA busy_timeout = 5000")
    _set_journal_mode_wal(conn)
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn
