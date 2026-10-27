"""The sqlite store's failure mapping and its first open under contention."""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import sqlite3
from pathlib import Path

import pytest

from noeta.core.errors import StoreUnavailable
from noeta.core.events import TaskCreated
from noeta.core.store.base import NewTask
from noeta.core.store.sqlite import SqliteStore


async def test_a_locked_database_is_store_unavailable(tmp_path: Path) -> None:
    path = str(tmp_path / "t.db")
    store = SqliteStore(path)
    await store.create(NewTask(task_id="t", created=TaskCreated(agent="a")))
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")

    def short_timeout(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA busy_timeout=50")

    await store._run(short_timeout)
    try:
        with pytest.raises(StoreUnavailable):
            await store.claim("w", ["default"], 30)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    # The connection is usable again: no transaction was left open.
    assert await store.claim("w", ["default"], 30) is not None
    await store.close()


def _open(path: str, q: mp.Queue) -> None:  # type: ignore[type-arg]
    async def go() -> None:
        s = SqliteStore(path)
        try:
            await s.get("x")
            q.put("ok")
        except Exception as e:  # noqa: BLE001
            q.put(f"{type(e).__name__}: {e}")
        await s.close()

    asyncio.run(go())


def test_concurrent_first_open(tmp_path: Path) -> None:
    ctx = mp.get_context("spawn")
    results = []
    for i in range(6):
        path = str(tmp_path / f"t{i}.db")
        q = ctx.Queue()
        procs = [ctx.Process(target=_open, args=(path, q)) for _ in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
        results += [q.get(timeout=5) for _ in procs]
    assert results == ["ok"] * len(results)
