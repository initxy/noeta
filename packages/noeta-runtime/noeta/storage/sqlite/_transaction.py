"""Generic transaction helpers for sqlite backend adapters.

Issue 17 promoted this module out of :mod:`noeta.storage.sqlite.migrations`
so that both the migration runner and the dispatcher (and any future
adapter that needs ``BEGIN IMMEDIATE`` retry semantics) consume the
same helper instead of reaching into migration-runner internals.
"""

from __future__ import annotations

import sqlite3
import time


__all__ = ["_BEGIN_IMMEDIATE_RETRY_DELAYS", "_begin_immediate_with_retry"]


_BEGIN_IMMEDIATE_RETRY_DELAYS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0)


def _begin_immediate_with_retry(conn: sqlite3.Connection) -> None:
    """Issue ``BEGIN IMMEDIATE``, retrying briefly on transient
    ``database is locked``.

    ``PRAGMA busy_timeout`` covers most contention paths, but sqlite's
    built-in busy handler does not always fire for the WAL-mode
    writer-lock acquisition that ``BEGIN IMMEDIATE`` triggers from
    Python's sqlite3 driver — under heavy contention it can return
    ``SQLITE_BUSY`` straight through to the caller as
    ``OperationalError('database is locked')``. We back off with
    short sleeps so concurrent writers converge instead of aborting.
    """
    last_error: sqlite3.OperationalError | None = None
    for delay in _BEGIN_IMMEDIATE_RETRY_DELAYS:
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            last_error = exc
            time.sleep(delay)
    # Final attempt: let any failure propagate so the caller sees the
    # real error rather than a synthetic one.
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise sqlite3.OperationalError(
            f"BEGIN IMMEDIATE remained locked after retries: {exc}"
        ) from last_error
