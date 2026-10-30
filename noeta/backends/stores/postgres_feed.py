"""The Postgres store's change feed (``on_change``) and live output
(``publish_deltas`` / ``on_delta``) across processes.

Changes are announced with ``pg_notify`` inside the same transaction, so a
notification exists exactly when the change committed. A dedicated
``LISTEN`` connection (reopened when it drops) feeds ``on_change`` in every
process. This process's own changes are reported synchronously after commit
and their echoes are dropped by backend pid. Notifications sent while the
listener was reconnecting are lost; polling covers that gap.

Live model output rides a second channel, ``<schema>_deltas``, outside any
task transaction, heard only by processes that subscribe: best effort,
never stored, each notification under Postgres' 8000-byte payload limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import weakref
from collections.abc import Callable, Sequence
from typing import Any

import msgspec
import psycopg

from noeta.core.store import ChangeKind, OnChange
from noeta.core.types import Delta

Conn = psycopg.AsyncConnection[Any]

log = logging.getLogger("noeta.backends.stores.postgres")

#: Bytes of one delta notification's payload (Postgres refuses 8000 and up).
DELTA_PAYLOAD_BYTES = 7000
_delta_batch = msgspec.json.Decoder(list[tuple[str, msgspec.Raw]])
_delta = msgspec.json.Decoder(Delta)


def delta_payloads(deltas: Sequence[tuple[str, Delta]]) -> list[str]:
    """``deltas`` as JSON payloads under the notification limit, in order;
    a fragment too long for one is cut into several."""
    pieces: list[tuple[str, Delta]] = []
    step = DELTA_PAYLOAD_BYTES // 8  # a character takes at most 6 bytes as JSON
    for task_id, d in deltas:
        text = d.text
        if d.kind == "tool":  # the latest output replaces the one before: its tail
            pieces.append((task_id, msgspec.structs.replace(d, text=text[-step:])))
            continue
        for i in range(0, max(len(text), 1), step):
            pieces.append((task_id, msgspec.structs.replace(d, text=text[i:i + step])))
    out: list[str] = []
    batch: list[tuple[str, Delta]] = []
    size = 2
    for piece in pieces:
        n = len(msgspec.json.encode(piece)) + 1
        if batch and size + n > DELTA_PAYLOAD_BYTES:
            out.append(msgspec.json.encode(batch).decode())
            batch, size = [], 2
        batch.append(piece)
        size += n
    if batch:
        out.append(msgspec.json.encode(batch).decode())
    return out


class ChangeFeed:
    """The listening side of one ``PostgresStore``: its ``LISTEN``
    connections and the callbacks they feed."""

    def __init__(self, dsn: str, schema: str, *, connect_timeout: float,
                 listen_check_interval: float) -> None:
        self.dsn = dsn
        self.channel = f"{schema}_changes"
        self.delta_channel = f"{schema}_deltas"
        self.connect_timeout = connect_timeout
        self.listen_check_interval = listen_check_interval
        self.closed = False
        self._listener: asyncio.Task[None] | None = None
        self._delta_listener: asyncio.Task[None] | None = None
        self._lock = threading.Lock()
        self._listeners: list[tuple[OnChange, asyncio.AbstractEventLoop | None]] = []
        self._delta_listeners: list[Callable[[str, Delta], None]] = []
        #: Backend pids of the store's pool connections: their notifications
        #: were already reported locally. Weak, so a closed connection's pid
        #: (which the server may reuse) drops out.
        self.own: weakref.WeakValueDictionary[int, Conn] = weakref.WeakValueDictionary()

    def add_own(self, conn: Conn) -> None:
        """``conn`` is one of the store's pool connections."""
        self.own[conn.info.backend_pid] = conn

    async def connect(self) -> Conn:
        """A ``LISTEN`` connection on the change channel, opened before the
        store counts as open (so a failure to open is the store's)."""
        return await self._listen_connection()

    def start(self, conn: Conn) -> None:
        """Hear the change channel on ``conn`` from now on."""
        self._listener = asyncio.get_running_loop().create_task(self._listen(conn))

    async def notify(self, conn: Conn, changes: Sequence[tuple[str, ChangeKind]]) -> None:
        """Queue the notifications in ``conn``'s current transaction."""
        if changes:
            await conn.execute("SELECT pg_notify(%s, c) FROM unnest(%s::text[]) AS c",
                               (self.channel, [f"{t}:{k}" for t, k in changes]))

    async def send_deltas(self, conn: Conn, deltas: Sequence[tuple[str, Delta]]) -> None:
        payloads = delta_payloads(deltas)
        if payloads:
            await conn.execute("SELECT pg_notify(%s, p) FROM unnest(%s::text[]) AS p",
                               (self.delta_channel, payloads))

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
        own = self.own.get(pid)
        if own is not None and not own.closed:
            return  # already reported by this process
        task_id, _, kind = payload.rpartition(":")
        if task_id and kind in ("appended", "posted", "created", "released"):
            self.deliver([(task_id, kind)])  # type: ignore[list-item]

    def _remote_deltas(self, pid: int, payload: str) -> None:
        own = self.own.get(pid)
        if own is not None and not own.closed:
            return  # this process's own output reached its sinks directly
        try:
            items = _delta_batch.decode(payload)
        except msgspec.DecodeError:
            log.warning("dropped a malformed delta notification")
            return
        for task_id, raw in items:
            try:
                delta = _delta.decode(raw)
            except msgspec.ValidationError:
                continue  # a kind a newer worker sends and this version lacks
            for cb in list(self._delta_listeners):
                try:
                    cb(task_id, delta)
                except Exception:
                    log.exception("on_delta listener failed")

    def on_delta(self, callback: Callable[[str, Delta], None]) -> Callable[[], None]:
        """Call ``callback(task_id, delta)`` with the live output other
        processes publish; returns the unsubscribe function. Its own
        ``LISTEN`` connection opens with the first callback, so a process
        nobody watches through never receives output."""
        self._delta_listeners.append(callback)
        if self._delta_listener is None and not self.closed:
            self._delta_listener = asyncio.get_running_loop().create_task(
                self._listen(None, self.delta_channel, self._remote_deltas))

        def unsubscribe() -> None:
            if callback in self._delta_listeners:
                self._delta_listeners.remove(callback)

        return unsubscribe

    def deliver(self, changes: Sequence[tuple[str, ChangeKind]]) -> None:
        """Report ``changes`` to every ``on_change`` callback, on the event
        loop it subscribed from."""
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
        self.closed = True
        for listening in (self._listener, self._delta_listener):
            if listening is not None:
                listening.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listening
        self._listener = self._delta_listener = None
