"""sqlite3 connection construction with the Phase 1 PRAGMA set.

Centralises every PRAGMA we want set on a fresh sqlite3 connection so
the EventLog adapter (issue 15) and the upcoming ContentStore / Dispatcher
adapters (issues 16 / 17) all see identical durability and locking
semantics. The actual values are pinned by the issue 15 grill / architect
sign-off:

* ``journal_mode=WAL``      — readers don't block writers; required for
                              EventLog read paths to run during
                              writes. The ``:memory:`` engine may silently
                              ignore WAL; callers must not rely on the
                              mode being live there — file-backed
                              durability tests verify the real flag.
* ``synchronous=FULL``      — EventLog is the "decision and
                              causality source of truth"; no committed
                              event may be lost on OS crash.
                              Snapshots are a fold-acceleration point,
                              **not** a recovery mechanism. ``NORMAL``
                              stays out of the default for issue 15.
* ``busy_timeout=5000``     — five seconds of cooperative back-off when
                              another writer holds the database lock.
* ``foreign_keys=ON``       — guard against later migrations that forget
                              to enable them per connection.
* ``temp_store=MEMORY``     — sort / index scratch lives in RAM; cheaper
                              for the small Phase 1 workloads.

``mmap_size`` deliberately remains unset — macOS cgroup accounting for
mmap regions is unfriendly and Phase 1 has no measured benefit. Phase 2
may revisit.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional, Union


_JOURNAL_MODE_RETRY_DELAYS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0)


def _set_journal_mode_wal(conn: sqlite3.Connection) -> None:
    """Attempt to switch the connection to WAL mode, retrying on lock.

    ``PRAGMA journal_mode = WAL`` rewrites the database header and
    must hold an exclusive lock to do so. Unlike most write paths,
    this PRAGMA does **not** honour ``busy_timeout`` reliably across
    sqlite versions — two threads opening a fresh database
    simultaneously can both return ``database is locked`` here even
    after the timeout was set. We back off and retry the lock-error
    path; if sqlite *silently* returns a non-WAL mode (notably
    ``:memory:`` databases, which sqlite forces to ``memory`` journal
    mode and ignores the WAL request) we accept that as a deliberate
    choice and move on. The contract suite that runs ``:memory:``
    backends does not depend on file-locking semantics, and the
    file-on-disk durability test asserts the file path produces a
    ``wal`` journal mode.
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

    # One final attempt: surface the lock error if it still stands.
    try:
        result = conn.execute("PRAGMA journal_mode = WAL").fetchone()
        _accept_journal_mode_result(result)
    except sqlite3.OperationalError as exc:
        raise sqlite3.OperationalError(
            f"PRAGMA journal_mode = WAL still locked after retries: {exc}"
        ) from last_error


def _accept_journal_mode_result(row: Optional[sqlite3.Row]) -> None:
    """Validate the value sqlite returned for ``PRAGMA journal_mode = WAL``.

    Only ``wal`` (real WAL switch) and ``memory`` (``:memory:`` DBs
    silently stay in their built-in memory journal) are acceptable;
    anything else means another connection forced the file into a
    non-WAL mode and we should surface that loudly instead of
    pretending the switch worked.
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
    """Open a sqlite3 connection and apply the Phase 1 PRAGMA set.

    ``check_same_thread=False`` lets the single :class:`SqliteEventLog`
    connection be used from any thread; the adapter serialises real
    access through its own :class:`threading.Lock`, so the sqlite3
    re-entrancy guard would only add noise. The connection's
    ``isolation_level`` is set to ``None`` so the adapter owns
    transaction boundaries explicitly (``BEGIN IMMEDIATE`` /
    ``COMMIT`` / ``ROLLBACK``) and the Python driver never injects an
    implicit ``COMMIT`` between statements.
    """
    target = str(path)
    conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # ``busy_timeout`` must be set BEFORE any statement that takes a
    # write lock — including ``PRAGMA journal_mode = WAL``, which
    # modifies the file header and contends with concurrent
    # initialisers. Without this ordering, two threads opening a fresh
    # database at the same time can fail their journal-mode change
    # with ``database is locked`` before the timeout has any chance
    # to apply.
    conn.execute("PRAGMA busy_timeout = 5000")
    _set_journal_mode_wal(conn)
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn
