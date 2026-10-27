"""The stdio MCP client and the request/response surface both clients share.

The stdio transport is newline-delimited JSON-RPC 2.0 on the server's
stdin/stdout; its logs go to stderr, which is discarded so a chatty server can
never fill a pipe. Only the request/response subset is spoken (``initialize``,
``tools/*``, ``prompts/*``, ``resources/*``); nothing server-pushed
(``list_changed``, sampling, elicitation) is listened for, so a task's tool
set only changes when it is listed again.

* The server is launched from an argv list (never a shell) with a scrubbed
  environment plus the spec's ``env``, in its own process group.
* One exchange at a time: a pooled connection is shared by every task that
  names the server, so a request holds the connection's lock from send to the
  matching reply.
* Reads are bounded by a per-call timeout, a per-line cap and a per-call
  total. A message carrying ``method`` is a server request or notification,
  never our reply: a ``ping`` is answered, anything else ignored.
* A timeout, EOF or read fault leaves the pipe in an unknown state, so the
  connection marks itself ``broken``; the pool then retires it and the next
  use connects afresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from noeta.core.execenv import scrub_env
from noeta.plugins.mcp.config import DEFAULT_TIMEOUT_S, McpError

__all__ = [
    "LINE_CAP",
    "MAX_LIST_PAGES",
    "PROTOCOL_VERSION",
    "TOTAL_CAP",
    "McpClient",
    "StdioClient",
    "collect_pages",
]

LINE_CAP = 1024 * 1024
TOTAL_CAP = 8 * 1024 * 1024
PROTOCOL_VERSION = "2024-11-05"
#: Pages one list call follows: a server that keeps handing back a cursor
#: must not loop forever.
MAX_LIST_PAGES = 100
_MAX_INTERLEAVED = 64
_CLIENT_INFO = {"name": "noeta", "version": "1"}

Request = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def collect_pages(request: Request, method: str, key: str) -> list[dict[str, Any]]:
    """Run a paginated list call to its last page: follow ``nextCursor``
    until it stops, repeats, or :data:`MAX_LIST_PAGES` pages were read."""
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(MAX_LIST_PAGES):
        result = await request(method, {"cursor": cursor} if cursor else {})
        entries = result.get(key)
        if not isinstance(entries, list):
            raise McpError(f"{method} result missing {key!r} array")
        out.extend(e for e in entries if isinstance(e, dict))
        nxt = result.get("nextCursor")
        if not isinstance(nxt, str) or not nxt or nxt in seen:
            break
        seen.add(nxt)
        cursor = nxt
    return out


def initialize_params() -> dict[str, Any]:
    return {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": dict(_CLIENT_INFO)}


class McpClient:
    """What the extension needs from a connection, whatever the transport."""

    broken: str | None = None

    async def start(self) -> None:
        raise NotImplementedError

    async def request(self, method: str, params: dict[str, Any], *,
                      timeout: float | None = None) -> dict[str, Any]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def list_tools(self) -> list[dict[str, Any]]:
        return await collect_pages(self.request, "tools/list", "tools")

    async def call_tool(self, name: str, arguments: dict[str, Any], *,
                        timeout: float | None = None) -> dict[str, Any]:
        return await self.request("tools/call", {"name": name, "arguments": dict(arguments)},
                                  timeout=timeout)

    async def list_prompts(self) -> list[dict[str, Any]]:
        return await collect_pages(self.request, "prompts/list", "prompts")

    async def get_prompt(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.request("prompts/get", {"name": name, "arguments": dict(arguments)})

    async def list_resources(self) -> list[dict[str, Any]]:
        return await collect_pages(self.request, "resources/list", "resources")

    async def read_resource(self, uri: str) -> dict[str, Any]:
        return await self.request("resources/read", {"uri": uri})


class _PipeFault(McpError):
    """A fault that leaves the pipe unusable."""


class StdioClient(McpClient):
    """A live connection to one local stdio server."""

    def __init__(self, argv: Sequence[str], *, env: dict[str, str] | None = None,
                 timeout: float = DEFAULT_TIMEOUT_S, line_cap: int = LINE_CAP,
                 total_cap: int = TOTAL_CAP) -> None:
        if not argv:
            raise McpError("mcp server argv is empty")
        self.argv = list(argv)
        self.extra_env = dict(env or {})
        self.timeout = timeout
        self.line_cap = line_cap
        self.total_cap = total_cap
        self.proc: asyncio.subprocess.Process | None = None
        self.broken: str | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    async def start(self) -> None:
        if self.proc is not None:
            raise McpError("client already started")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={**scrub_env(), **self.extra_env},
                limit=self.line_cap + 1,
                start_new_session=True,
            )
        except OSError as exc:
            raise McpError(f"spawn failed: {exc}") from exc
        await self.request("initialize", initialize_params())
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized",
                          "params": {}}, locked=False)

    async def request(self, method: str, params: dict[str, Any], *,
                      timeout: float | None = None) -> dict[str, Any]:
        async with self._lock:
            if self.broken is not None:
                raise McpError(f"{method}: connection unusable ({self.broken})")
            try:
                return await self._exchange(method, params,
                                            self.timeout if timeout is None else timeout)
            except _PipeFault as exc:
                self.broken = str(exc)
                raise

    async def _exchange(self, method: str, params: dict[str, Any],
                        timeout: float) -> dict[str, Any]:
        self._next_id += 1
        req_id = self._next_id
        await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
                         locked=True)
        consumed = 0
        try:
            async with asyncio.timeout(timeout):
                for _ in range(_MAX_INTERLEAVED):
                    msg, size = await self._recv()
                    consumed += size
                    if consumed > self.total_cap:
                        raise McpError("server output exceeded total cap")
                    if "method" in msg:
                        if msg.get("method") == "ping" and msg.get("id") is not None:
                            await self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}},
                                             locked=True)
                        continue
                    if msg.get("id") != req_id:
                        continue  # a late reply to a request that was given up on
                    if msg.get("error") is not None:
                        raise McpError(f"{method} error: {msg['error']}")
                    result = msg.get("result")
                    if not isinstance(result, dict):
                        raise McpError(f"{method} result is not an object")
                    return result
        except TimeoutError:
            raise _PipeFault("timeout waiting for server response") from None
        raise McpError(f"{method}: too many interleaved messages before response")

    async def _send(self, obj: dict[str, Any], *, locked: bool) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise McpError("client not started")
        line = json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            self.proc.stdin.write(line)
            await self.proc.stdin.drain()
        except (OSError, RuntimeError) as exc:
            if not locked:
                self.broken = f"write failed: {exc}"
            raise _PipeFault(f"write failed: {exc}") from exc

    async def _recv(self) -> tuple[dict[str, Any], int]:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            line = await self.proc.stdout.readline()
        except (asyncio.LimitOverrunError, ValueError) as exc:
            raise _PipeFault("server response line exceeded cap") from exc
        except OSError as exc:
            raise _PipeFault(f"read failed: {exc}") from exc
        if not line:
            raise _PipeFault("server closed stdout (process exited?)")
        if len(line) > self.line_cap:
            raise _PipeFault("server response line exceeded cap")
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpError(f"malformed JSON-RPC line: {exc}") from exc
        if not isinstance(obj, dict):
            raise McpError("JSON-RPC message is not an object")
        return obj, len(line)

    async def close(self) -> None:
        """Close stdin, terminate, wait, kill: bounded, idempotent, never
        raises."""
        if self._closed:
            return
        self._closed = True
        proc = self.proc
        if proc is None:
            return
        with contextlib.suppress(Exception):
            if proc.stdin is not None:
                proc.stdin.close()
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 2.0)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(proc.wait(), 2.0)
