"""A host-owned pool of live MCP connections, keyed by server identity.

The Engine is a per-turn value (``noeta.execution.resolver``): every turn
builds its tool set afresh, and an MCP server's ``tools/list`` is part of
that build. The *connection* behind those tools is the one expensive
external resource in the build — a stdio server is a subprocess that takes
hundreds of milliseconds to seconds to start and may hold state a user
expects to keep between turns (a browser, a database handle, a login) — so
it outlives the turn here, shared by every build that names the same server.

Identity is the server plus the host's **scope**: ``(transport, scope,
alias, argv, env)`` for a stdio spec, ``(transport, scope, alias, url,
headers)`` for HTTP. Two tenants whose resolver hands out different
credentials therefore get different connections; two tasks on the same
server in the same scope share one; and a host that resolves a scope per
task (``HostConfig.mcp_scope_resolver`` — a tenant id, a workspace) keeps a
stateful server's state (a browser, a login) from crossing tenants even when
the spec is byte-identical. ``None`` scope is the shared partition. The key
lives in memory only — credentials never leave the host — and
``tool_subset`` is not part of it (a subset filters the tool set, it does
not change the connection).

Lifecycle:

* :meth:`McpConnectionPool.acquire` returns a started client and counts a
  holder; :meth:`release` uncounts. The Engine build acquires, the built
  Engine's ``weakref.finalize`` releases, a build that fails after acquiring
  releases in its ``except``.
* A connection with no holders that goes unused past ``idle_ttl`` is closed
  on the next pool operation (no background thread; the clock is monotonic
  and host-side — the kernel stays clock-free).
* :meth:`invalidate` retires entries: a retired connection is dropped from
  the table at once, so the next ``acquire`` connects fresh, but it closes
  only when its last holder releases — a turn that is still using it keeps
  it. This is the host's "server config changed" verb.
* :meth:`shutdown` closes every connection now (the host calls it after its
  workers have stopped).

Every client serializes its JSON-RPC exchanges on an instance lock (see the
clients), so two turns sharing one connection queue rather than interleave.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

from noeta.builtins.mcp.impl._client import McpStdioClient, SpawnFn
from noeta.builtins.mcp.impl._http_client import McpHttpClient
from noeta.runtime.mcp import (
    HttpPostFn,
    McpAnyServerSpec,
    McpHttpServerSpec,
)


__all__ = [
    "DEFAULT_MCP_IDLE_TTL_S",
    "McpClient",
    "McpConnectionPool",
    "connection_key",
]

_log = logging.getLogger(__name__)

McpClient = Union[McpStdioClient, McpHttpClient]

#: Default idle expiry for a pooled connection with no holder: half an hour.
DEFAULT_MCP_IDLE_TTL_S: float = 1800.0


def connection_key(
    spec: McpAnyServerSpec, scope: Optional[str] = None
) -> tuple[Any, ...]:
    """The pool key of ``spec`` in ``scope`` — the connection's identity,
    credentials included, in memory only. ``tool_subset`` is deliberately
    left out; ``scope`` (``None`` = shared) is the host's partition."""
    if isinstance(spec, McpHttpServerSpec):
        return ("http", scope, spec.alias, spec.url, tuple(spec.headers))
    return ("stdio", scope, spec.alias, tuple(spec.argv), tuple(spec.env))


def _connect_client(
    spec: McpAnyServerSpec,
    *,
    spawn: Optional[SpawnFn],
    http_post: Optional[HttpPostFn],
) -> McpClient:
    """Construct (not yet started) the transport client for ``spec``."""
    if isinstance(spec, McpHttpServerSpec):
        return McpHttpClient(url=spec.url, headers=spec.headers_dict(), post=http_post)
    return McpStdioClient(argv=list(spec.argv), env=spec.env_dict(), spawn=spawn)


def _close_all(by_client: dict[int, "_Entry"]) -> None:
    """The pool's finalizer body: shut every still-tracked client down."""
    entries = list(by_client.values())
    by_client.clear()
    for entry in entries:
        _shutdown_quietly(entry.client)


def _shutdown_quietly(client: Any) -> None:
    try:
        client.shutdown()
    except Exception:  # noqa: BLE001 — one bad client must not break the pool
        _log.warning("MCP client shutdown failed", exc_info=True)


@dataclass
class _Entry:
    key: tuple[Any, ...]
    alias: str
    client: McpClient
    holders: int
    last_used: float
    retired: bool = False


class McpConnectionPool:
    """See the module docstring."""

    def __init__(
        self,
        *,
        spawn: Optional[SpawnFn] = None,
        http_post: Optional[HttpPostFn] = None,
        idle_ttl: Optional[float] = DEFAULT_MCP_IDLE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._spawn = spawn
        self._http_post = http_post
        self._idle_ttl = idle_ttl
        self._clock = clock
        # key → live entry (retired entries leave this table at once).
        self._entries: dict[tuple[Any, ...], _Entry] = {}
        # id(client) → entry, for every client a holder may still release —
        # retired ones included, until their last holder lets go.
        self._by_client: dict[int, _Entry] = {}
        self._lock = threading.Lock()
        # One connect at a time per key; builds for different servers connect
        # concurrently, and a second build for the same server waits for the
        # first connect instead of spawning a duplicate.
        self._connect_locks: dict[tuple[Any, ...], threading.Lock] = {}
        # Close whatever is still open when the pool itself is collected or
        # the interpreter exits — a host that never called ``shutdown`` (a
        # test, a crashed process) must not leave stdio servers behind.
        weakref.finalize(self, _close_all, self._by_client)

    # -- acquire / release ----------------------------------------------

    def acquire(
        self, spec: McpAnyServerSpec, scope: Optional[str] = None
    ) -> tuple[McpClient, bool]:
        """A started client for ``spec`` in ``scope`` and whether it was reused.

        Connects when no live connection exists for the spec's key, under a
        per-key lock. A connect or handshake fault raises ``McpError`` and
        leaves nothing behind. The caller must :meth:`release` the client.
        """
        key = connection_key(spec, scope)
        with self._lock:
            expired = self._sweep_locked()
            hit = self._take_locked(key)
            if hit is None:
                connect_lock = self._connect_locks.setdefault(key, threading.Lock())
        for c in expired:
            _shutdown_quietly(c)
        if hit is not None:
            return hit.client, True
        with connect_lock:
            try:
                with self._lock:
                    hit = self._take_locked(key)
                    if hit is not None:
                        return hit.client, True
                client = _connect_client(
                    spec, spawn=self._spawn, http_post=self._http_post
                )
                try:
                    client.start()
                except BaseException:
                    _shutdown_quietly(client)
                    raise
                with self._lock:
                    entry = _Entry(
                        key=key,
                        alias=spec.alias,
                        client=client,
                        holders=1,
                        last_used=self._clock(),
                    )
                    if key in self._entries:
                        # Lost a race to a build that took a fresh per-key
                        # lock after ours was popped: keep this connection
                        # only for its own holder, closing on release.
                        entry.retired = True
                    else:
                        self._entries[key] = entry
                    self._by_client[id(client)] = entry
                return client, False
            finally:
                # Never leak a per-key lock, whether the connect succeeded,
                # failed, or lost the race to a concurrent build.
                with self._lock:
                    self._connect_locks.pop(key, None)

    def release(self, client: Any) -> None:
        """Uncount one holder of ``client``. Closes it when it was retired
        and this was its last holder; sweeps idle connections. Unknown or
        already-closed clients are ignored, so a late finalizer is harmless."""
        to_close: list[Any] = []
        with self._lock:
            entry = self._by_client.get(id(client))
            if entry is not None:
                entry.holders = max(0, entry.holders - 1)
                entry.last_used = self._clock()
                if entry.retired and entry.holders == 0:
                    self._by_client.pop(id(client), None)
                    to_close.append(entry.client)
            to_close.extend(self._sweep_locked())
        for c in to_close:
            _shutdown_quietly(c)

    def release_all(self, clients: Any) -> None:
        """:meth:`release` each of ``clients`` — the ``weakref.finalize``
        callback shape."""
        for client in list(clients):
            self.release(client)

    def retire(self, client: Any) -> None:
        """Drop ``client``'s entry so the next :meth:`acquire` for its server
        connects fresh; the client itself closes when its holders are gone.
        The build path calls this on a pooled connection that stopped
        answering (a stale stdio server, a restarted HTTP endpoint)."""
        with self._lock:
            entry = self._by_client.get(id(client))
            if entry is None:
                return
            self._retire_locked(entry)

    # -- host verbs ------------------------------------------------------

    def invalidate(self, alias: Optional[str] = None) -> None:
        """Retire every live connection (``alias=None``) or those of one
        alias. In-flight holders keep theirs until they release; the next
        build reconnects."""
        to_close: list[Any] = []
        with self._lock:
            for entry in list(self._entries.values()):
                if alias is None or entry.alias == alias:
                    self._retire_locked(entry)
                    if entry.holders == 0:
                        self._by_client.pop(id(entry.client), None)
                        to_close.append(entry.client)
        for c in to_close:
            _shutdown_quietly(c)

    def shutdown(self) -> None:
        """Close every connection now, holders or not. The host calls this
        after its workers have stopped, so no turn is mid-call."""
        with self._lock:
            clients = [e.client for e in self._by_client.values()]
            self._entries.clear()
            self._by_client.clear()
            self._connect_locks.clear()
        for c in clients:
            _shutdown_quietly(c)

    # -- introspection (tests, diagnostics) -----------------------------

    def live_count(self) -> int:
        """Connections the pool still holds open — retired-but-held included."""
        with self._lock:
            return len(self._by_client)

    def holders_of(self, client: Any) -> int:
        with self._lock:
            entry = self._by_client.get(id(client))
            return entry.holders if entry is not None else 0

    # -- internals (call under ``self._lock``) --------------------------

    def _take_locked(self, key: tuple[Any, ...]) -> Optional[_Entry]:
        entry = self._entries.get(key)
        if entry is None or entry.retired:
            return None
        entry.holders += 1
        entry.last_used = self._clock()
        return entry

    def _retire_locked(self, entry: _Entry) -> None:
        entry.retired = True
        if self._entries.get(entry.key) is entry:
            del self._entries[entry.key]

    def _sweep_locked(self) -> list[Any]:
        """Drop idle, holder-less connections past the TTL; return the
        clients to close outside the lock."""
        if self._idle_ttl is None:
            return []
        now = self._clock()
        expired: list[Any] = []
        for entry in list(self._entries.values()):
            if entry.holders == 0 and now - entry.last_used >= self._idle_ttl:
                self._retire_locked(entry)
                self._by_client.pop(id(entry.client), None)
                expired.append(entry.client)
        return expired
