"""The Postgres store's fold snapshots (``Store.snapshot`` /
``put_snapshot``), run inside the store's transactions. ``q`` puts the
store's schema into a statement (``{s}``)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg

from noeta.core.store import SNAPSHOT_VERSIONS, Snapshot

Conn = psycopg.AsyncConnection[Any]
Quote = Callable[[str], str]


async def read(conn: Conn, q: Quote, task_id: str, version: str | None) -> Snapshot | None:
    """The task's snapshot of ``version``; without one, its newest."""
    if version is None:
        cur = await conn.execute(q(
            "SELECT seq, version, data FROM {s}.snapshots WHERE task_id = %s"
            " ORDER BY seq DESC LIMIT 1"), (task_id,))
    else:
        cur = await conn.execute(q(
            "SELECT seq, version, data FROM {s}.snapshots WHERE task_id = %s"
            " AND version = %s"), (task_id, version))
    row = await cur.fetchone()
    return Snapshot(seq=int(row[0]), version=str(row[1]), data=bytes(row[2])) if row else None


async def write(conn: Conn, q: Quote, task_id: str, snapshot: Snapshot) -> None:
    """Keep ``snapshot`` unless the stored one of its version is at a later
    seq or the task has ended; past ``SNAPSHOT_VERSIONS`` versions the one
    at the lowest seq goes."""
    # The row lock orders this against the append that ends the task (and
    # deletes its snapshot): a snapshot never outlives the end.
    cur = await conn.execute(q(
        "SELECT status FROM {s}.tasks WHERE task_id = %s FOR SHARE"), (task_id,))
    row = await cur.fetchone()
    if row is not None and row[0] == "ended":
        return
    await conn.execute(q(
        "INSERT INTO {s}.snapshots (task_id, seq, version, data) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (task_id, version) DO UPDATE SET seq = excluded.seq,"
        " data = excluded.data WHERE excluded.seq >= {s}.snapshots.seq"),
        (task_id, snapshot.seq, snapshot.version, snapshot.data))
    await conn.execute(q(
        "DELETE FROM {s}.snapshots WHERE task_id = %s AND version NOT IN ("
        " SELECT version FROM {s}.snapshots WHERE task_id = %s"
        " ORDER BY version = %s DESC, seq DESC LIMIT %s)"),
        (task_id, task_id, snapshot.version, SNAPSHOT_VERSIONS))


async def delete(conn: Conn, q: Quote, task_id: str) -> None:
    """The task ended: it is never claimed again."""
    await conn.execute(q("DELETE FROM {s}.snapshots WHERE task_id = %s"), (task_id,))
