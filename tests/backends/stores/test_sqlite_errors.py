"""The sqlite store's failure mapping and its first open under contention."""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import sqlite3
from pathlib import Path

import pytest

from noeta.backends.stores._rows import SCHEMA_VERSION
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.errors import StoreUnavailable
from noeta.core.events import TaskCreated
from noeta.core.store import NewTask, Snapshot


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


async def test_a_store_from_a_newer_noeta_is_refused(tmp_path: Path) -> None:
    path = str(tmp_path / "t.db")
    store = SqliteStore(path)
    await store.create(NewTask(task_id="t", created=TaskCreated(agent="a")))
    await store.close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE noeta_schema SET version = ?", (SCHEMA_VERSION + 1,))
    conn.commit()
    conn.close()
    newer = SqliteStore(path)
    with pytest.raises(StoreUnavailable, match="schema version"):
        await newer.get("t")
    await newer.close()


async def test_a_version_1_store_is_upgraded_in_place(tmp_path: Path) -> None:
    """0.6's store (version 1) kept its workers' holds in ``holds`` and had
    no snapshots: the holds go, the tasks stay, and the store is current."""
    path = str(tmp_path / "t.db")
    store = SqliteStore(path)
    await store.create(NewTask(task_id="R", created=TaskCreated(agent="a")))
    await store.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE snapshots")
    conn.execute("CREATE TABLE holds (root_id TEXT PRIMARY KEY, worker TEXT NOT NULL,"
                 " until REAL NOT NULL)")
    conn.execute("INSERT INTO holds VALUES ('R', 'w-old', 1e12)")
    conn.execute("UPDATE noeta_schema SET version = 1")
    conn.commit()
    conn.close()
    store = SqliteStore(path)
    assert (await store.get("R")) is not None
    await store.put_snapshot("R", Snapshot(seq=1, version="v", data=b"x"))
    assert await store.snapshot("R") == Snapshot(seq=1, version="v", data=b"x")
    await store.close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT version FROM noeta_schema").fetchall() == [(SCHEMA_VERSION,)]
    assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%holds%'"
                        ).fetchall() == []
    conn.close()
