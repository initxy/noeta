"""SQLite store. One connection, used from one worker thread, so the async
methods never block the event loop and never share a connection across
threads. Several processes may open the same file: every write is one
``BEGIN IMMEDIATE`` transaction, and the conditional checks run inside it."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from noeta.backends.stores._rows import (
    SCHEMA_VERSION,
    TASK_COLUMNS,
    encode,
    event_decoder,
    inbox_decoder,
    inbox_kind,
    task_row,
)
from noeta.core.errors import Conflict, StoreUnavailable, UnknownTask
from noeta.core.events import Event, InboxItem, Recorded, event_type
from noeta.core.store import (
    SNAPSHOT_VERSIONS,
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

T = TypeVar("T")

_BUSY_TIMEOUT_MS = 10000
_OPEN_ATTEMPTS = 5
#: sqlite failures that mean "the store is not usable right now", not a bug
#: in the statement (``IntegrityError`` / ``ProgrammingError`` stay as they are).
_UNAVAILABLE = (sqlite3.OperationalError,)


def _unavailable(exc: BaseException) -> bool:
    return isinstance(exc, _UNAVAILABLE) or type(exc) is sqlite3.DatabaseError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS noeta_schema (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
    task_id        TEXT PRIMARY KEY,
    parent_id      TEXT,
    root_id        TEXT NOT NULL,
    agent          TEXT NOT NULL,
    queue          TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('ready','running','waiting','ended')),
    waiting_on     TEXT,
    ready_at       REAL,
    owner          TEXT,
    owner_epoch    INTEGER NOT NULL DEFAULT 0,
    lease_until    REAL,
    last_seq       INTEGER NOT NULL,
    inbox_seq      INTEGER NOT NULL DEFAULT 0,
    inbox_read_seq INTEGER NOT NULL DEFAULT 0,
    end_status     TEXT,
    dedupe_key     TEXT UNIQUE,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_claimable ON tasks (queue, status, ready_at);
CREATE INDEX IF NOT EXISTS tasks_parent ON tasks (parent_id);
CREATE INDEX IF NOT EXISTS tasks_root ON tasks (root_id);
CREATE TABLE IF NOT EXISTS events (
    task_id TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    type    TEXT NOT NULL,
    at      REAL NOT NULL,
    data    BLOB NOT NULL,
    PRIMARY KEY (task_id, seq)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS inbox (
    task_id    TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    at         REAL NOT NULL,
    data       BLOB NOT NULL,
    dedupe_key TEXT,
    PRIMARY KEY (task_id, seq)
) WITHOUT ROWID;
CREATE UNIQUE INDEX IF NOT EXISTS inbox_dedupe ON inbox (task_id, dedupe_key)
    WHERE dedupe_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS snapshots (
    task_id TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    version TEXT NOT NULL,
    data    BLOB NOT NULL,
    PRIMARY KEY (task_id, version)
) WITHOUT ROWID;
"""

def _refuse_newer(conn: sqlite3.Connection, path: str) -> None:
    row = conn.execute("SELECT max(version) FROM noeta_schema").fetchone()
    if row is not None and row[0] is not None and int(row[0]) > SCHEMA_VERSION:
        raise StoreUnavailable(f"sqlite store {path} is at schema version {row[0]}; this "
                               f"noeta knows {SCHEMA_VERSION} (stop every worker and upgrade)")


def _set_version(conn: sqlite3.Connection) -> None:
    """Record this code's schema version once its tables are in place. An
    older store's holds tables go (0.6's ``holds``, a development build's
    ``tree_holds``): a job they kept died with the worker that ran it, as
    the next pickup of its task tells."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT max(version) FROM noeta_schema").fetchone()
        if row is None or row[0] is None:
            conn.execute("INSERT INTO noeta_schema VALUES (?)", (SCHEMA_VERSION,))
        elif int(row[0]) < SCHEMA_VERSION:
            conn.execute("DROP TABLE IF EXISTS holds")
            conn.execute("DROP TABLE IF EXISTS tree_holds")
            conn.execute("UPDATE noeta_schema SET version = ?", (SCHEMA_VERSION,))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _create_private(path: str) -> None:
    """Create the database file owner-only (it holds every conversation)
    when it does not exist; SQLite gives its ``-wal`` and ``-shm`` files the
    database file's mode. An existing file keeps its mode."""
    if not path or path == ":memory:" or path.startswith("file:"):
        return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    os.close(fd)


class SqliteStore:
    def __init__(self, path: str, *, poll_interval: float = 0.5) -> None:
        self.path = path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="noeta-sqlite")
        self._conn: sqlite3.Connection | None = None
        self._listeners: list[OnChange] = []
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.poll_interval = poll_interval

    # ------------------------------------------------------------ plumbing

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            # Two processes opening a new file at once race on switching it to
            # WAL; the busy timeout comes first so that waits instead of
            # failing, and a first open that still finds the file locked is
            # tried again.
            _create_private(self.path)
            for attempt in range(_OPEN_ATTEMPTS):
                conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
                try:
                    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    conn.execute("CREATE TABLE IF NOT EXISTS noeta_schema"
                                 " (version INTEGER NOT NULL)")
                    _refuse_newer(conn, self.path)
                    conn.executescript(_SCHEMA)
                    _set_version(conn)
                except sqlite3.OperationalError as exc:
                    conn.close()
                    if "locked" not in str(exc) or attempt + 1 >= _OPEN_ATTEMPTS:
                        raise
                    time.sleep(0.05 * (attempt + 1))
                    continue
                except BaseException:
                    conn.close()
                    raise
                self._conn = conn
                break
        assert self._conn is not None
        return self._conn

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        self._loop = asyncio.get_running_loop()

        def call() -> T:
            try:
                return fn(self._connect())
            except sqlite3.DatabaseError as exc:
                if not _unavailable(exc):
                    raise
                # A locked, full, unreadable or vanished database: nothing
                # was written (the transaction rolled back), so the caller may
                # try again later.
                raise StoreUnavailable(f"sqlite store {self.path}: {exc}") from exc

        return await self._loop.run_in_executor(self._executor, call)

    async def _write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def tx(conn: sqlite3.Connection) -> T:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
                conn.execute("COMMIT")
            except BaseException:
                # sqlite may already have rolled back (a full disk); a
                # transaction left open would break every later write.
                if conn.in_transaction:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")
                raise
            return result

        return await self._run(tx)

    def _notify(self, changes: Sequence[tuple[str, ChangeKind]]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for task_id, kind in changes:
            for cb in listeners:
                cb(task_id, kind)

    def on_change(self, callback: OnChange) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return unsubscribe

    async def close(self) -> None:
        def shut(conn: sqlite3.Connection) -> None:
            conn.close()

        if self._conn is not None:
            await self._run(shut)
            self._conn = None
        self._executor.shutdown(wait=False)

    # ------------------------------------------------------------ helpers run inside a tx

    @staticmethod
    def _insert_task(conn: sqlite3.Connection, task: NewTask, now: float) -> tuple[str, bool]:
        if task.dedupe_key is not None:
            hit = conn.execute(
                "SELECT task_id FROM tasks WHERE dedupe_key = ?", (task.dedupe_key,)
            ).fetchone()
            if hit:
                return str(hit[0]), False
        if conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task.task_id,)).fetchone():
            return task.task_id, False
        created = task.created
        conn.execute(
            "INSERT INTO tasks (task_id, parent_id, root_id, agent, queue, status, last_seq,"
            " inbox_seq, dedupe_key, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'ready', 1, ?, ?, ?, ?)",
            (
                task.task_id,
                created.parent_id,
                created.root_id or task.task_id,
                created.agent,
                task.queue,
                len(task.inbox),
                task.dedupe_key,
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO events (task_id, seq, type, at, data) VALUES (?, 1, ?, ?, ?)",
            (task.task_id, event_type(created), now, encode(created)),
        )
        for i, body in enumerate(task.inbox, start=1):
            conn.execute(
                "INSERT INTO inbox (task_id, seq, kind, at, data) VALUES (?, ?, ?, ?, ?)",
                (task.task_id, i, inbox_kind(body), now, encode(body)),
            )
        return task.task_id, True

    @staticmethod
    def _insert_post(conn: sqlite3.Connection, post: Post, now: float) -> tuple[int, bool]:
        row = conn.execute(
            "SELECT status, inbox_seq FROM tasks WHERE task_id = ?", (post.task_id,)
        ).fetchone()
        if row is None:
            raise UnknownTask(post.task_id)
        status, inbox_seq = str(row[0]), int(row[1])
        if post.dedupe_key is not None:
            hit = conn.execute(
                "SELECT seq FROM inbox WHERE task_id = ? AND dedupe_key = ?",
                (post.task_id, post.dedupe_key),
            ).fetchone()
            if hit:
                return int(hit[0]), False
        seq = inbox_seq + 1
        conn.execute(
            "INSERT INTO inbox (task_id, seq, kind, at, data, dedupe_key)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (post.task_id, seq, inbox_kind(post.body), now, encode(post.body), post.dedupe_key),
        )
        wake = status == "waiting" and post.wake
        conn.execute(
            "UPDATE tasks SET inbox_seq = ?, updated_at = ?"
            + (", status = 'ready', ready_at = NULL" if wake else "")
            + " WHERE task_id = ?",
            (seq, now, post.task_id),
        )
        return seq, True

    # ------------------------------------------------------------ Store

    async def create(self, task: NewTask) -> tuple[str, bool]:
        result = await self._write(lambda conn: self._insert_task(conn, task, time.time()))
        if result[1]:
            self._notify([(result[0], "created")])
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

        def tx(conn: sqlite3.Connection) -> Appended:
            now = time.time()
            row = conn.execute(
                "SELECT lease_until, owner_epoch, last_seq, inbox_seq, inbox_read_seq"
                " FROM tasks WHERE task_id = ?",
                (claim.task_id,),
            ).fetchone()
            if row is None:
                raise UnknownTask(claim.task_id)
            lease, epoch, last_seq, inbox_seq, read_seq = row
            # Only a held claim has a lease: running, or kept over a wait.
            if lease is None or epoch != claim.epoch:
                raise Conflict(f"{claim.task_id}: claim epoch {claim.epoch} no longer holds")
            if last_seq != expected_seq:
                raise Conflict(
                    f"{claim.task_id}: expected seq {expected_seq}, log is at {last_seq}"
                )
            new_status, waiting_on, ready_at, read_seq = next_row_state(patch, inbox_seq,
                                                                        read_seq)
            seq = int(last_seq)
            for event in events:
                seq += 1
                conn.execute(
                    "INSERT INTO events (task_id, seq, type, at, data) VALUES (?, ?, ?, ?, ?)",
                    (claim.task_id, seq, event_type(event), now, encode(event)),
                )
            releasing = new_status != "running" and not (patch.keep and new_status != "ended")
            if new_status == "ended":
                conn.execute("DELETE FROM snapshots WHERE task_id = ?", (claim.task_id,))
            conn.execute(
                "UPDATE tasks SET last_seq = ?, status = ?, waiting_on = ?, ready_at = ?,"
                " inbox_read_seq = ?, end_status = COALESCE(?, end_status), updated_at = ?"
                + (", owner = NULL, lease_until = NULL" if releasing else "")
                + " WHERE task_id = ?",
                (
                    seq,
                    new_status,
                    waiting_on,
                    ready_at,
                    read_seq,
                    patch.end_status,
                    now,
                    claim.task_id,
                ),
            )
            changes.append((claim.task_id, "appended"))
            if releasing and new_status == "ready":
                changes.append((claim.task_id, "released"))
            for task in creates:
                task_id, created = self._insert_task(conn, task, now)
                if created:
                    changes.append((task_id, "created"))
            for post in posts:
                try:
                    _, created = self._insert_post(conn, post, now)
                except UnknownTask:
                    continue  # a notice for a task that does not exist is dropped
                if created:
                    changes.append((post.task_id, "posted"))
            return Appended(seq=seq, at=now)

        done = await self._write(tx)
        self._notify(changes)
        return done

    async def read(self, task_id: str, after: int = 0) -> list[Recorded]:
        def q(conn: sqlite3.Connection) -> list[Recorded]:
            rows = conn.execute(
                "SELECT seq, at, data FROM events WHERE task_id = ? AND seq > ? ORDER BY seq",
                (task_id, after),
            ).fetchall()
            return [Recorded(seq=s, at=a, event=event_decoder.decode(d)) for s, a, d in rows]

        return await self._run(q)

    async def post(self, post: Post) -> tuple[int, bool]:
        result = await self._write(lambda conn: self._insert_post(conn, post, time.time()))
        if result[1]:
            self._notify([(post.task_id, "posted")])
        return result

    async def inbox(self, task_id: str, after: int = 0) -> list[InboxItem]:
        def q(conn: sqlite3.Connection) -> list[InboxItem]:
            rows = conn.execute(
                "SELECT seq, at, data FROM inbox WHERE task_id = ? AND seq > ? ORDER BY seq",
                (task_id, after),
            ).fetchall()
            return [InboxItem(seq=s, at=a, body=inbox_decoder.decode(d)) for s, a, d in rows]

        return await self._run(q)

    async def get(self, task_id: str) -> TaskRow | None:
        def q(conn: sqlite3.Connection) -> TaskRow | None:
            row = conn.execute(
                f"SELECT {TASK_COLUMNS} FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            return task_row(row) if row else None

        return await self._run(q)

    async def tasks(
        self,
        *,
        parent_id: str | None = None,
        status: Status | None = None,
        limit: int = 100,
        owner: str | None = None,
        roots_only: bool = False,
    ) -> list[TaskRow]:
        where, args = [], []
        if parent_id is not None:
            where.append("parent_id = ?")
            args.append(parent_id)
        if status is not None:
            where.append("status = ?")
            args.append(status)
        if owner is not None:
            where.append("owner = ?")
            args.append(owner)
        if roots_only:
            where.append("parent_id IS NULL")
        sql = f"SELECT {TASK_COLUMNS} FROM tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"

        def q(conn: sqlite3.Connection) -> list[TaskRow]:
            return [task_row(r) for r in conn.execute(sql, (*args, limit)).fetchall()]

        return await self._run(q)

    async def claim(
        self, worker: str, queues: Sequence[str], lease_seconds: float,
        *, roots: Sequence[str] | None = None,
    ) -> Claim | None:
        if not queues or (roots is not None and not roots):
            return None
        marks = ",".join("?" for _ in queues)
        root_ids = list(roots) if roots is not None else []
        in_roots = f" AND root_id IN ({','.join('?' for _ in root_ids)})" if root_ids else ""

        def tx(conn: sqlite3.Connection) -> Claim | None:
            now = time.time()
            row = conn.execute(
                "SELECT task_id, owner_epoch, queue FROM tasks"
                f" WHERE queue IN ({marks}){in_roots} AND ("
                " (status = 'ready' AND (ready_at IS NULL OR ready_at <= ?))"
                " OR (status = 'waiting' AND (ready_at <= ? OR lease_until IS NOT NULL))"
                " OR status = 'running')"
                " AND (lease_until IS NULL OR lease_until < ?)"
                " ORDER BY updated_at LIMIT 1",
                (*queues, *root_ids, now, now, now),
            ).fetchone()
            if row is None:
                return None
            task_id, epoch = str(row[0]), int(row[1]) + 1
            until = now + lease_seconds
            conn.execute(
                "UPDATE tasks SET status = 'running', owner = ?, owner_epoch = ?,"
                " lease_until = ?, ready_at = NULL, waiting_on = NULL, updated_at = ?"
                " WHERE task_id = ?",
                (worker, epoch, until, now, task_id),
            )
            return Claim(task_id=task_id, worker=worker, epoch=epoch, lease_until=until,
                         queue=str(row[2]))

        return await self._write(tx)

    async def renew(self, claim: Claim, lease_seconds: float) -> Claim | None:
        def tx(conn: sqlite3.Connection) -> Claim | None:
            until = time.time() + lease_seconds
            cur = conn.execute(
                "UPDATE tasks SET lease_until = ? WHERE task_id = ? AND owner_epoch = ?"
                " AND lease_until IS NOT NULL",
                (until, claim.task_id, claim.epoch),
            )
            if cur.rowcount == 0:
                return None
            return Claim(task_id=claim.task_id, worker=claim.worker, epoch=claim.epoch,
                         lease_until=until, queue=claim.queue)

        return await self._write(tx)

    async def snapshot(self, task_id: str, version: str | None = None) -> Snapshot | None:
        def q(conn: sqlite3.Connection) -> Snapshot | None:
            row = conn.execute(
                "SELECT seq, version, data FROM snapshots WHERE task_id = ?"
                + (" AND version = ?" if version is not None else "")
                + " ORDER BY seq DESC LIMIT 1",
                (task_id, version) if version is not None else (task_id,)).fetchone()
            return Snapshot(seq=int(row[0]), version=str(row[1]), data=bytes(row[2])) \
                if row else None

        return await self._run(q)

    async def put_snapshot(self, task_id: str, snapshot: Snapshot) -> None:
        def tx(conn: sqlite3.Connection) -> None:
            if conn.execute("SELECT 1 FROM tasks WHERE task_id = ? AND status = 'ended'",
                            (task_id,)).fetchone() is not None:
                return  # its end deleted the last one; nothing claims it again
            conn.execute(
                "INSERT INTO snapshots (task_id, seq, version, data) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (task_id, version) DO UPDATE SET seq = excluded.seq,"
                " data = excluded.data WHERE excluded.seq >= snapshots.seq",
                (task_id, snapshot.seq, snapshot.version, snapshot.data))
            conn.execute(
                "DELETE FROM snapshots WHERE task_id = ? AND version NOT IN ("
                " SELECT version FROM snapshots WHERE task_id = ?"
                " ORDER BY version = ? DESC, seq DESC LIMIT ?)",
                (task_id, task_id, snapshot.version, SNAPSHOT_VERSIONS))

        await self._write(tx)

    async def release(self, claim: Claim) -> None:
        def tx(conn: sqlite3.Connection) -> int:
            return conn.execute(
                "UPDATE tasks SET status = CASE status WHEN 'running' THEN 'ready'"
                " ELSE status END, owner = NULL, lease_until = NULL, updated_at = ?"
                " WHERE task_id = ? AND owner_epoch = ? AND lease_until IS NOT NULL",
                (time.time(), claim.task_id, claim.epoch),
            ).rowcount

        if await self._write(tx):
            self._notify([(claim.task_id, "released")])


def open_url(url: str) -> SqliteStore:
    """``sqlite:///path`` -> a store at ``path`` (``~`` expanded, parent
    directories made). The ``sqlite`` entry point of ``noeta.stores``."""
    from pathlib import Path

    path = Path(url.removeprefix("sqlite:///")).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteStore(str(path))
