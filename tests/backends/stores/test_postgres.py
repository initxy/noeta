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
from noeta.core.store import NewTask, Patch, Post

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
    """0.6's schema (version 1) kept its workers' holds in ``holds`` and a
    claim index over every unended task: both go, the tasks stay."""
    import psycopg

    from noeta.backends.stores._rows import SCHEMA_VERSION

    store = pg.store()
    await store.create(new())
    await store.close()
    s = f'"{pg.schema}"'
    with psycopg.connect(pg.dsn, autocommit=True) as conn:
        conn.execute(f"DROP TABLE {s}.snapshots; DROP INDEX {s}.tasks_claim")
        conn.execute(f"CREATE INDEX tasks_claimable ON {s}.tasks (queue, updated_at)"
                     " WHERE status <> 'ended'")
        conn.execute(f"CREATE TABLE {s}.holds (root_id TEXT PRIMARY KEY,"
                     " worker TEXT NOT NULL, until DOUBLE PRECISION NOT NULL)")
        conn.execute(f"INSERT INTO {s}.holds VALUES ('t1', 'w-old', 1e12)")
        conn.execute(f"UPDATE {s}.noeta_schema SET version = 1")
    store = pg.store()
    assert await store.get("t1") is not None
    assert await store.claim("w", ["default"], 30) is not None
    await store.close()
    with psycopg.connect(pg.dsn, autocommit=True) as conn:
        rows = conn.execute(f"SELECT version FROM {s}.noeta_schema").fetchall()
        names = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s"
            " UNION SELECT indexname FROM pg_indexes WHERE schemaname = %s",
            (pg.schema, pg.schema)).fetchall()}
    assert rows == [(SCHEMA_VERSION,)]
    assert "holds" not in names and "tasks_claimable" not in names and "tasks_claim" in names


async def test_the_claim_index_holds_only_claimable_rows_and_the_claim_uses_it(
        pg: Any) -> None:
    """Idle waiting tasks (no timer) are not in the claim index, and the
    claim's scan goes through it."""
    import psycopg

    from noeta.backends.stores.postgres import _CLAIMABLE

    store = pg.store()
    for i in range(3):
        await store.create(new(f"t{i}"))
        claim = await store.claim("w", ["default"], 30)
        assert claim is not None
        await store.append(claim, [Waiting(on="input")], expected_seq=1,
                           patch=Patch(status="waiting", waiting_on="input"))
    await store.create(new("ready"))
    s = f'"{pg.schema}"'
    with psycopg.connect(pg.dsn, autocommit=True) as conn:
        conn.execute(f"ANALYZE {s}.tasks")
        conn.execute("SET enable_seqscan = off")
        plan = "\n".join(r[0] for r in conn.execute(
            f"EXPLAIN SELECT task_id FROM {s}.tasks WHERE queue = ANY(ARRAY['default'])"
            f" AND {_CLAIMABLE} ORDER BY updated_at LIMIT 1").fetchall())
        indexed = conn.execute(
            f"SELECT count(*) FROM {s}.tasks WHERE {_CLAIMABLE}").fetchone()
    assert "tasks_claim" in plan
    assert indexed == (1,)
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None and claim.task_id == "ready"
    await store.close()
