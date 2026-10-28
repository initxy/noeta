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

Changes are announced with ``pg_notify`` inside the same transaction, so a
notification exists exactly when the change committed. A dedicated
``LISTEN`` connection (reopened when it drops) feeds ``on_change`` in every
process. This process's own changes are reported synchronously after commit
and their echoes are dropped by backend pid. Notifications sent while the
listener was reconnecting are lost; polling covers that gap.

Live model output (``publish_deltas`` / ``on_delta``) rides a second
channel, ``<schema>_deltas``, outside any task transaction, heard only by
processes that subscribe: best effort, never stored, each notification
under Postgres' 8000-byte payload limit.

Connection loss: a connection that drops before ``COMMIT`` is sent leaves
nothing behind, so the transaction is re-run once on a fresh connection. A
drop while ``COMMIT`` is in flight raises ``CommitUnknown`` (a ``Conflict``)
and is never re-run; the pool replaces the broken connection for the next
call.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import weakref
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

import msgspec
import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from noeta.backends.stores._rows import (
    TASK_COLUMNS,
    encode,
    event_decoder,
    inbox_decoder,
    inbox_kind,
    task_row,
)
from noeta.core.errors import CommitUnknown, Conflict, StoreUnavailable, UnknownTask
from noeta.core.events import Event, InboxItem, Recorded, event_type
from noeta.core.store import (
    Appended,
    ChangeKind,
    Claim,
    NewTask,
    OnChange,
    Patch,
    Post,
    Status,
    TaskRow,
    hold_lost,
    next_row_state,
)
from noeta.core.types import Delta

T = TypeVar("T")
Conn = psycopg.AsyncConnection[Any]

log = logging.getLogger("noeta.backends.stores.postgres")

SCHEMA_VERSION = 1
_LOCK_CLASS = 0x6E6F6574  # "noet": the advisory-lock namespace for schema setup
_RETRY_SQLSTATES = frozenset({"40001", "40P01"})  # serialization failure, deadlock
_LOST_SQLSTATES = frozenset({"57P01", "57P02", "57P03"})
_DEADLOCK_RETRIES = 5
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
"""

#: Bytes of one delta notification's payload (Postgres refuses 8000 and up).
_DELTA_PAYLOAD_BYTES = 7000
_delta_batch = msgspec.json.Decoder(list[tuple[str, Delta]])


def _delta_payloads(deltas: Sequence[tuple[str, Delta]]) -> list[str]:
    """``deltas`` as JSON payloads under the notification limit, in order;
    a fragment too long for one is cut into several."""
    pieces: list[tuple[str, Delta]] = []
    for task_id, d in deltas:
        text = d.text
        step = _DELTA_PAYLOAD_BYTES // 8  # a character takes at most 6 bytes as JSON
        for i in range(0, max(len(text), 1), step):
            pieces.append((task_id, msgspec.structs.replace(d, text=text[i:i + step])))
    out: list[str] = []
    batch: list[tuple[str, Delta]] = []
    size = 2
    for piece in pieces:
        n = len(msgspec.json.encode(piece)) + 1
        if batch and size + n > _DELTA_PAYLOAD_BYTES:
            out.append(msgspec.json.encode(batch).decode())
            batch, size = [], 2
        batch.append(piece)
        size += n
    if batch:
        out.append(msgspec.json.encode(batch).decode())
    return out


def _lost(conn: Conn | None, exc: BaseException) -> bool:
    """Whether ``exc`` means the connection is gone (not a query error)."""
    if not isinstance(exc, psycopg.OperationalError | psycopg.InterfaceError):
        return False
    if conn is not None and (conn.closed or conn.broken):
        return True
    state = getattr(exc, "sqlstate", None) or ""
    return state.startswith("08") or state in _LOST_SQLSTATES


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
        self.channel = f"{schema}_changes"
        self.delta_channel = f"{schema}_deltas"
        self.connect_timeout = connect_timeout
        self.listen_check_interval = listen_check_interval
        self._pool = AsyncConnectionPool(
            dsn,
            min_size=1,
            max_size=max(pool_size, 1),
            open=False,
            kwargs={"autocommit": True},
            configure=self._configure,
            check=AsyncConnectionPool.check_connection,
            timeout=connect_timeout,
            name=f"noeta-{schema}",
        )
        self._open_lock = asyncio.Lock()
        self._opened = False
        self._closed = False
        self._listener: asyncio.Task[None] | None = None
        self._delta_listener: asyncio.Task[None] | None = None
        self._lock = threading.Lock()
        self._listeners: list[tuple[OnChange, asyncio.AbstractEventLoop | None]] = []
        self._delta_listeners: list[Callable[[str, Delta], None]] = []
        #: Backend pids of this store's pool connections: their notifications
        #: were already reported locally. Weak, so a closed connection's pid
        #: (which the server may reuse) drops out.
        self._own: weakref.WeakValueDictionary[int, Conn] = weakref.WeakValueDictionary()

    def _q(self, sql: str) -> str:
        return sql.replace("{s}", f'"{self.schema}"')

    # ------------------------------------------------------------ connections

    async def _configure(self, conn: Conn) -> None:
        self._own[conn.info.backend_pid] = conn

    async def _ensure_open(self) -> None:
        if self._opened:
            return
        async with self._open_lock:
            if self._opened:
                return
            if self._closed:
                raise StoreUnavailable("the store is closed")
            try:
                await self._pool.open(wait=True, timeout=self.connect_timeout)
                async with self._pool.connection() as conn:
                    await self._create_schema(conn)
                listen = await self._listen_connection()
            except (PoolTimeout, psycopg.OperationalError) as e:
                raise StoreUnavailable(f"cannot open the postgres store: {e}") from e
            self._listener = asyncio.get_running_loop().create_task(self._listen(listen))
            self._opened = True

    async def _create_schema(self, conn: Conn) -> None:
        async with conn.transaction():
            # Concurrent CREATE ... IF NOT EXISTS can still collide in the
            # catalog; one setup at a time per schema, across processes.
            await conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                               (_LOCK_CLASS, self.schema))
            await conn.execute(self._q('CREATE SCHEMA IF NOT EXISTS {s}'))
            await conn.execute(self._q(_SCHEMA))
            cur = await conn.execute(self._q("SELECT max(version) FROM {s}.noeta_schema"))
            row = await cur.fetchone()
            version = row[0] if row else None
            if version is None:
                await conn.execute(self._q("INSERT INTO {s}.noeta_schema VALUES (%s)"),
                                   (SCHEMA_VERSION,))
            elif version > SCHEMA_VERSION:
                raise StoreUnavailable(
                    f"schema {self.schema!r} is at version {version}; this noeta knows "
                    f"{SCHEMA_VERSION}")

    async def _tx(self, body: Callable[[Conn], Awaitable[T]], *, write: bool = True) -> T:
        """Run ``body`` in one transaction (or, for reads, one autocommit
        statement batch), with the retry rules in the module docstring."""
        await self._ensure_open()
        dropped = deadlocks = 0
        while True:
            conn: Conn | None = None
            committing = False
            try:
                async with self._pool.connection() as conn:
                    if not write:
                        return await body(conn)
                    async with conn.transaction():
                        result = await body(conn)
                        committing = True
                    return result
            except PoolTimeout as e:
                raise StoreUnavailable(f"no postgres connection: {e}") from e
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                if _lost(conn, e):
                    if committing:
                        raise CommitUnknown(f"connection lost during COMMIT: {e}") from e
                    if dropped:
                        raise StoreUnavailable(f"postgres connection lost: {e}") from e
                    dropped += 1
                    continue
                if getattr(e, "sqlstate", None) in _RETRY_SQLSTATES and \
                        deadlocks < _DEADLOCK_RETRIES:
                    deadlocks += 1
                    await asyncio.sleep(0.005 * deadlocks)
                    continue
                # Any other operational failure (a cancelled statement, a full
                # disk): nothing committed; the caller may try again later.
                raise StoreUnavailable(f"postgres: {e}") from e

    async def _notify(self, conn: Conn, changes: Sequence[tuple[str, ChangeKind]]) -> None:
        """Queue the notifications in the current transaction."""
        if changes:
            await conn.execute("SELECT pg_notify(%s, c) FROM unnest(%s::text[]) AS c",
                               (self.channel, [f"{t}:{k}" for t, k in changes]))

    # ------------------------------------------------------------ change feed

    async def _listen_connection(self, channel: str | None = None) -> Conn:
        conn = await psycopg.AsyncConnection.connect(
            self.dsn, autocommit=True, connect_timeout=max(int(self.connect_timeout), 1))
        await conn.execute(f'LISTEN "{channel or self.channel}"')
        return conn

    async def _listen(self, conn: Conn | None, channel: str | None = None,
                      handle: Callable[[int, str], None] | None = None) -> None:
        handle = handle or self._remote
        backoff = 0.1
        while True:
            try:
                if conn is None:
                    conn = await self._listen_connection(channel)
                    backoff = 0.1
                while True:
                    async for n in conn.notifies(timeout=self.listen_check_interval):
                        handle(n.pid, n.payload)
                    # Quiet for a while: make sure the connection is alive.
                    await conn.execute("SELECT 1")
            except asyncio.CancelledError:
                if conn is not None:
                    await conn.close()
                raise
            except Exception as e:  # noqa: BLE001 — any failure means reconnect
                log.warning("noeta change listener lost its connection (%s); reconnecting", e)
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.close()
                conn = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    def _remote(self, pid: int, payload: str) -> None:
        own = self._own.get(pid)
        if own is not None and not own.closed:
            return  # already reported by this process
        task_id, _, kind = payload.rpartition(":")
        if task_id and kind in ("appended", "posted", "created", "released"):
            self._deliver([(task_id, kind)])  # type: ignore[list-item]

    def _remote_deltas(self, pid: int, payload: str) -> None:
        own = self._own.get(pid)
        if own is not None and not own.closed:
            return  # this process's own output reached its sinks directly
        try:
            items = _delta_batch.decode(payload)
        except msgspec.DecodeError:
            log.warning("dropped a malformed delta notification")
            return
        for task_id, delta in items:
            for cb in list(self._delta_listeners):
                try:
                    cb(task_id, delta)
                except Exception:
                    log.exception("on_delta listener failed")

    async def publish_deltas(self, deltas: Sequence[tuple[str, Delta]]) -> None:
        """Send live output to the other processes on this store
        (:meth:`on_delta`): best effort, never stored."""
        payloads = _delta_payloads(deltas)
        if not payloads:
            return

        async def send(conn: Conn) -> None:
            await conn.execute("SELECT pg_notify(%s, p) FROM unnest(%s::text[]) AS p",
                               (self.delta_channel, payloads))

        await self._tx(send, write=False)

    def on_delta(self, callback: Callable[[str, Delta], None]) -> Callable[[], None]:
        """Call ``callback(task_id, delta)`` with the live output other
        processes publish; returns the unsubscribe function. Its own
        ``LISTEN`` connection opens with the first callback, so a process
        nobody watches through never receives output."""
        self._delta_listeners.append(callback)
        if self._delta_listener is None and not self._closed:
            self._delta_listener = asyncio.get_running_loop().create_task(
                self._listen(None, self.delta_channel, self._remote_deltas))

        def unsubscribe() -> None:
            if callback in self._delta_listeners:
                self._delta_listeners.remove(callback)

        return unsubscribe

    def _deliver(self, changes: Sequence[tuple[str, ChangeKind]]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        try:
            here: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        for task_id, kind in changes:
            for cb, loop in listeners:
                if loop is None or loop is here:
                    try:
                        cb(task_id, kind)
                    except Exception:
                        log.exception("on_change listener failed")
                elif not loop.is_closed():
                    loop.call_soon_threadsafe(cb, task_id, kind)

    def on_change(self, callback: OnChange) -> Callable[[], None]:
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        entry = (callback, loop)
        with self._lock:
            self._listeners.append(entry)

        def unsubscribe() -> None:
            with self._lock:
                if entry in self._listeners:
                    self._listeners.remove(entry)

        return unsubscribe

    async def close(self) -> None:
        self._closed = True
        for listening in (self._listener, self._delta_listener):
            if listening is not None:
                listening.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listening
        self._listener = self._delta_listener = None
        await self._pool.close()
        self._opened = False

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
            " ready_at = CASE WHEN status = 'waiting' THEN NULL ELSE ready_at END,"
            " status = CASE WHEN status = 'waiting' THEN 'ready' ELSE status END"
            " WHERE task_id = %(t)s"),
            {"t": post.task_id, "seq": seq, "kind": inbox_kind(post.body),
             "data": encode(post.body), "key": post.dedupe_key})
        return seq, True

    # ------------------------------------------------------------ Store

    async def create(self, task: NewTask) -> tuple[str, bool]:
        async def tx(conn: Conn) -> tuple[str, bool]:
            result = await self._insert_task(conn, task)
            if result[1]:
                await self._notify(conn, [(result[0], "created")])
            return result

        result = await self._tx(tx)
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
            seq = int(last_seq) + len(events)
            if events:
                await conn.execute(self._q(
                    f"INSERT INTO {{s}}.events (task_id, seq, type, at, data)"
                    f" SELECT %s, s, t, {_NOW}, d"
                    " FROM unnest(%s::bigint[], %s::text[], %s::bytea[]) AS u(s, t, d)"),
                    (claim.task_id, list(range(int(last_seq) + 1, seq + 1)),
                     [event_type(e) for e in events], [encode(e) for e in events]))
            new_status, waiting_on, ready_at, read_seq = next_row_state(patch, inbox_seq,
                                                                        read_seq)
            releasing = new_status != "running"
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
                _, created = await self._insert_post(conn, post)
                if created:
                    changes.append((post.task_id, "posted"))
            await self._notify(conn, changes)
            return Appended(seq=seq, at=float(stamped[0]))

        done = await self._tx(tx)
        self._deliver(changes)
        return done

    async def read(self, task_id: str, after: int = 0) -> list[Recorded]:
        async def q(conn: Conn) -> list[Recorded]:
            cur = await conn.execute(self._q(
                "SELECT seq, at, data FROM {s}.events WHERE task_id = %s AND seq > %s"
                " ORDER BY seq"), (task_id, after))
            return [Recorded(seq=s, at=a, event=event_decoder.decode(d))
                    for s, a, d in await cur.fetchall()]

        return await self._tx(q, write=False)

    async def post(self, post: Post) -> tuple[int, bool]:
        async def tx(conn: Conn) -> tuple[int, bool]:
            result = await self._insert_post(conn, post)
            if result[1]:
                await self._notify(conn, [(post.task_id, "posted")])
            return result

        result = await self._tx(tx)
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

        return await self._tx(q, write=False)

    async def get(self, task_id: str) -> TaskRow | None:
        async def q(conn: Conn) -> TaskRow | None:
            cur = await conn.execute(
                self._q(f"SELECT {TASK_COLUMNS} FROM {{s}}.tasks WHERE task_id = %s"),
                (task_id,))
            row = await cur.fetchone()
            return task_row(row) if row else None

        return await self._tx(q, write=False)

    async def tasks(
        self,
        *,
        parent_id: str | None = None,
        status: Status | None = None,
        limit: int = 100,
    ) -> list[TaskRow]:
        where: list[str] = []
        args: list[Any] = []
        if parent_id is not None:
            where.append("parent_id = %s")
            args.append(parent_id)
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

        return await self._tx(q, write=False)

    async def claim(
        self, worker: str, queues: Sequence[str], lease_seconds: float,
        *, roots: Sequence[str] | None = None,
    ) -> Claim | None:
        if not queues or (roots is not None and not roots):
            return None
        in_roots = " AND root_id = ANY(%(r)s)" if roots is not None else ""

        async def tx(conn: Conn) -> Claim | None:
            cur = await conn.execute(self._q(
                "WITH c AS (SELECT task_id FROM {s}.tasks k WHERE queue = ANY(%(q)s)"
                + in_roots + " AND ("
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
                " RETURNING t.task_id, t.owner_epoch, t.lease_until"),
                {"q": list(queues), "w": worker, "ls": lease_seconds,
                 "r": list(roots or [])})
            row = await cur.fetchone()
            if row is None:
                return None
            return Claim(task_id=str(row[0]), worker=worker, epoch=int(row[1]),
                         lease_until=float(row[2]))

        return await self._tx(tx)

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
                         lease_until=float(row[0]))

        return await self._tx(tx)

    async def hold(self, root_id: str, worker: str, seconds: float,
                   *, epoch: int | None = None) -> int | None:
        async def tx(conn: Conn) -> int | None:
            if epoch is not None:
                cur = await conn.execute(self._q(
                    f"UPDATE {{s}}.tree_holds SET until = {_NOW} + %s WHERE epoch = %s"
                    " AND root_id = %s AND worker = %s RETURNING epoch"),
                    (seconds, epoch, root_id, worker))
            else:
                cur = await conn.execute(self._q(
                    "INSERT INTO {s}.tree_holds (root_id, worker, until)"
                    f" VALUES (%s, %s, {_NOW} + %s) ON CONFLICT (root_id, worker)"
                    " DO UPDATE SET until = excluded.until RETURNING epoch"),
                    (root_id, worker, seconds))
            row = await cur.fetchone()
            return int(row[0]) if row else None

        return await self._tx(tx)

    async def _lost_posts(self, conn: Conn, root_id: str, worker: str, epoch: int,
                          changes: list[tuple[str, ChangeKind]]) -> None:
        """Post ``hold_lost`` to every unended task of the tree."""
        cur = await conn.execute(self._q(
            "SELECT task_id FROM {s}.tasks WHERE root_id = %s AND status <> 'ended'"
            " ORDER BY task_id"), (root_id,))
        for (task_id,) in await cur.fetchall():
            _, created = await self._insert_post(conn, hold_lost(str(task_id), worker, epoch))
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
                await self._lost_posts(conn, root_id, worker, epoch, changes)
            await self._notify(conn, changes)

        await self._tx(tx)
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
        if not await self._tx(expired, write=False):
            return 0
        reaped = await self._tx(tx)
        self._deliver(changes)
        return reaped

    async def release(self, claim: Claim) -> None:
        async def tx(conn: Conn) -> bool:
            cur = await conn.execute(self._q(
                "UPDATE {s}.tasks SET status = 'ready', owner = NULL, lease_until = NULL,"
                f" updated_at = {_NOW} WHERE task_id = %s AND owner_epoch = %s"
                " AND status = 'running'"), (claim.task_id, claim.epoch))
            if cur.rowcount:
                await self._notify(conn, [(claim.task_id, "released")])
            return cur.rowcount > 0

        if await self._tx(tx):
            self._deliver([(claim.task_id, "released")])


def open_url(url: str) -> PostgresStore:
    """``postgres://...`` -> a store. The ``postgres`` / ``postgresql`` entry
    points of ``noeta.stores``."""
    return PostgresStore(url)
