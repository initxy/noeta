"""Postgres store: the same four tables as the sqlite store (blobs aside), for
many processes on many machines.

Why each check is race-free (every write is one READ COMMITTED transaction):

* ``append`` first takes ``SELECT ... FOR UPDATE`` on the task row, then
  checks status, epoch and ``last_seq``. Any other append, claim, renew,
  release or post on that task waits for the row lock and, once it has it,
  sees this transaction's committed result. So the conditional checks and
  the writes that follow act on a row nobody else can change in between.
* ``post`` locks the target task row the same way before it reads
  ``inbox_seq``, looks up the dedupe key and decides to wake a waiting task.
  The dedupe lookup is a fresh statement after the lock, so it sees a post
  that committed while we waited. Because an append going to ``waiting``
  holds the same lock while it compares ``inbox_seq`` with the read mark, a
  post lands either before (the append stays ``ready``) or after (the post
  wakes the task); nothing is left unseen.
* ``create`` inserts with ``ON CONFLICT DO NOTHING``: a concurrent creator
  with the same task id or dedupe key blocks on the unique index until the
  first commits, then does nothing and reads the winner's id.
* ``claim`` picks a row with ``FOR UPDATE SKIP LOCKED``, so two workers
  never take the same task, and Postgres re-checks the claimable condition
  on the newest row version before the lock is granted. The epoch bump is
  the fence; leases only decide when a silent owner may be replaced.
* Deadlocks (an append posting to a task that is posting back) abort one
  transaction whole; it is re-run, and its checks run again.

Time for leases and claims is the database clock, so machines with skewed
clocks agree on who holds a lease. ``Patch.ready_at`` timers come from the
runner's clock.

Payloads are ``bytea`` holding msgspec's JSON bytes as is: no re-parse on
write or re-serialisation on read, and ``jsonb``'s refusal of ``\\u0000`` in
strings (which model and tool output can contain) cannot fail an append.

Changes are announced with ``pg_notify`` inside the same transaction, and
live model output rides a second channel (``postgres_feed``). Snapshot
reads and writes are in ``postgres_snapshots``.

The connection pool, opening the store and the transaction retry rules
(deadlocks, connection loss) are in ``postgres_conn``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import msgspec
import psycopg

from noeta.backends.stores import postgres_snapshots as snapshots
from noeta.backends.stores._rows import (
    TASK_COLUMNS,
    encode,
    event_decoder,
    inbox_decoder,
    inbox_kind,
    task_row,
)
from noeta.backends.stores.postgres_conn import Connections
from noeta.backends.stores.postgres_feed import ChangeFeed
from noeta.core.errors import Conflict, StoreUnavailable, UnknownTask
from noeta.core.events import Event, InboxItem, Recorded, event_type
from noeta.core.store import (
    Appended,
    ChangeKind,
    Claim,
    NewTask,
    OnChange,
    Patch,
    Post,
    Snapshot,
    Status,
    TaskRow,
    hold_lost,
    next_row_state,
)
from noeta.core.types import Delta

Conn = psycopg.AsyncConnection[Any]

log = logging.getLogger("noeta.backends.stores.postgres")

#: 2: holds moved to ``tree_holds`` (one holding worker per tree), and
#: ``tasks.tenant``, ``tenant_usage`` and ``snapshots`` were added. 3:
#: ``snapshots`` keyed by task and fold version. A store at a later version
#: than this code knows is refused.
SCHEMA_VERSION = 3
_LOCK_CLASS = 0x6E6F6574  # "noet": the advisory-lock namespace for schema setup
_TENANT_LOCK_CLASS = 0x6E6F7465  # "note": one claim at a time per limited tenant
_HOLD_LOCK_CLASS = 0x6E6F7468  # "noth": one hold taken at a time per tree; claims share it
_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
_NOW = "EXTRACT(EPOCH FROM transaction_timestamp())::float8"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS {s}.noeta_schema (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS {s}.tasks (
    task_id        TEXT PRIMARY KEY,
    parent_id      TEXT,
    root_id        TEXT NOT NULL,
    agent          TEXT NOT NULL,
    queue          TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('ready','running','waiting','ended')),
    waiting_on     TEXT,
    ready_at       DOUBLE PRECISION,
    owner          TEXT,
    owner_epoch    BIGINT NOT NULL DEFAULT 0,
    lease_until    DOUBLE PRECISION,
    last_seq       BIGINT NOT NULL,
    inbox_seq      BIGINT NOT NULL DEFAULT 0,
    inbox_read_seq BIGINT NOT NULL DEFAULT 0,
    end_status     TEXT,
    dedupe_key     TEXT UNIQUE,
    created_at     DOUBLE PRECISION NOT NULL,
    updated_at     DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_claimable ON {s}.tasks (queue, updated_at)
    WHERE status <> 'ended';
CREATE INDEX IF NOT EXISTS tasks_parent ON {s}.tasks (parent_id);
CREATE INDEX IF NOT EXISTS tasks_root ON {s}.tasks (root_id);
CREATE TABLE IF NOT EXISTS {s}.events (
    task_id TEXT NOT NULL,
    seq     BIGINT NOT NULL,
    type    TEXT NOT NULL,
    at      DOUBLE PRECISION NOT NULL,
    data    BYTEA NOT NULL,
    PRIMARY KEY (task_id, seq)
);
CREATE TABLE IF NOT EXISTS {s}.inbox (
    task_id    TEXT NOT NULL,
    seq        BIGINT NOT NULL,
    kind       TEXT NOT NULL,
    at         DOUBLE PRECISION NOT NULL,
    data       BYTEA NOT NULL,
    dedupe_key TEXT,
    PRIMARY KEY (task_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS inbox_dedupe ON {s}.inbox (task_id, dedupe_key)
    WHERE dedupe_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS {s}.tree_holds (
    epoch   BIGSERIAL PRIMARY KEY,
    root_id TEXT NOT NULL,
    worker  TEXT NOT NULL,
    until   DOUBLE PRECISION NOT NULL,
    UNIQUE (root_id, worker)
);
CREATE INDEX IF NOT EXISTS tree_holds_until ON {s}.tree_holds (until);
CREATE TABLE IF NOT EXISTS {s}.snapshots (
    task_id TEXT NOT NULL,
    seq     BIGINT NOT NULL,
    version TEXT NOT NULL,
    data    BYTEA NOT NULL,
    PRIMARY KEY (task_id, version)
);
"""

#: Added after the tables were first created; existing rows take the
#: default, nothing is ever dropped.
_ADDED = """
ALTER TABLE {s}.tasks ADD COLUMN IF NOT EXISTS tenant TEXT NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS tasks_tenant ON {s}.tasks (tenant, status);
"""


_TABLES = """
CREATE TABLE IF NOT EXISTS {s}.tenant_usage (
    tenant   TEXT NOT NULL,
    period   TEXT NOT NULL,
    tokens   BIGINT NOT NULL DEFAULT 0,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant, period)
);
"""


class _HeldNow(Exception):
    """A claim's tree was held by another worker after all."""


class _ClaimInFlight(Exception):
    """A hold found a lapsed claim of its tree locked by a claim or renewal
    in flight: rolled back so that one can finish (and hold's lock not
    wait on a row a claim waiting on hold's lock has)."""


class _TenantFull(Exception):
    """A claim found its tenant at its running limit after all."""

    def __init__(self, tenant: str) -> None:
        super().__init__(tenant)
        self.tenant = tenant


class PostgresStore:
    """``Store`` on Postgres (psycopg 3, async).

    ``schema`` holds the tables (created on first use); the change channel
    is ``<schema>_changes``. ``pool_size`` caps this process's connections
    besides the one ``LISTEN`` connection: a worker runs many tasks at once
    and a lease renewal must not queue behind another task's write.
    """

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "noeta",
        pool_size: int = 8,
        connect_timeout: float = 10.0,
        listen_check_interval: float = 15.0,
    ) -> None:
        if not _NAME.match(schema):
            raise ValueError(f"schema must match {_NAME.pattern}: {schema!r}")
        self.dsn = dsn
        self.schema = schema
        self.connect_timeout = connect_timeout
        self._feed = ChangeFeed(dsn, schema, connect_timeout=connect_timeout,
                                listen_check_interval=listen_check_interval)
        self._conns = Connections(dsn, name=f"noeta-{schema}", pool_size=pool_size,
                                  connect_timeout=connect_timeout, feed=self._feed,
                                  setup=self._create_schema)

    def _q(self, sql: str) -> str:
        return sql.replace("{s}", f'"{self.schema}"')

    # ------------------------------------------------------------ schema

    async def _create_schema(self, conn: Conn) -> None:
        async with conn.transaction():
            # Concurrent CREATE ... IF NOT EXISTS can still collide in the
            # catalog; one setup at a time per schema, across processes.
            await conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                               (_LOCK_CLASS, self.schema))
            await conn.execute(self._q('CREATE SCHEMA IF NOT EXISTS {s}'))
            await conn.execute(self._q(
                "CREATE TABLE IF NOT EXISTS {s}.noeta_schema (version INTEGER NOT NULL)"))
            cur = await conn.execute(self._q("SELECT max(version) FROM {s}.noeta_schema"))
            row = await cur.fetchone()
            version = row[0] if row else None
            if version is not None and version > SCHEMA_VERSION:
                raise StoreUnavailable(
                    f"schema {self.schema!r} is at version {version}; this noeta knows "
                    f"{SCHEMA_VERSION} (stop every worker and upgrade)")
            await conn.execute(self._q(_SCHEMA))
            cur = await conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = %s"
                " AND table_name = 'tasks' AND column_name = 'tenant'", (self.schema,))
            if await cur.fetchone() is None:  # ALTER locks the table; only when needed
                await conn.execute(self._q(_ADDED))
            await conn.execute(self._q(_TABLES))
            if version is None:
                await conn.execute(self._q("INSERT INTO {s}.noeta_schema VALUES (%s)"),
                                   (SCHEMA_VERSION,))
            elif version < SCHEMA_VERSION:
                if version < 2:
                    await self._tell_version_1_holds(conn)
                else:  # version 2 kept one snapshot per task
                    await conn.execute(self._q(
                        "ALTER TABLE {s}.snapshots DROP CONSTRAINT snapshots_pkey,"
                        " ADD PRIMARY KEY (task_id, version)"))
                await conn.execute(self._q("UPDATE {s}.noeta_schema SET version = %s"),
                                   (SCHEMA_VERSION,))

    async def _tell_version_1_holds(self, conn: Conn) -> None:
        """Version 1 kept holds in ``holds``, which this version does not
        read: each tree held there is told the hold is gone (``hold_lost``
        with epoch 0, which no hold of ``tree_holds`` has). The table itself
        is kept."""
        cur = await conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = %s"
            " AND table_name = 'holds'", (self.schema,))
        if await cur.fetchone() is None:
            return
        cur = await conn.execute(self._q("SELECT root_id, worker FROM {s}.holds ORDER BY root_id"))
        changes: list[tuple[str, ChangeKind]] = []
        for root_id, worker in await cur.fetchall():
            await self._lost_posts(conn, str(root_id), str(worker), 0, changes)
        await self._notify(conn, changes)

    async def _notify(self, conn: Conn, changes: Sequence[tuple[str, ChangeKind]]) -> None:
        """Queue the notifications in the current transaction."""
        await self._feed.notify(conn, changes)

    def _deliver(self, changes: Sequence[tuple[str, ChangeKind]]) -> None:
        self._feed.deliver(changes)

    # ------------------------------------------------------------ change feed

    def on_change(self, callback: OnChange) -> Callable[[], None]:
        return self._feed.on_change(callback)

    def on_delta(self, callback: Callable[[str, Delta], None]) -> Callable[[], None]:
        """Call ``callback(task_id, delta)`` with the live output other
        processes publish (:meth:`ChangeFeed.on_delta`)."""
        return self._feed.on_delta(callback)

    async def publish_deltas(self, deltas: Sequence[tuple[str, Delta]]) -> None:
        """Send live output to the other processes on this store
        (:meth:`on_delta`): best effort, never stored."""
        if deltas:
            await self._conns.tx(lambda conn: self._feed.send_deltas(conn, deltas), write=False)

    async def close(self) -> None:
        await self._conns.close()

    # ------------------------------------------------------------ helpers run inside a tx

    async def _insert_task(self, conn: Conn, task: NewTask) -> tuple[str, bool]:
        created = task.created
        cur = await conn.execute(self._q(
            "INSERT INTO {s}.tasks (task_id, parent_id, root_id, agent, queue, status, last_seq,"
            " inbox_seq, dedupe_key, created_at, updated_at, tenant)"
            f" VALUES (%s, %s, %s, %s, %s, 'ready', 1, %s, %s, {_NOW}, {_NOW}, %s)"
            " ON CONFLICT DO NOTHING RETURNING task_id"),
            (task.task_id, created.parent_id, created.root_id or task.task_id, created.agent,
             task.queue, len(task.inbox), task.dedupe_key, created.tenant))
        if await cur.fetchone() is None:
            if task.dedupe_key is not None:
                cur = await conn.execute(
                    self._q("SELECT task_id FROM {s}.tasks WHERE dedupe_key = %s"),
                    (task.dedupe_key,))
                hit = await cur.fetchone()
                if hit:
                    return str(hit[0]), False
            return task.task_id, False
        await conn.execute(self._q(
            f"INSERT INTO {{s}}.events (task_id, seq, type, at, data)"
            f" VALUES (%s, 1, %s, {_NOW}, %s)"),
            (task.task_id, event_type(created), encode(created)))
        if task.inbox:
            await conn.execute(self._q(
                f"INSERT INTO {{s}}.inbox (task_id, seq, kind, at, data)"
                f" SELECT %s, s, k, {_NOW}, d"
                " FROM unnest(%s::bigint[], %s::text[], %s::bytea[]) AS u(s, k, d)"),
                (task.task_id, list(range(1, len(task.inbox) + 1)),
                 [inbox_kind(b) for b in task.inbox], [encode(b) for b in task.inbox]))
        return task.task_id, True

    async def _exists(self, conn: Conn, task_id: str) -> bool:
        cur = await conn.execute(self._q("SELECT 1 FROM {s}.tasks WHERE task_id = %s"),
                                 (task_id,))
        return await cur.fetchone() is not None

    async def _forwarded(self, conn: Conn, post: Post) -> Post:
        """``post``, or the same post to its task's root when it asks to be
        forwarded and the task has ended (see ``Post.forward_if_ended``).
        The task's row stays locked to the end of the transaction, so its
        end cannot land between this check and the post."""
        if not post.forward_if_ended:
            return post
        cur = await conn.execute(self._q(
            "SELECT status, root_id FROM {s}.tasks WHERE task_id = %s FOR UPDATE"),
            (post.task_id,))
        row = await cur.fetchone()
        if row is None or row[0] != "ended" or row[1] == post.task_id:
            return post
        return msgspec.structs.replace(post, task_id=str(row[1]), forward_if_ended=False)

    async def _insert_post(self, conn: Conn, post: Post) -> tuple[int, bool]:
        cur = await conn.execute(
            self._q("SELECT inbox_seq FROM {s}.tasks WHERE task_id = %s FOR UPDATE"),
            (post.task_id,))
        row = await cur.fetchone()
        if row is None:
            raise UnknownTask(post.task_id)
        if post.dedupe_key is not None:
            cur = await conn.execute(
                self._q("SELECT seq FROM {s}.inbox WHERE task_id = %s AND dedupe_key = %s"),
                (post.task_id, post.dedupe_key))
            hit = await cur.fetchone()
            if hit:
                return int(hit[0]), False
        seq = int(row[0]) + 1
        await conn.execute(self._q(
            "WITH ins AS (INSERT INTO {s}.inbox (task_id, seq, kind, at, data, dedupe_key)"
            f" VALUES (%(t)s, %(seq)s, %(kind)s, {_NOW}, %(data)s, %(key)s))"
            f" UPDATE {{s}}.tasks SET inbox_seq = %(seq)s, updated_at = {_NOW},"
            " ready_at = CASE WHEN status = 'waiting' AND %(wake)s THEN NULL ELSE ready_at END,"
            " status = CASE WHEN status = 'waiting' AND %(wake)s THEN 'ready' ELSE status END"
            " WHERE task_id = %(t)s"),
            {"t": post.task_id, "seq": seq, "kind": inbox_kind(post.body),
             "data": encode(post.body), "key": post.dedupe_key, "wake": post.wake})
        return seq, True

    # ------------------------------------------------------------ Store

    async def create(self, task: NewTask) -> tuple[str, bool]:
        async def tx(conn: Conn) -> tuple[str, bool]:
            result = await self._insert_task(conn, task)
            if result[1]:
                await self._notify(conn, [(result[0], "created")])
            return result

        result = await self._conns.tx(tx)
        if result[1]:
            self._deliver([(result[0], "created")])
        return result

    async def append(
        self,
        claim: Claim,
        events: Sequence[Event],
        *,
        expected_seq: int,
        patch: Patch = Patch(),
        posts: Sequence[Post] = (),
        creates: Sequence[NewTask] = (),
    ) -> Appended:
        changes: list[tuple[str, ChangeKind]] = []

        async def tx(conn: Conn) -> Appended:
            changes.clear()  # a re-run starts over
            cur = await conn.execute(self._q(
                "SELECT status, owner_epoch, last_seq, inbox_seq, inbox_read_seq"
                " FROM {s}.tasks WHERE task_id = %s FOR UPDATE"), (claim.task_id,))
            row = await cur.fetchone()
            if row is None:
                raise UnknownTask(claim.task_id)
            status, epoch, last_seq, inbox_seq, read_seq = row
            if status != "running" or epoch != claim.epoch:
                raise Conflict(f"{claim.task_id}: claim epoch {claim.epoch} no longer holds")
            if last_seq != expected_seq:
                raise Conflict(
                    f"{claim.task_id}: expected seq {expected_seq}, log is at {last_seq}")
            new_status, waiting_on, ready_at, read_seq = next_row_state(patch, inbox_seq,
                                                                        read_seq)
            seq = int(last_seq) + len(events)
            if events:
                await conn.execute(self._q(
                    f"INSERT INTO {{s}}.events (task_id, seq, type, at, data)"
                    f" SELECT %s, s, t, {_NOW}, d"
                    " FROM unnest(%s::bigint[], %s::text[], %s::bytea[]) AS u(s, t, d)"),
                    (claim.task_id, list(range(int(last_seq) + 1, seq + 1)),
                     [event_type(e) for e in events], [encode(e) for e in events]))
            releasing = new_status != "running"
            if new_status == "ended":
                await snapshots.delete(conn, self._q, claim.task_id)
            cur = await conn.execute(self._q(
                "UPDATE {s}.tasks SET last_seq = %s, status = %s, waiting_on = %s,"
                " ready_at = %s, inbox_read_seq = %s, end_status = COALESCE(%s, end_status),"
                f" updated_at = {_NOW}"
                + (", owner = NULL, lease_until = NULL" if releasing else "")
                + " WHERE task_id = %s RETURNING updated_at"),
                (seq, new_status, waiting_on,
                 ready_at, read_seq, patch.end_status, claim.task_id))
            stamped = await cur.fetchone()
            assert stamped is not None  # the row was locked above
            changes.append((claim.task_id, "appended"))
            for task in creates:
                task_id, created = await self._insert_task(conn, task)
                if created:
                    changes.append((task_id, "created"))
            for post in posts:
                if not await self._exists(conn, post.task_id):
                    continue  # a notice for a task that does not exist is dropped
                post = await self._forwarded(conn, post)
                _, created = await self._insert_post(conn, post)
                if created:
                    changes.append((post.task_id, "posted"))
            await self._notify(conn, changes)
            return Appended(seq=seq, at=float(stamped[0]))

        done = await self._conns.tx(tx)
        self._deliver(changes)
        return done

    async def read(self, task_id: str, after: int = 0) -> list[Recorded]:
        async def q(conn: Conn) -> list[Recorded]:
            cur = await conn.execute(self._q(
                "SELECT seq, at, data FROM {s}.events WHERE task_id = %s AND seq > %s"
                " ORDER BY seq"), (task_id, after))
            return [Recorded(seq=s, at=a, event=event_decoder.decode(d))
                    for s, a, d in await cur.fetchall()]

        return await self._conns.tx(q, write=False)

    async def post(self, post: Post) -> tuple[int, bool]:
        async def tx(conn: Conn) -> tuple[Post, tuple[int, bool]]:
            to = await self._forwarded(conn, post)
            result = await self._insert_post(conn, to)
            if result[1]:
                await self._notify(conn, [(to.task_id, "posted")])
            return to, result

        to, result = await self._conns.tx(tx)
        if result[1]:
            self._deliver([(to.task_id, "posted")])
        return result

    async def inbox(self, task_id: str, after: int = 0) -> list[InboxItem]:
        async def q(conn: Conn) -> list[InboxItem]:
            cur = await conn.execute(self._q(
                "SELECT seq, at, data FROM {s}.inbox WHERE task_id = %s AND seq > %s"
                " ORDER BY seq"), (task_id, after))
            return [InboxItem(seq=s, at=a, body=inbox_decoder.decode(d))
                    for s, a, d in await cur.fetchall()]

        return await self._conns.tx(q, write=False)

    async def get(self, task_id: str) -> TaskRow | None:
        async def q(conn: Conn) -> TaskRow | None:
            cur = await conn.execute(
                self._q(f"SELECT {TASK_COLUMNS} FROM {{s}}.tasks WHERE task_id = %s"),
                (task_id,))
            row = await cur.fetchone()
            return task_row(row) if row else None

        return await self._conns.tx(q, write=False)

    async def tasks(
        self,
        *,
        parent_id: str | None = None,
        status: Status | None = None,
        limit: int = 100,
        tenant: str | None = None,
    ) -> list[TaskRow]:
        where: list[str] = []
        args: list[Any] = []
        if parent_id is not None:
            where.append("parent_id = %s")
            args.append(parent_id)
        if tenant is not None:
            where.append("tenant = %s")
            args.append(tenant)
        if status is not None:
            where.append("status = %s")
            args.append(status)
        sql = f"SELECT {TASK_COLUMNS} FROM {{s}}.tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT %s"

        async def q(conn: Conn) -> list[TaskRow]:
            cur = await conn.execute(self._q(sql), (*args, limit))
            return [task_row(r) for r in await cur.fetchall()]

        return await self._conns.tx(q, write=False)

    async def claim(
        self, worker: str, queues: Sequence[str], lease_seconds: float,
        *, roots: Sequence[str] | None = None,
        tenant_limits: Mapping[str, int] | None = None,
    ) -> Claim | None:
        if not queues or (roots is not None and not roots):
            return None
        in_roots = " AND root_id = ANY(%(r)s)" if roots is not None else ""
        limits = dict(tenant_limits or {})

        async def running(conn: Conn, tenant: str, besides: str = "") -> int:
            cur = await conn.execute(self._q(
                "SELECT count(*) FROM {s}.tasks WHERE tenant = %s AND status = 'running'"
                f" AND lease_until >= {_NOW} AND task_id <> %s"), (tenant, besides))
            row = await cur.fetchone()
            return int(row[0]) if row else 0

        #: Tenants a try found at their limit after all: the next try skips them.
        found_full: set[str] = set()

        async def tx(conn: Conn) -> Claim | None:
            full = [t for t, most in limits.items()
                    if t in found_full or await running(conn, t) >= most]
            cur = await conn.execute(self._q(
                "WITH c AS (SELECT task_id, status AS was FROM {s}.tasks k"
                " WHERE queue = ANY(%(q)s)" + in_roots
                + (" AND (status = 'running' OR tenant <> ALL(%(full)s))" if full else "")
                + " AND ("
                f" (status = 'ready' AND (ready_at IS NULL OR ready_at <= {_NOW}))"
                f" OR (status = 'waiting' AND ready_at IS NOT NULL AND ready_at <= {_NOW})"
                f" OR (status = 'running' AND lease_until < {_NOW}))"
                " AND NOT EXISTS (SELECT 1 FROM {s}.tree_holds h WHERE h.root_id = k.root_id"
                " AND h.worker <> %(w)s)"
                " ORDER BY updated_at LIMIT 1 FOR UPDATE OF k SKIP LOCKED)"
                " UPDATE {s}.tasks t SET status = 'running', owner = %(w)s,"
                f" owner_epoch = t.owner_epoch + 1, lease_until = {_NOW} + %(ls)s,"
                f" ready_at = NULL, waiting_on = NULL, updated_at = {_NOW}"
                " FROM c WHERE t.task_id = c.task_id"
                " RETURNING t.task_id, t.owner_epoch, t.lease_until, t.tenant, c.was, t.root_id,"
                " t.queue"),
                {"q": list(queues), "w": worker, "ls": lease_seconds,
                 "r": list(roots or []), "full": full})
            row = await cur.fetchone()
            if row is None:
                return None
            task_id, tenant = str(row[0]), str(row[3])
            # A hold of this tree being taken now: wait for it, then look
            # again (a fresh statement sees what it committed).
            await conn.execute("SELECT pg_advisory_xact_lock_shared(%s, hashtext(%s))",
                               (_HOLD_LOCK_CLASS, f"{self.schema}:{row[5]}"))
            cur = await conn.execute(self._q(
                "SELECT 1 FROM {s}.tree_holds WHERE root_id = %s AND worker <> %s"),
                (row[5], worker))
            if await cur.fetchone() is not None:
                raise _HeldNow
            if tenant in limits and row[4] != "running":
                # Workers claiming at once each counted before the others
                # committed: one at a time per tenant, count again.
                await conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                                   (_TENANT_LOCK_CLASS, f"{self.schema}:{tenant}"))
                if await running(conn, tenant, besides=task_id) >= limits[tenant]:
                    raise _TenantFull(tenant)
            return Claim(task_id=task_id, worker=worker, epoch=int(row[1]),
                         lease_until=float(row[2]), queue=str(row[6]))

        while True:
            try:
                return await self._conns.tx(tx)
            except _HeldNow:
                continue  # rolled back; the committed hold now keeps the tree out
            except _TenantFull as full:
                # Rolled back: the task stays where it was. Another tenant
                # may have work; try again without this one.
                found_full.add(full.tenant)

    async def add_tenant_usage(self, tenant: str, period: str, tokens: int,
                               cost_usd: float) -> None:
        async def tx(conn: Conn) -> None:
            await conn.execute(self._q(
                "INSERT INTO {s}.tenant_usage AS u (tenant, period, tokens, cost_usd)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (tenant, period) DO UPDATE SET"
                " tokens = u.tokens + excluded.tokens,"
                " cost_usd = u.cost_usd + excluded.cost_usd"), (tenant, period, tokens, cost_usd))

        await self._conns.tx(tx)

    async def tenant_usage(self, tenant: str, period: str) -> tuple[int, float]:
        async def q(conn: Conn) -> tuple[int, float]:
            cur = await conn.execute(self._q(
                "SELECT tokens, cost_usd FROM {s}.tenant_usage WHERE tenant = %s"
                " AND period = %s"), (tenant, period))
            row = await cur.fetchone()
            return (int(row[0]), float(row[1])) if row else (0, 0.0)

        return await self._conns.tx(q, write=False)

    async def renew(self, claim: Claim, lease_seconds: float) -> Claim | None:
        async def tx(conn: Conn) -> Claim | None:
            cur = await conn.execute(self._q(
                f"UPDATE {{s}}.tasks SET lease_until = {_NOW} + %s WHERE task_id = %s"
                " AND owner_epoch = %s AND status = 'running' RETURNING lease_until"),
                (lease_seconds, claim.task_id, claim.epoch))
            row = await cur.fetchone()
            if row is None:
                return None
            return Claim(task_id=claim.task_id, worker=claim.worker, epoch=claim.epoch,
                         lease_until=float(row[0]), queue=claim.queue)

        return await self._conns.tx(tx)

    async def hold(self, root_id: str, worker: str, seconds: float,
                   *, epoch: int | None = None,
                   queues: Sequence[str] | None = None) -> int | None:
        changes: list[tuple[str, ChangeKind]] = []

        async def tx(conn: Conn) -> int | None:
            changes.clear()  # a re-run starts over
            if epoch is not None:
                cur = await conn.execute(self._q(
                    f"UPDATE {{s}}.tree_holds SET until = {_NOW} + %s WHERE epoch = %s"
                    " AND root_id = %s AND worker = %s RETURNING epoch"),
                    (seconds, epoch, root_id, worker))
            else:
                # Two workers taking a hold at once would each find no other
                # holder; one at a time per tree, so a tree has one holder.
                await conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                                   (_HOLD_LOCK_CLASS, f"{self.schema}:{root_id}"))
                cur = await conn.execute(self._q(
                    "SELECT 1 FROM {s}.tree_holds WHERE root_id = %s AND worker <> %s"),
                    (root_id, worker))
                if await cur.fetchone() is not None:
                    return None
                if queues is not None:
                    cur = await conn.execute(self._q(
                        "SELECT 1 FROM {s}.tasks WHERE root_id = %s AND status <> 'ended'"
                        " AND queue <> ALL(%s) LIMIT 1"), (root_id, list(queues)))
                    if await cur.fetchone() is not None:
                        return None  # a task of the tree this worker could not claim
                await self._revoke_lapsed(conn, root_id, worker, changes)
                # A claim of the tree takes the same lock (shared), so one
                # that committed before this lock is seen here, and one
                # after sees this hold.
                cur = await conn.execute(self._q(
                    "SELECT 1 FROM {s}.tasks WHERE root_id = %s AND status = 'running'"
                    f" AND owner <> %s AND lease_until >= {_NOW} LIMIT 1"), (root_id, worker))
                if await cur.fetchone() is not None:
                    return None
                cur = await conn.execute(self._q(
                    "INSERT INTO {s}.tree_holds (root_id, worker, until)"
                    f" VALUES (%s, %s, {_NOW} + %s) ON CONFLICT (root_id, worker)"
                    " DO UPDATE SET until = excluded.until RETURNING epoch"),
                    (root_id, worker, seconds))
            row = await cur.fetchone()
            await self._notify(conn, changes)
            return int(row[0]) if row else None

        while True:
            try:
                held = await self._conns.tx(tx)
            except _ClaimInFlight:
                await asyncio.sleep(0.005)
                continue
            self._deliver(changes)
            return held

    async def _revoke_lapsed(self, conn: Conn, root_id: str, worker: str,
                             changes: list[tuple[str, ChangeKind]]) -> None:
        """Take back other workers' lapsed claims of the tree (a new epoch,
        back to ``ready``), so a worker that stalled past its lease cannot
        renew or write once this one holds the tree."""
        try:
            cur = await conn.execute(self._q(
                "SELECT task_id FROM {s}.tasks WHERE root_id = %s AND status = 'running'"
                f" AND owner <> %s AND lease_until < {_NOW} FOR UPDATE NOWAIT"),
                (root_id, worker))
        except psycopg.errors.LockNotAvailable as e:
            raise _ClaimInFlight from e
        lapsed = [str(r[0]) for r in await cur.fetchall()]
        if not lapsed:
            return
        await conn.execute(self._q(
            "UPDATE {s}.tasks SET status = 'ready', owner = NULL, lease_until = NULL,"
            f" owner_epoch = owner_epoch + 1, updated_at = {_NOW}"
            " WHERE task_id = ANY(%s)"), (lapsed,))
        changes.extend((t, "released") for t in lapsed)

    async def _lost_posts(self, conn: Conn, root_id: str, worker: str, epoch: int,
                          changes: list[tuple[str, ChangeKind]], *, wake: bool = True) -> None:
        """Post ``hold_lost`` to every unended task of the tree."""
        cur = await conn.execute(self._q(
            "SELECT task_id FROM {s}.tasks WHERE root_id = %s AND status <> 'ended'"
            " ORDER BY task_id"), (root_id,))
        for (task_id,) in await cur.fetchall():
            _, created = await self._insert_post(conn, hold_lost(str(task_id), worker, epoch,
                                                                     wake=wake))
            if created:
                changes.append((str(task_id), "posted"))

    async def unhold(self, root_id: str, worker: str, epoch: int,
                     *, lost: bool = False) -> None:
        changes: list[tuple[str, ChangeKind]] = []

        async def tx(conn: Conn) -> None:
            changes.clear()  # a re-run starts over
            cur = await conn.execute(self._q(
                "DELETE FROM {s}.tree_holds WHERE epoch = %s AND root_id = %s AND worker = %s"),
                (epoch, root_id, worker))
            if not cur.rowcount:
                return
            changes.append((root_id, "released"))
            if lost:
                await self._lost_posts(conn, root_id, worker, epoch, changes, wake=False)
            await self._notify(conn, changes)

        await self._conns.tx(tx)
        self._deliver(changes)

    async def reap_holds(self) -> int:
        changes: list[tuple[str, ChangeKind]] = []

        async def expired(conn: Conn) -> bool:
            cur = await conn.execute(self._q(
                f"SELECT 1 FROM {{s}}.tree_holds WHERE until < {_NOW} LIMIT 1"))
            return await cur.fetchone() is not None

        async def tx(conn: Conn) -> int:
            changes.clear()  # a re-run starts over
            cur = await conn.execute(self._q(
                "DELETE FROM {s}.tree_holds WHERE epoch IN (SELECT epoch FROM {s}.tree_holds"
                f" WHERE until < {_NOW} ORDER BY epoch FOR UPDATE SKIP LOCKED)"
                " RETURNING epoch, root_id, worker"))
            gone = sorted(await cur.fetchall())
            for epoch, root_id, worker in gone:
                changes.append((str(root_id), "released"))
                await self._lost_posts(conn, str(root_id), str(worker), int(epoch), changes)
            await self._notify(conn, changes)
            return len(gone)

        # Most rounds find nothing to reap: a read, then no write at all.
        if not await self._conns.tx(expired, write=False):
            return 0
        reaped = await self._conns.tx(tx)
        self._deliver(changes)
        return reaped

    async def snapshot(self, task_id: str, version: str | None = None) -> Snapshot | None:
        return await self._conns.tx(
            lambda conn: snapshots.read(conn, self._q, task_id, version), write=False)

    async def put_snapshot(self, task_id: str, snapshot: Snapshot) -> None:
        await self._conns.tx(lambda conn: snapshots.write(conn, self._q, task_id, snapshot))

    async def release(self, claim: Claim) -> None:
        async def tx(conn: Conn) -> bool:
            cur = await conn.execute(self._q(
                "UPDATE {s}.tasks SET status = 'ready', owner = NULL, lease_until = NULL,"
                f" updated_at = {_NOW} WHERE task_id = %s AND owner_epoch = %s"
                " AND status = 'running'"), (claim.task_id, claim.epoch))
            if cur.rowcount:
                await self._notify(conn, [(claim.task_id, "released")])
            return cur.rowcount > 0

        if await self._conns.tx(tx):
            self._deliver([(claim.task_id, "released")])


def open_url(url: str) -> PostgresStore:
    """``postgres://...`` -> a store. The ``postgres`` / ``postgresql`` entry
    points of ``noeta.stores``."""
    return PostgresStore(url)
