"""What every MCP connection shares, whatever its transport.

* **Requests out**: :class:`McpClient` is the request/response surface the
  extension uses (``tools/*``, ``prompts/*``, ``resources/*``), lists
  followed to their last page.
* **Requests in**: a server may ask the client something while a request of
  ours is open. ``ping`` is answered; ``roots/list`` answers the workspace
  directories of the request in flight; ``elicitation/create`` goes to the
  in-flight request's ``elicit`` (the extension turns it into a question for
  the person) and is declined when there is none. Anything else is "method
  not found".
* **Notifications in**: ``notifications/tools/list_changed`` sets
  ``tools_changed``; the rest is ignored.

Who is "in flight" is a :class:`RequestContext` the caller binds around its
requests (:meth:`McpClient.bound`); a connection pooled across tasks sends
a server request to the most recently bound context that can take it.

:class:`StreamClient` is the multiplexed form of a connection over one
message stream (stdio, the legacy SSE pair, a WebSocket): one reader routes
replies to their waiting requests by id, so requests run concurrently, a
timed-out request is cancelled (``notifications/cancelled``) without
spoiling the stream, and only a dead stream marks the connection
``broken``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from noeta.plugins.mcp.config import DEFAULT_TIMEOUT_S, McpError

__all__ = [
    "MAX_LIST_PAGES",
    "PROTOCOL_VERSION",
    "Elicit",
    "McpClient",
    "RequestContext",
    "StreamClient",
    "collect_pages",
    "initialize_params",
]

log = logging.getLogger("noeta.mcp")

PROTOCOL_VERSION = "2025-11-25"
#: Pages one list call follows: a server that keeps handing back a cursor
#: must not loop forever.
MAX_LIST_PAGES = 100
_CLIENT_INFO = {"name": "noeta", "version": "1"}
_METHOD_NOT_FOUND = -32601

Request = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
#: ``elicitation/create`` params -> the result (``{"action": ...}``).
Elicit = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


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


def initialize_params(*, bare_elicitation: bool = False) -> dict[str, Any]:
    """``initialize``'s params: roots (with change notices) and elicitation
    (form and url modes, or ``{}`` alone for a server that rejects them)."""
    elicitation: dict[str, Any] = {} if bare_elicitation else {"form": {}, "url": {}}
    return {"protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": True}, "elicitation": elicitation},
            "clientInfo": dict(_CLIENT_INFO)}


@dataclass
class RequestContext:
    """Whom a server request made during a request of ours is for: the
    task's workspace directories (``roots/list``) and how to ask its person
    (``elicitation/create``; ``None``: declined)."""

    roots: Sequence[str] = ()
    elicit: Elicit | None = None


def _roots(dirs: Sequence[str]) -> list[dict[str, str]]:
    out = []
    for d in dirs:
        path = Path(d).expanduser().resolve()
        out.append({"uri": path.as_uri(), "name": path.name or str(path)})
    return out


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


class McpClient:
    """What the extension needs from a connection, whatever the transport."""

    def __init__(self) -> None:
        self.broken: str | None = None
        #: A ``notifications/tools/list_changed`` arrived since it was cleared.
        self.tools_changed = False
        #: Answers server requests made outside any bound context (e.g.
        #: right after ``initialize``): the context of whoever opened the
        #: connection.
        self.fallback: RequestContext | None = None
        self._bound: list[RequestContext] = []

    async def start(self) -> None:
        raise NotImplementedError

    async def request(self, method: str, params: dict[str, Any], *,
                      timeout: float | None = None) -> dict[str, Any]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    @contextlib.contextmanager
    def bound(self, context: RequestContext | None) -> Iterator[None]:
        """Route server requests to ``context`` while the block runs."""
        if context is None:
            yield
            return
        self._bound.append(context)
        try:
            yield
        finally:
            self._bound.remove(context)

    def _context(self, need_elicit: bool = False) -> RequestContext | None:
        for ctx in reversed(self._bound):
            if not need_elicit or ctx.elicit is not None:
                return ctx
        fb = self.fallback
        return fb if fb is not None and (not need_elicit or fb.elicit is not None) else None

    async def answer(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """The reply to one server message (``None`` for a notification)."""
        method = msg.get("method")
        if "id" not in msg or msg.get("id") is None:
            if method == "notifications/tools/list_changed":
                self.tools_changed = True
            return None
        req_id = msg["id"]
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "roots/list":
            ctx = self._context()
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"roots": _roots(ctx.roots if ctx is not None else ())}}
        if method == "elicitation/create":
            ctx = self._context(need_elicit=True)
            if ctx is None or ctx.elicit is None:
                return {"jsonrpc": "2.0", "id": req_id, "result": {"action": "decline"}}
            try:
                result = await ctx.elicit(dict(params or {}))
            except Exception:
                log.exception("mcp: answering an elicitation failed")
                result = {"action": "cancel"}
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        return _error(req_id, _METHOD_NOT_FOUND, f"method not found: {method}")

    async def list_tools(self) -> list[dict[str, Any]]:
        return await collect_pages(self.request, "tools/list", "tools")

    async def call_tool(self, name: str, arguments: dict[str, Any], *,
                        timeout: float | None = None,
                        meta: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "arguments": dict(arguments)}
        if meta:
            params["_meta"] = dict(meta)
        return await self.request("tools/call", params, timeout=timeout)

    async def list_prompts(self) -> list[dict[str, Any]]:
        return await collect_pages(self.request, "prompts/list", "prompts")

    async def get_prompt(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.request("prompts/get", {"name": name, "arguments": dict(arguments)})

    async def list_resources(self) -> list[dict[str, Any]]:
        return await collect_pages(self.request, "resources/list", "resources")

    async def read_resource(self, uri: str) -> dict[str, Any]:
        return await self.request("resources/read", {"uri": uri})


def reply_result(method: str, msg: dict[str, Any]) -> dict[str, Any]:
    """A reply's ``result``, or the ``McpError`` its ``error`` says."""
    if msg.get("error") is not None:
        raise McpError(f"{method} error: {msg['error']}")
    result = msg.get("result")
    if not isinstance(result, dict):
        raise McpError(f"{method} result is not an object")
    return result


@dataclass
class _Waiting:
    method: str
    reply: asyncio.Future[dict[str, Any]] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future())


class StreamClient(McpClient):
    """A connection over one ordered message stream. Subclasses open the
    stream (``_open``), write one message (``_write``), yield what arrives
    (``_messages``) and shut it (``_shut``)."""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT_S,
                 bare_elicitation: bool = False) -> None:
        super().__init__()
        self.timeout = timeout
        self.bare_elicitation = bare_elicitation
        self._next_id = 0
        self._waiting: dict[str, _Waiting] = {}
        self._reader: asyncio.Task[None] | None = None
        self._answering: set[asyncio.Task[None]] = set()
        self._started = False
        self._closed = False

    # ------------------------------------------------ transport hooks

    async def _open(self) -> None:
        raise NotImplementedError

    async def _write(self, msg: dict[str, Any]) -> None:
        raise NotImplementedError

    def _messages(self) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError

    async def _shut(self) -> None:
        raise NotImplementedError

    # ------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._started:
            raise McpError("client already started")
        self._started = True
        await self._open()
        self._reader = asyncio.create_task(self._read_loop())
        await self.request("initialize", initialize_params(
            bare_elicitation=self.bare_elicitation))
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized",
                          "params": {}})

    async def request(self, method: str, params: dict[str, Any], *,
                      timeout: float | None = None) -> dict[str, Any]:
        if self.broken is not None:
            raise McpError(f"{method}: connection unusable ({self.broken})")
        self._next_id += 1
        req_id = self._next_id
        waiting = _Waiting(method)
        self._waiting[str(req_id)] = waiting
        try:
            await self._send({"jsonrpc": "2.0", "id": req_id, "method": method,
                              "params": params})
            try:
                async with asyncio.timeout(self.timeout if timeout is None else timeout):
                    msg = await waiting.reply
            except TimeoutError:
                with contextlib.suppress(Exception):
                    await self._send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                      "params": {"requestId": req_id, "reason": "timeout"}})
                raise McpError(f"{method}: timeout waiting for server response") from None
        finally:
            self._waiting.pop(str(req_id), None)
        return reply_result(method, msg)

    async def _send(self, msg: dict[str, Any]) -> None:
        try:
            await self._write(msg)
        except McpError as exc:
            self._fail(str(exc))
            raise
        except (OSError, RuntimeError) as exc:
            self._fail(f"write failed: {exc}")
            raise McpError(f"write failed: {exc}") from exc

    async def _read_loop(self) -> None:
        reason = "server closed the connection"
        try:
            async for msg in self._messages():
                self._dispatch(msg)
        except asyncio.CancelledError:
            reason = "connection closed"
            raise
        except McpError as exc:
            reason = str(exc)
        except Exception as exc:  # noqa: BLE001 — any read fault ends the stream
            reason = f"read failed: {exc}"
        finally:
            self._fail(reason)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "method" in msg:
            task = asyncio.create_task(self._answer_one(msg))
            self._answering.add(task)
            task.add_done_callback(self._answering.discard)
            return
        waiting = self._waiting.get(str(msg.get("id")))
        if waiting is not None and not waiting.reply.done():
            waiting.reply.set_result(msg)
        # else: a late reply to a request that was given up on

    async def _answer_one(self, msg: dict[str, Any]) -> None:
        reply = await self.answer(msg)
        if reply is not None and self.broken is None:
            with contextlib.suppress(McpError):
                await self._send(reply)

    def _fail(self, reason: str) -> None:
        if self.broken is None:
            self.broken = reason
        for waiting in self._waiting.values():
            if not waiting.reply.done():
                waiting.reply.set_exception(McpError(f"{waiting.method}: {reason}"))
                waiting.reply.exception()  # retrieved: a cancelled waiter never warns

    async def close(self) -> None:
        """Stop the reader and shut the stream: bounded, idempotent, never
        raises."""
        if self._closed:
            return
        self._closed = True
        tasks = [t for t in (self._reader, *self._answering) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        with contextlib.suppress(Exception):
            await self._shut()
