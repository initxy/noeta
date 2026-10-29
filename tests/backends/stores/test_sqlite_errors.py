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
from noeta.core.events import External, TaskCreated
from noeta.core.store import HOLD_LOST, NewTask, Snapshot


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
    path = str(tmp_path / "t.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE noeta_schema (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO noeta_schema VALUES (1)")
    conn.commit()
    conn.close()
    store = SqliteStore(path)
    await store.create(NewTask(task_id="t", created=TaskCreated(agent="a")))
    await store.close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT version FROM noeta_schema").fetchall() == [(SCHEMA_VERSION,)]
    conn.close()
    assert SCHEMA_VERSION == 4


async def test_upgrading_a_version_1_store_tells_its_held_trees(tmp_path: Path) -> None:
    """Version 1 kept holds in a ``holds`` table that version 2 does not
    read: each tree held there is told its hold is gone (its background
    jobs' worker no longer holds it) instead of being let go silently."""
    path = str(tmp_path / "t.db")
    store = SqliteStore(path)
    await store.create(NewTask(task_id="R", created=TaskCreated(agent="a")))
    await store.create(NewTask(task_id="S", created=TaskCreated(agent="a", parent_id="R",
                                                                root_id="R")))
    await store.create(NewTask(task_id="free", created=TaskCreated(agent="a")))
    await store.close()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE holds (root_id TEXT PRIMARY KEY, worker TEXT NOT NULL,"
                 " until REAL NOT NULL)")
    conn.execute("INSERT INTO holds VALUES ('R', 'w-old', 1e12)")
    conn.execute("UPDATE noeta_schema SET version = 1")
    conn.commit()
    conn.close()
    lost = External(name=HOLD_LOST, data={"worker": "w-old", "epoch": 0})
    for _ in range(2):  # told once
        store = SqliteStore(path)
        assert [i.body for i in await store.inbox("R")] == [lost]
        assert [i.body for i in await store.inbox("S")] == [lost]
        assert await store.inbox("free") == []
        await store.close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'holds'").fetchone() is None
    conn.close()


async def test_a_holds_table_an_old_binary_wrote_again_is_told_and_dropped(
        tmp_path: Path) -> None:
    """An older binary started on an upgraded store writes ``holds`` anew;
    the next open tells those trees and drops the table again."""
    path = str(tmp_path / "t.db")
    store = SqliteStore(path)
    await store.create(NewTask(task_id="R", created=TaskCreated(agent="a")))
    await store.close()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE holds (root_id TEXT PRIMARY KEY, worker TEXT NOT NULL,"
                 " until REAL NOT NULL)")
    conn.execute("INSERT INTO holds VALUES ('R', 'w-old', 1e12)")
    conn.commit()
    conn.close()
    store = SqliteStore(path)
    assert [i.body for i in await store.inbox("R")] == [
        External(name=HOLD_LOST, data={"worker": "w-old", "epoch": 0})]
    await store.close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'holds'").fetchone() is None
    conn.close()


async def test_a_version_2_store_keeps_its_snapshots_keyed_by_version(tmp_path: Path) -> None:
    """Version 2 kept one snapshot per task; version 3 keys them by task
    and fold version. The rows are carried over."""
    path = str(tmp_path / "t.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE noeta_schema (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO noeta_schema VALUES (2)")
    conn.execute("CREATE TABLE snapshots (task_id TEXT PRIMARY KEY, seq INTEGER NOT NULL,"
                 " version TEXT NOT NULL, data BLOB NOT NULL) WITHOUT ROWID")
    conn.execute("INSERT INTO snapshots VALUES ('t', 4, 'v1', x'00')")
    conn.commit()
    conn.close()
    for _ in range(2):
        store = SqliteStore(path)
        assert await store.snapshot("t", "v1") == Snapshot(seq=4, version="v1", data=b"\x00")
        await store.put_snapshot("t", Snapshot(seq=6, version="v2", data=b"two"))
        assert await store.snapshot("t", "v1") == Snapshot(seq=4, version="v1", data=b"\x00")
        await store.close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT version FROM noeta_schema").fetchall() == [(SCHEMA_VERSION,)]
    conn.close()


async def test_a_version_3_store_drops_its_tenant_column_and_usage(tmp_path: Path) -> None:
    """Version 3 kept a tenant per task and each tenant's usage; version 4
    drops both and keeps the tasks."""
    path = str(tmp_path / "t.db")
    store = SqliteStore(path)
    await store.create(NewTask(task_id="t", created=TaskCreated(agent="a")))
    await store.close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE tasks ADD COLUMN tenant TEXT NOT NULL DEFAULT 'default'")
    conn.execute("CREATE INDEX tasks_tenant ON tasks (tenant, status)")
    conn.execute("CREATE TABLE tenant_usage (tenant TEXT NOT NULL, period TEXT NOT NULL,"
                 " tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,"
                 " PRIMARY KEY (tenant, period))")
    conn.execute("UPDATE tasks SET tenant = 'acme'")
    conn.execute("UPDATE noeta_schema SET version = 3")
    conn.commit()
    conn.close()
    store = SqliteStore(path)
    assert (await store.get("t")) is not None
    await store.create(NewTask(task_id="u", created=TaskCreated(agent="a")))
    assert {r.task_id for r in await store.tasks()} == {"t", "u"}
    await store.close()
    conn = sqlite3.connect(path)
    assert "tenant" not in {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert conn.execute("SELECT name FROM sqlite_master WHERE name IN"
                        " ('tenant_usage', 'tasks_tenant')").fetchall() == []
    assert conn.execute("SELECT version FROM noeta_schema").fetchall() == [(SCHEMA_VERSION,)]
    conn.close()


async def test_a_store_without_hold_queues_gains_the_column(tmp_path: Path) -> None:
    """``tree_holds.queues`` came later: an older store gets the column, and
    a hold written without it (NULL) refuses no child."""
    path = str(tmp_path / "t.db")
    store = SqliteStore(path)
    await store.create(NewTask(task_id="R", created=TaskCreated(agent="a")))
    await store.close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE tree_holds DROP COLUMN queues")
    conn.execute("INSERT INTO tree_holds (root_id, worker, until) VALUES ('R', 'w', 1e12)")
    conn.commit()
    conn.close()
    store = SqliteStore(path)
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    await store.append(claim, [], expected_seq=1, creates=[NewTask(
        task_id="R.gpu", queue="gpu",
        created=TaskCreated(agent="a", parent_id="R", root_id="R"))])
    assert await store.get("R.gpu") is not None
    await store.close()
