"""``BEGIN IMMEDIATE`` acquisition with retry, shared by the sqlite adapters.

It sits in its own module so the migration runner, the dispatcher, and any
other writer needing the same lock back-off consume one helper rather than
reaching into each other's internals.
"""

from __future__ import annotations

import sqlite3
import time


__all__ = ["_BEGIN_IMMEDIATE_RETRY_DELAYS", "_begin_immediate_with_retry"]


_BEGIN_IMMEDIATE_RETRY_DELAYS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0)


def _begin_immediate_with_retry(conn: sqlite3.Connection) -> None:
    """Issue ``BEGIN IMMEDIATE``, retrying briefly on transient
    ``database is locked``.

    ``PRAGMA busy_timeout`` covers most contention paths, but sqlite's built-in
    busy handler does not always fire for the WAL writer-lock acquisition that
    ``BEGIN IMMEDIATE`` triggers through Python's sqlite3 driver: under heavy
    contention ``SQLITE_BUSY`` reaches the caller as
    ``OperationalError('database is locked')``. The explicit back-off lets
    concurrent writers converge instead of aborting.
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
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise sqlite3.OperationalError(
            f"BEGIN IMMEDIATE remained locked after retries: {exc}"
        ) from last_error
