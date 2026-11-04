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
  the fence; leases only decide when a silent owner may be replaced. The
  claim index holds only rows a claim can take (:data:`_CLAIMABLE`), so the
  scan never walks idle waiting tasks.
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

import logging
import re
from collections.abc import Callable, Sequence
from typing import Any

import psycopg

from noeta.backends.stores import postgres_snapshots as snapshots
from noeta.backends.stores._rows import (
    SCHEMA_VERSION,
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
    next_row_state,
)
from noeta.core.types import Delta
from noeta.toolkit.local_env import POSTGRES_SECRETS, withhold

Conn = psycopg.AsyncConnection[Any]

log = logging.getLogger("noeta.backends.stores.postgres")

_LOCK_CLASS = 0x6E6F6574  # "noet": the advisory-lock namespace for schema setup
_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
_NOW = "EXTRACT(EPOCH FROM transaction_timestamp())::float8"
#: The rows a claim can take, given a lease run out: the claim index's
#: predicate, repeated word for word in the claim so the planner uses it.
_CLAIMABLE = ("(status = 'ready' OR status = 'running'"
              " OR (status = 'waiting' AND (ready_at IS NOT NULL OR lease_until IS NOT NULL)))")

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
CREATE INDEX IF NOT EXISTS tasks_claim ON {s}.tasks (queue, updated_at)
    WHERE """ + _CLAIMABLE + """;
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
CREATE TABLE IF NOT EXISTS {s}.snapshots (
    task_id TEXT NOT NULL,
    seq     BIGINT NOT NULL,
    version TEXT NOT NULL,
    data    BYTEA NOT NULL,
    PRIMARY KEY (task_id, version)
);
"""

class PostgresStore:
    """``Store`` on Postgres (psycopg 3, async).

    ``schema`` holds the tables (created on first use); the change channel
    is ``<schema>_changes``, which every process hears (``shared_changes``).
    ``pool_size`` caps this process's connections
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
        self.shared_changes = True
        if not _NAME.match(schema):
            raise ValueError(f"schema must match {_NAME.pattern}: {schema!r}")
        # libpq logs in with these too: noeta's store credentials from now on.
        withhold(POSTGRES_SECRETS)
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
            if version is None:
                await conn.execute(self._q("INSERT INTO {s}.noeta_schema VALUES (%s)"),
                                   (SCHEMA_VERSION,))
            elif version < SCHEMA_VERSION:
                # Older holds tables (0.6's ``holds``, a development build's
                # ``tree_holds``) and the claim index over every unended
                # task go: a job they kept died with its worker, as the next
                # pickup of its task tells.
                await conn.execute(self._q(
                    "DROP TABLE IF EXISTS {s}.holds; DROP TABLE IF EXISTS {s}.tree_holds;"
                    " DROP INDEX IF EXISTS {s}.tasks_claimable"))
                await conn.execute(self._q("UPDATE {s}.noeta_schema SET version = %s"),
                                   (SCHEMA_VERSION,))

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
            " inbox_seq, dedupe_key, created_at, updated_at)"
            f" VALUES (%s, %s, %s, %s, %s, 'ready', 1, %s, %s, {_NOW}, {_NOW})"
            " ON CONFLICT DO NOTHING RETURNING task_id"),
            (task.task_id, created.parent_id, created.root_id or task.task_id, created.agent,
             task.queue, len(task.inbox), task.dedupe_key))
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
                "SELECT lease_until, owner_epoch, last_seq, inbox_seq, inbox_read_seq"
                " FROM {s}.tasks WHERE task_id = %s FOR UPDATE"), (claim.task_id,))
            row = await cur.fetchone()
            if row is None:
                raise UnknownTask(claim.task_id)
            lease, epoch, last_seq, inbox_seq, read_seq = row
            # Only a held claim has a lease: running, or kept over a wait.
            if lease is None or epoch != claim.epoch:
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
            releasing = new_status != "running" and not (patch.keep and new_status != "ended")
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
            if releasing and new_status == "ready":
                changes.append((claim.task_id, "released"))
            for task in creates:
                task_id, created = await self._insert_task(conn, task)
                if created:
                    changes.append((task_id, "created"))
            for post in posts:
                if not await self._exists(conn, post.task_id):
                    continue  # a notice for a task that does not exist is dropped
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
        async def tx(conn: Conn) -> tuple[int, bool]:
            result = await self._insert_post(conn, post)
            if result[1]:
                await self._notify(conn, [(post.task_id, "posted")])
            return result

        result = await self._conns.tx(tx)
        if result[1]:
            self._deliver([(post.task_id, "posted")])
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
        owner: str | None = None,
    ) -> list[TaskRow]:
        where: list[str] = []
        args: list[Any] = []
        if parent_id is not None:
            where.append("parent_id = %s")
            args.append(parent_id)
        if status is not None:
            where.append("status = %s")
            args.append(status)
        if owner is not None:
            where.append("owner = %s")
            args.append(owner)
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
    ) -> Claim | None:
        if not queues or (roots is not None and not roots):
            return None
        in_roots = " AND root_id = ANY(%(r)s)" if roots is not None else ""

        async def tx(conn: Conn) -> Claim | None:
            cur = await conn.execute(self._q(
                "WITH c AS (SELECT task_id FROM {s}.tasks k"
                f" WHERE queue = ANY(%(q)s) AND {_CLAIMABLE}" + in_roots
                + " AND ("
                f" (status = 'ready' AND (ready_at IS NULL OR ready_at <= {_NOW}))"
                f" OR (status = 'waiting' AND (ready_at <= {_NOW} OR lease_until IS NOT NULL))"
                " OR status = 'running')"
                f" AND (lease_until IS NULL OR lease_until < {_NOW})"
                " ORDER BY updated_at LIMIT 1 FOR UPDATE OF k SKIP LOCKED)"
                " UPDATE {s}.tasks t SET status = 'running', owner = %(w)s,"
                f" owner_epoch = t.owner_epoch + 1, lease_until = {_NOW} + %(ls)s,"
                f" ready_at = NULL, waiting_on = NULL, updated_at = {_NOW}"
                " FROM c WHERE t.task_id = c.task_id"
                " RETURNING t.task_id, t.owner_epoch, t.lease_until, t.queue"),
                {"q": list(queues), "w": worker, "ls": lease_seconds,
                 "r": list(roots or [])})
            row = await cur.fetchone()
            if row is None:
                return None
            return Claim(task_id=str(row[0]), worker=worker, epoch=int(row[1]),
                         lease_until=float(row[2]), queue=str(row[3]))

        return await self._conns.tx(tx)

    async def renew(self, claim: Claim, lease_seconds: float) -> Claim | None:
        async def tx(conn: Conn) -> Claim | None:
            cur = await conn.execute(self._q(
                f"UPDATE {{s}}.tasks SET lease_until = {_NOW} + %s WHERE task_id = %s"
                " AND owner_epoch = %s AND lease_until IS NOT NULL RETURNING lease_until"),
                (lease_seconds, claim.task_id, claim.epoch))
            row = await cur.fetchone()
            if row is None:
                return None
            return Claim(task_id=claim.task_id, worker=claim.worker, epoch=claim.epoch,
                         lease_until=float(row[0]), queue=claim.queue)

        return await self._conns.tx(tx)

    async def snapshot(self, task_id: str, version: str | None = None) -> Snapshot | None:
        return await self._conns.tx(
            lambda conn: snapshots.read(conn, self._q, task_id, version), write=False)

    async def put_snapshot(self, task_id: str, snapshot: Snapshot) -> None:
        await self._conns.tx(lambda conn: snapshots.write(conn, self._q, task_id, snapshot))

    async def release(self, claim: Claim) -> None:
        async def tx(conn: Conn) -> bool:
            cur = await conn.execute(self._q(
                "UPDATE {s}.tasks SET status = CASE status WHEN 'running' THEN 'ready'"
                " ELSE status END, owner = NULL, lease_until = NULL,"
                f" updated_at = {_NOW} WHERE task_id = %s AND owner_epoch = %s"
                " AND lease_until IS NOT NULL"), (claim.task_id, claim.epoch))
            if cur.rowcount:
                await self._notify(conn, [(claim.task_id, "released")])
            return cur.rowcount > 0

        if await self._conns.tx(tx):
            self._deliver([(claim.task_id, "released")])


def open_url(url: str) -> PostgresStore:
    """``postgres://...`` -> a store. The ``postgres`` / ``postgresql`` entry
    points of ``noeta.stores``."""
    return PostgresStore(url)
