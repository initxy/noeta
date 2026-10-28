"""Live MCP connections, shared per worker process.

A stdio server is a subprocess that may take seconds to start and may hold
state a user expects to keep (a browser, a login), so its connection outlives
a turn and is shared by every task naming the same server in the same scope.
The key is ``(scope, server identity)`` — credentials included, in memory
only — so two tenants with different credentials, or a host that scopes per
tenant, never share a connection.

* ``acquire`` returns a started connection (connecting when needed) and
  counts a holder until the ``async with`` exits.
* A connection that is ``broken`` (timeout, EOF) or was ``retire``\\ d is
  dropped from the table at once, so the next acquire connects afresh; it
  closes when its last holder is done.
* A connection with no holder that went unused past ``idle_ttl`` is closed
  on the next pool operation (no background task).
* ``close`` shuts every connection now.

Connections belong to the event loop that made them (a subprocess's pipes
do), so :func:`default_pool` keeps one pool per running loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import signal
import time
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx

from noeta.plugins.mcp.client import McpClient, RequestContext
from noeta.plugins.mcp.config import McpAuthRequired, McpError, McpServer, request_timeout_s
from noeta.plugins.mcp.http_client import HttpClient
from noeta.plugins.mcp.oauth import OAuth, store_for
from noeta.plugins.mcp.sse_client import SseClient
from noeta.plugins.mcp.stdio import StdioClient
from noeta.plugins.mcp.ws_client import WsClient

__all__ = ["DEFAULT_IDLE_TTL_S", "HELPER_TIMEOUT_S", "McpPool", "connect", "default_pool",
           "helper_headers"]

log = logging.getLogger("noeta.mcp")

#: Idle expiry of a connection nobody holds: half an hour.
DEFAULT_IDLE_TTL_S = 1800.0
#: A ``headersHelper`` command's budget, and the most it may print.
HELPER_TIMEOUT_S = 10.0
_HELPER_OUTPUT_CAP = 1_000_000

#: ``(server, scope) -> client``; a one-argument callable is also accepted.
Connect = Callable[..., McpClient]
T = TypeVar("T")


async def helper_headers(server: McpServer) -> dict[str, str]:
    """The headers ``server``'s ``headersHelper`` prints: a shell command
    run with ``NOETA_MCP_SERVER_NAME`` / ``NOETA_MCP_SERVER_URL`` set, whose
    stdout must be a JSON object of strings within :data:`HELPER_TIMEOUT_S`.
    A helper that fails is logged and gives no headers (the static ones
    still apply)."""
    env = {**os.environ, "NOETA_MCP_SERVER_NAME": server.alias,
           "NOETA_MCP_SERVER_URL": server.url}
    try:
        proc = await asyncio.create_subprocess_shell(
            server.headers_helper, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env,
            start_new_session=True)
    except OSError as exc:
        log.warning("mcp: headersHelper of %s could not start: %s", server.alias, exc)
        return {}
    try:
        async with asyncio.timeout(HELPER_TIMEOUT_S):
            out, _ = await proc.communicate()
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)  # the shell and what it started
        await proc.wait()
        log.warning("mcp: headersHelper of %s timed out", server.alias)
        return {}
    data = None
    if proc.returncode == 0 and out.strip():
        with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
            data = json.loads(out[:_HELPER_OUTPUT_CAP].decode("utf-8"))
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        log.warning("mcp: headersHelper of %s did not print a JSON object of strings "
                    "(exit %s)", server.alias, proc.returncode)
        return {}
    return {str(k): v for k, v in data.items()}


def connect(server: McpServer, scope: str | None = None, *,
            http: httpx.AsyncClient | None = None) -> McpClient:
    """A new (not yet started) client for ``server``; a remote one signs
    in with the tokens kept for it in ``scope``."""
    timeout = request_timeout_s()
    if server.transport == "stdio":
        return StdioClient(list(server.command), env=dict(server.env), timeout=timeout,
                           bare_elicitation=server.bare_elicitation)

    async def run_helper() -> dict[str, str]:
        return await helper_headers(server)

    headers_fn = run_helper if server.headers_helper else None
    if server.transport == "ws":
        return WsClient(server.url, headers=dict(server.headers), timeout=timeout,
                        headers_fn=headers_fn, bare_elicitation=server.bare_elicitation)
    auth = OAuth(server, store_for(server, scope), http)
    if server.transport == "sse":
        return SseClient(server.url, headers=dict(server.headers), http=http,
                         timeout=timeout, auth=auth, headers_fn=headers_fn,
                         bare_elicitation=server.bare_elicitation)
    return HttpClient(server.url, headers=dict(server.headers), http=http, timeout=timeout,
                      auth=auth, headers_fn=headers_fn, bare_elicitation=server.bare_elicitation)


@dataclass
class _Entry:
    client: McpClient
    holders: int = 0
    last_used: float = 0.0
    retired: bool = False


class McpPool:
    """``http``: the ``httpx.AsyncClient`` remote connections and sign-ins
    go through (a host's proxy, mTLS); ``connect`` replaces how a client is
    made (``(server)`` or ``(server, scope)``)."""

    def __init__(self, *, idle_ttl: float | None = DEFAULT_IDLE_TTL_S,
                 connect: Connect | None = None, clock: Callable[[], float] = time.monotonic,
                 http: httpx.AsyncClient | None = None) -> None:
        self.idle_ttl = idle_ttl
        self.http = http
        self._connect = connect
        try:
            self._scoped = connect is not None and len(inspect.signature(connect).parameters) > 1
        except (TypeError, ValueError):
            self._scoped = False
        self._clock = clock
        self._entries: dict[tuple[object, ...], _Entry] = {}
        self._starting: dict[tuple[object, ...], asyncio.Future[_Entry]] = {}

    @staticmethod
    def key(server: McpServer, scope: str | None) -> tuple[object, ...]:
        return (scope, *server.key())

    def __len__(self) -> int:
        return len(self._entries)

    @contextlib.asynccontextmanager
    async def acquire(self, server: McpServer, scope: str | None = None,
                      context: RequestContext | None = None) -> AsyncIterator[McpClient]:
        """Hold a started connection to ``server`` for the ``async with``
        body, with server requests routed to ``context`` meanwhile (and to
        it, too, while a new connection initializes). A failed connect
        raises ``McpError`` and leaves nothing behind."""
        await self._expire()
        key = self.key(server, scope)
        entry = await self._entry(key, server, scope, context)
        entry.holders += 1
        try:
            with entry.client.bound(context):
                yield entry.client
        finally:
            entry.holders -= 1
            entry.last_used = self._clock()
            if entry.client.broken is not None:
                self._drop(key, entry)
            if entry.retired and entry.holders == 0:
                await entry.client.close()

    async def _entry(self, key: tuple[object, ...], server: McpServer, scope: str | None,
                     context: RequestContext | None) -> _Entry:
        entry = self._entries.get(key)
        if entry is not None and entry.client.broken is None:
            return entry
        if entry is not None:
            self._drop(key, entry)
            if entry.holders == 0:
                await entry.client.close()
        pending = self._starting.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        fut: asyncio.Future[_Entry] = asyncio.get_running_loop().create_future()
        self._starting[key] = fut
        if self._connect is None:
            client = connect(server, scope, http=self.http)
        else:
            client = self._connect(server, scope) if self._scoped else self._connect(server)
        client.fallback = context
        try:
            await client.start()
        except BaseException as exc:
            await client.close()
            fut.set_exception(exc)
            fut.exception()  # retrieved: concurrent waiters see it, nobody warns
            raise
        finally:
            self._starting.pop(key, None)
        entry = _Entry(client=client, last_used=self._clock())
        self._entries[key] = entry
        fut.set_result(entry)
        return entry

    async def use(self, server: McpServer, scope: str | None,
                  fn: Callable[[McpClient], Awaitable[T]], *, retry: bool = False,
                  context: RequestContext | None = None) -> T:
        """Run ``fn`` on a held connection. With ``retry`` (idempotent
        requests only: a list, a read) a connection that was reused and fails
        is dropped and the server connected afresh once — a stdio server that
        died between turns, or an HTTP endpoint that restarted, costs one
        reconnect rather than the turn's tools."""
        reused = self.key(server, scope) in self._entries
        try:
            async with self.acquire(server, scope, context) as client:
                return await fn(client)
        except McpAuthRequired:
            raise  # reconnecting does not sign anyone in
        except McpError:
            if not (retry and reused):
                raise
            await self.forget(server, scope)
        async with self.acquire(server, scope, context) as client:
            return await fn(client)

    async def forget(self, server: McpServer, scope: str | None = None) -> None:
        """Drop ``server``'s connection in ``scope``; the next use reconnects."""
        key = self.key(server, scope)
        entry = self._entries.get(key)
        if entry is not None:
            self._drop(key, entry)
            if entry.holders == 0:
                await entry.client.close()

    def _drop(self, key: tuple[object, ...], entry: _Entry) -> None:
        entry.retired = True
        if self._entries.get(key) is entry:
            del self._entries[key]

    async def retire(self, alias: str | None = None) -> None:
        """Reconnect on next use: every connection (or ``alias``'s, in every
        scope). One still held closes when its holder is done."""
        for key, entry in list(self._entries.items()):
            if alias is None or key[2] == alias:
                self._drop(key, entry)
                if entry.holders == 0:
                    await entry.client.close()

    async def _expire(self) -> None:
        if self.idle_ttl is None:
            return
        now = self._clock()
        for key, entry in list(self._entries.items()):
            if entry.holders == 0 and now - entry.last_used > self.idle_ttl:
                self._drop(key, entry)
                await entry.client.close()

    async def close(self) -> None:
        entries = list(self._entries.values())
        self._entries.clear()
        for entry in entries:
            entry.retired = True
            await entry.client.close()


_POOLS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, McpPool] = (
    weakref.WeakKeyDictionary())


def default_pool() -> McpPool:
    """This process's pool for the running event loop."""
    loop = asyncio.get_running_loop()
    pool = _POOLS.get(loop)
    if pool is None:
        pool = _POOLS[loop] = McpPool()
    return pool
