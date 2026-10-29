"""Postgres-only behaviour: changes from other processes reach ``on_change``,
dropped connections leave no half-written state and are replaced, and a
commit with an unknown outcome is never retried."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from typing import Any

import pytest

from noeta.core.errors import CommitUnknown, StoreUnavailable
from noeta.core.events import Cancel, TaskCreated, Waiting
from noeta.core.store import NewTask, Post, Snapshot

pytestmark = pytest.mark.usefixtures("pg")


def new(task_id: str = "t1") -> NewTask:
    return NewTask(task_id=task_id, created=TaskCreated(agent="a"))


async def _until(pred: Any, timeout: float = 10.0) -> None:
    async def loop() -> None:
        while not pred():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(loop(), timeout)


async def _terminate(dsn: str, pids: list[int]) -> None:
    import psycopg

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as c:
        for pid in pids:
            await c.execute("SELECT pg_terminate_backend(%s)", (pid,))
    await asyncio.sleep(0.2)


async def test_on_change_sees_commits_from_another_process(pg: Any) -> None:
    store = pg.store()
    await store.create(new())
    seen: list[tuple[str, str]] = []
    unsubscribe = store.on_change(lambda t, k: seen.append((t, k)))
    script = textwrap.dedent(f"""
        import asyncio
        from noeta.core.events import Cancel
        from noeta.core.store import Post
        from noeta.backends.stores.postgres import PostgresStore

        async def main():
            s = PostgresStore({pg.dsn!r}, schema={pg.schema!r})
            await s.post(Post(task_id="t1", body=Cancel()))
            await s.close()

        asyncio.run(main())
    """)
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", script)
    assert await proc.wait() == 0
    await _until(lambda: ("t1", "posted") in seen)
    unsubscribe()
    await store.close()


async def test_on_change_sees_another_connection_and_not_its_own_echo(pg: Any) -> None:
    a, b = pg.store(), pg.store()
    seen_a: list[tuple[str, str]] = []
    seen_b: list[tuple[str, str]] = []
    a.on_change(lambda t, k: seen_a.append((t, k)))
    b.on_change(lambda t, k: seen_b.append((t, k)))
    await b.get("x")  # b is listening from here on
    await a.create(new())
    await a.post(Post(task_id="t1", body=Cancel()))
    await _until(lambda: len(seen_b) == 2)
    await asyncio.sleep(0.3)  # give a's own echoes time to (not) arrive
    assert seen_a == [("t1", "created"), ("t1", "posted")]
    assert seen_b == [("t1", "created"), ("t1", "posted")]
    await a.close()
    await b.close()


async def test_listener_and_pool_reconnect_after_backends_are_killed(pg: Any) -> None:
    a, b = pg.store(), pg.store()
    seen: list[str] = []
    a.on_change(lambda t, k: seen.append(k))
    await a.create(new())
    await b.get("t1")
    await _terminate(pg.dsn, [*a._feed.own.keys(), *b._feed.own.keys()])
    import psycopg

    async with await psycopg.AsyncConnection.connect(pg.dsn, autocommit=True) as c:
        await c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                        " WHERE query = %s", (f'LISTEN "{a._feed.channel}"',))
    row = await a.get("t1")  # the pool replaced its dead connection
    assert row is not None and row.status == "ready"

    async def posted_and_seen() -> None:
        n = 0
        while "posted" not in seen:
            n += 1
            await b.post(Post(task_id="t1", body=Cancel(), dedupe_key=f"k{n}"))
            await asyncio.sleep(0.2)

    await asyncio.wait_for(posted_and_seen(), 15)
    await a.close()
    await b.close()


async def _claimed(store: Any) -> Any:
    await store.create(new())
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    return claim


async def test_drop_before_commit_is_rerun_once(pg: Any, monkeypatch: Any) -> None:
    store = pg.store()
    claim = await _claimed(store)
    original = store._notify
    calls = 0

    async def drop_first(conn: Any, changes: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            await _terminate(pg.dsn, [conn.info.backend_pid])
        await original(conn, changes)

    monkeypatch.setattr(store, "_notify", drop_first)
    assert (await store.append(claim, [Waiting(on="input")], expected_seq=1)).seq == 2
    assert calls == 2 and len(await store.read("t1")) == 2
    await store.close()


async def test_drop_during_commit_raises_and_leaves_nothing(pg: Any, monkeypatch: Any) -> None:
    store = pg.store()
    claim = await _claimed(store)
    original = store._notify
    calls = 0

    async def drop_after(conn: Any, changes: Any) -> None:
        nonlocal calls
        calls += 1
        await original(conn, changes)
        if calls == 1:  # the next thing this transaction sends is COMMIT
            await _terminate(pg.dsn, [conn.info.backend_pid])

    monkeypatch.setattr(store, "_notify", drop_after)
    with pytest.raises(CommitUnknown):
        await store.append(claim, [Waiting(on="input")], expected_seq=1)
    assert calls == 1  # not retried
    # Here the COMMIT never reached the server, so nothing landed; and the
    # store has a working connection again for the next call.
    assert len(await store.read("t1")) == 1
    assert (await store.append(claim, [Waiting(on="input")], expected_seq=1)).seq == 2
    await store.close()


async def test_concurrent_schema_setup_from_many_processes(pg: Any) -> None:
    script = textwrap.dedent(f"""
        import asyncio
        from noeta.backends.stores.postgres import PostgresStore

        async def main():
            s = PostgresStore({pg.dsn!r}, schema={pg.schema!r})
            await s.get("x")
            await s.close()

        asyncio.run(main())
    """)
    procs = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(6)]
    assert [p.wait(60) for p in procs] == [0] * 6


async def test_a_schema_from_a_newer_noeta_is_refused(pg: Any) -> None:
    import psycopg

    from noeta.backends.stores._rows import SCHEMA_VERSION

    store = pg.store()
    await store.create(new())
    await store.close()
    with psycopg.connect(pg.dsn, autocommit=True) as conn:
        conn.execute(f'UPDATE "{pg.schema}".noeta_schema SET version = %s',
                     (SCHEMA_VERSION + 1,))
    newer = pg.store()
    with pytest.raises(StoreUnavailable, match="version"):
        await newer.get("t1")
    await newer.close()


async def test_a_version_1_schema_is_upgraded_in_place(pg: Any) -> None:
    import psycopg

    from noeta.backends.stores._rows import SCHEMA_VERSION

    with psycopg.connect(pg.dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{pg.schema}"')
        conn.execute(f'CREATE TABLE "{pg.schema}".noeta_schema (version INTEGER NOT NULL)')
        conn.execute(f'INSERT INTO "{pg.schema}".noeta_schema VALUES (1)')
    store = pg.store()
    await store.create(new())
    await store.close()
    with psycopg.connect(pg.dsn, autocommit=True) as conn:
        rows = conn.execute(f'SELECT version FROM "{pg.schema}".noeta_schema').fetchall()
    assert rows == [(SCHEMA_VERSION,)] and SCHEMA_VERSION == 4


async def test_a_version_2_schema_keeps_its_snapshots_keyed_by_version(pg: Any) -> None:
    """Version 2 kept one snapshot per task; version 3 keys them by task and
    fold version, the rows kept."""
    import psycopg

    store = pg.store()
    await store.create(new())
    await store.close()
    with psycopg.connect(pg.dsn, autocommit=True) as conn:
        conn.execute(f'DROP TABLE "{pg.schema}".snapshots')
        conn.execute(f'CREATE TABLE "{pg.schema}".snapshots (task_id TEXT PRIMARY KEY,'
                     " seq BIGINT NOT NULL, version TEXT NOT NULL, data BYTEA NOT NULL)")
        conn.execute(f"INSERT INTO \"{pg.schema}\".snapshots VALUES ('t1', 4, 'v1', '\\x00')")
        conn.execute(f'UPDATE "{pg.schema}".noeta_schema SET version = 2')
    for _ in range(2):
        store = pg.store()
        assert await store.snapshot("t1", "v1") == Snapshot(seq=4, version="v1", data=b"\x00")
        await store.put_snapshot("t1", Snapshot(seq=6, version="v2", data=b"two"))
        assert await store.snapshot("t1", "v1") == Snapshot(seq=4, version="v1", data=b"\x00")
        await store.close()


async def test_a_version_3_schema_drops_its_tenant_column_and_usage(pg: Any) -> None:
    """Version 3 kept a tenant per task and each tenant's usage; version 4
    drops both and keeps the tasks."""
    import psycopg

    store = pg.store()
    await store.create(new())
    await store.close()
    with psycopg.connect(pg.dsn, autocommit=True) as conn:
        s = f'"{pg.schema}"'
        conn.execute(f"ALTER TABLE {s}.tasks ADD COLUMN tenant TEXT NOT NULL DEFAULT 'default'")
        conn.execute(f"CREATE INDEX tasks_tenant ON {s}.tasks (tenant, status)")
        conn.execute(f"CREATE TABLE {s}.tenant_usage (tenant TEXT NOT NULL, period TEXT NOT NULL,"
                     " tokens BIGINT NOT NULL DEFAULT 0, cost_usd DOUBLE PRECISION NOT NULL"
                     " DEFAULT 0, PRIMARY KEY (tenant, period))")
        conn.execute(f"UPDATE {s}.noeta_schema SET version = 3")
    store = pg.store()
    assert (await store.get("t1")) is not None
    await store.create(NewTask(task_id="t2", created=TaskCreated(agent="a")))
    await store.close()
    with psycopg.connect(pg.dsn, autocommit=True) as conn:
        cols = conn.execute("SELECT column_name FROM information_schema.columns WHERE"
                            " table_schema = %s AND table_name = 'tasks'", (pg.schema,)).fetchall()
        tables = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = %s"
                              " AND table_name = 'tenant_usage'", (pg.schema,)).fetchall()
    assert ("tenant",) not in cols and tables == []


async def test_a_claim_waits_for_a_hold_of_its_tree_being_taken(pg: Any) -> None:
    """A hold being taken (its transaction not committed yet) and a claim of
    the same tree serialize: the claim does not take a task of a tree that
    another worker's hold, once committed, keeps."""
    import psycopg

    from noeta.backends.stores.postgres import _HOLD_LOCK_CLASS

    store = pg.store()
    await store.create(new("R"))
    async with await psycopg.AsyncConnection.connect(pg.dsn) as holder:
        # What PostgresStore.hold does, held open before its commit.
        await holder.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                             (_HOLD_LOCK_CLASS, f"{pg.schema}:R"))
        await holder.execute(
            f'INSERT INTO "{pg.schema}".tree_holds (root_id, worker, until)'
            " VALUES ('R', 'A', extract(epoch from now()) + 60)")
        claim = asyncio.create_task(store.claim("B", ["default"], 30))
        await asyncio.sleep(0.3)
        await holder.commit()
        assert await asyncio.wait_for(claim, 10) is None
    row = await store.get("R")
    assert row is not None and row.owner is None
    await store.close()


async def test_a_hold_waits_for_a_claim_of_its_tree_being_taken(pg: Any) -> None:
    import psycopg

    from noeta.backends.stores.postgres import _HOLD_LOCK_CLASS

    store = pg.store()
    await store.create(new("R"))
    async with await psycopg.AsyncConnection.connect(pg.dsn) as claimer:
        # What PostgresStore.claim does, held open before its commit.
        await claimer.execute(
            f'UPDATE "{pg.schema}".tasks SET status = \'running\', owner = \'B\','
            " owner_epoch = owner_epoch + 1, lease_until = extract(epoch from now()) + 60"
            " WHERE task_id = 'R'")
        await claimer.execute("SELECT pg_advisory_xact_lock_shared(%s, hashtext(%s))",
                              (_HOLD_LOCK_CLASS, f"{pg.schema}:R"))
        hold = asyncio.create_task(store.hold("R", "A", 30))
        await asyncio.sleep(0.3)
        assert not hold.done()
        await claimer.commit()
        assert await asyncio.wait_for(hold, 10) is None
    await store.close()


async def test_a_hold_lets_a_claim_of_a_lapsed_task_in_flight_finish(pg: Any) -> None:
    """A claim that locked a lapsed task of the tree, before it takes the
    tree's lock, is not deadlocked by a hold taking the lapsed claim back:
    the hold steps aside, the claim commits, and the hold sees its lease."""
    import psycopg

    from noeta.backends.stores.postgres import _HOLD_LOCK_CLASS

    store = pg.store()
    await store.create(new("R"))
    assert await store.claim("B", ["default"], -1) is not None  # lapsed at once
    async with await psycopg.AsyncConnection.connect(pg.dsn) as claimer:
        # What PostgresStore.claim does with C's claim before the tree's lock.
        await claimer.execute(
            f'UPDATE "{pg.schema}".tasks SET owner = \'C\', owner_epoch = owner_epoch + 1,'
            " lease_until = extract(epoch from now()) + 60 WHERE task_id = 'R'")
        hold = asyncio.create_task(store.hold("R", "A", 30))
        await asyncio.sleep(0.3)
        assert not hold.done()
        # Well inside deadlock_timeout (1 s): no deadlock for Postgres to break.
        await asyncio.wait_for(claimer.execute(
            "SELECT pg_advisory_xact_lock_shared(%s, hashtext(%s))",
            (_HOLD_LOCK_CLASS, f"{pg.schema}:R")), 0.5)
        await claimer.commit()
        assert await asyncio.wait_for(hold, 10) is None
    row = await store.get("R")
    assert row is not None and row.owner == "C" and row.status == "running"
    await store.close()


async def test_upgrading_a_version_1_schema_tells_its_held_trees(pg: Any) -> None:
    import psycopg

    from noeta.core.events import External
    from noeta.core.store import HOLD_LOST

    store = pg.store()
    await store.create(new("R"))
    await store.create(NewTask(task_id="S", created=TaskCreated(agent="a", parent_id="R",
                                                                root_id="R")))
    await store.create(new("free"))
    await store.close()
    schema = f'"{pg.schema}"'
    with psycopg.connect(pg.dsn, autocommit=True) as conn:
        conn.execute(f"CREATE TABLE {schema}.holds (root_id TEXT PRIMARY KEY,"
                     " worker TEXT NOT NULL, until DOUBLE PRECISION NOT NULL)")
        conn.execute(f"INSERT INTO {schema}.holds VALUES ('R', 'w-old', 1e12)")
        conn.execute(f"UPDATE {schema}.noeta_schema SET version = 1")
    lost = External(name=HOLD_LOST, data={"worker": "w-old", "epoch": 0})
    for _ in range(2):  # told once
        store = pg.store()
        assert [i.body for i in await store.inbox("R")] == [lost]
        assert [i.body for i in await store.inbox("S")] == [lost]
        assert await store.inbox("free") == []
        await store.close()
    with psycopg.connect(pg.dsn, autocommit=True) as conn:
        assert conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = %s"
                            " AND table_name = 'holds'", (pg.schema,)).fetchone() is None


async def test_a_schema_without_hold_queues_gains_the_column(pg: Any) -> None:
    """``tree_holds.queues`` came later: an older schema gets the column, and
    a hold written without it (NULL) refuses no child."""
    import psycopg

    store = pg.store()
    await store.create(new("R"))
    await store.close()
    with psycopg.connect(pg.dsn, autocommit=True) as conn:
        conn.execute(f'ALTER TABLE "{pg.schema}".tree_holds DROP COLUMN queues')
        conn.execute(f'INSERT INTO "{pg.schema}".tree_holds (root_id, worker, until)'
                     " VALUES ('R', 'w', extract(epoch from now()) + 60)")
    store = pg.store()
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    await store.append(claim, [], expected_seq=1, creates=[NewTask(
        task_id="R.gpu", queue="gpu",
        created=TaskCreated(agent="a", parent_id="R", root_id="R"))])
    assert await store.get("R.gpu") is not None
    await store.close()


async def test_an_append_creating_a_child_waits_for_a_hold_of_its_tree_being_taken(
        pg: Any) -> None:
    """An append's child and a hold being taken of the same tree serialize:
    the hold, once committed, is seen, and its holder not serving the
    child's queue refuses the child."""
    import psycopg

    from noeta.backends.stores.postgres import _HOLD_LOCK_CLASS
    from noeta.core.errors import UnservedQueue

    store = pg.store()
    await store.create(new("R"))
    claim = await store.claim("A", ["default"], 30)
    assert claim is not None
    async with await psycopg.AsyncConnection.connect(pg.dsn) as holder:
        # What PostgresStore.hold does, held open before its commit.
        await holder.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                             (_HOLD_LOCK_CLASS, f"{pg.schema}:R"))
        await holder.execute(
            f'INSERT INTO "{pg.schema}".tree_holds (root_id, worker, until, queues)'
            " VALUES ('R', 'A', extract(epoch from now()) + 60, ARRAY['default'])")
        append = asyncio.create_task(store.append(claim, [], expected_seq=1, creates=[
            NewTask(task_id="R.gpu", queue="gpu",
                    created=TaskCreated(agent="a", parent_id="R", root_id="R"))]))
        await asyncio.sleep(0.3)
        assert not append.done()
        await holder.commit()
        with pytest.raises(UnservedQueue):
            await asyncio.wait_for(append, 10)
    assert await store.get("R.gpu") is None
    await store.close()
