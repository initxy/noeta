"""The Streamable HTTP MCP client (request/response subset).

Every JSON-RPC message is one ``POST`` to the server's endpoint. The reply is
either a JSON object or a ``text/event-stream`` body; on a stream the client
returns the moment the ``data:`` object whose ``id`` matches arrives and
never reads further, so a server that keeps the stream open (keep-alives,
pushes) costs nothing. The whole exchange is bounded by the call's timeout,
not just the gap between two reads.

Sessions: a server may assign an ``Mcp-Session-Id`` on ``initialize`` and
reject later requests without it. The client keeps that id, sends the
lifecycle's ``notifications/initialized`` (only when an id was assigned, so a
stateless server sees exactly the traffic it always did), echoes the id on
every later request, and ``DELETE``\\ s it when the connection is closed. A
``404`` while an id is held means the server dropped the session: the id is
forgotten and the call fails like a dead connection, which the pool heals by
connecting again. The id and the configured headers are credentials: sent on
the wire only, never logged or recorded.

A server may ask the client something (``roots/list``,
``elicitation/create``) on the stream of a request of ours: the question is
answered with a ``POST`` of its own and the stream read on
(:meth:`McpClient.answer`). A ``401`` goes to the connection's ``auth``
(:class:`~noeta.plugins.mcp.oauth.OAuth`): a renewed token is tried once,
otherwise the call fails with ``McpAuthRequired``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

import httpx

from noeta.plugins.mcp.client import (
    McpClient,
    cancelled_notice,
    initialize_params,
    reply_result,
    server_instructions,
)
from noeta.plugins.mcp.config import DEFAULT_TIMEOUT_S, McpAuthRequired, McpError
from noeta.plugins.mcp.stdio import LINE_CAP, TOTAL_CAP
from noeta.toolkit.http import DIRECT_LOOPBACK

if TYPE_CHECKING:
    from noeta.plugins.mcp.oauth import OAuth

__all__ = ["CONNECTION_ID_HEADER", "PROTOCOL_HEADER", "HeadersFn", "HttpClient"]

CONNECTION_ID_HEADER = "Mcp-Session-Id"
PROTOCOL_HEADER = "MCP-Protocol-Version"
#: Headers worked out when a connection opens (a ``headersHelper``).
HeadersFn = Callable[[], Awaitable[dict[str, str]]]
#: Teardown is bounded: a server that stopped answering must not hold up a
#: retire or a shutdown.
_DELETE_TIMEOUT_S = 5.0


def _sse_message(line: str) -> dict[str, Any] | None:
    """The JSON-RPC message one SSE ``data:`` line carries, or ``None``."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    if not payload:
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _is_reply(msg: dict[str, Any], req_id: object) -> bool:
    """Ids compare as strings (a server may echo ``"1"`` for ``1``)."""
    return "method" not in msg and str(msg.get("id")) == str(req_id)


class _TransportFault(McpError):
    pass


class _Refused(Exception):
    """A 401; ``challenge`` is its ``WWW-Authenticate``."""

    def __init__(self, challenge: str) -> None:
        super().__init__(challenge)
        self.challenge = challenge


class HttpClient(McpClient):
    """A connection to one remote MCP endpoint. ``http`` lets a host route
    through its own ``httpx.AsyncClient`` (proxy, mTLS); otherwise the client
    owns one."""

    def __init__(self, url: str, *, headers: Mapping[str, str] | None = None,
                 timeout: float = DEFAULT_TIMEOUT_S, total_cap: int = TOTAL_CAP,
                 http: httpx.AsyncClient | None = None, auth: OAuth | None = None,
                 headers_fn: HeadersFn | None = None, bare_elicitation: bool = False) -> None:
        if not url:
            raise McpError("mcp http server url is empty")
        super().__init__()
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.total_cap = total_cap
        self.auth = auth
        self.bare_elicitation = bare_elicitation
        self._headers_fn = headers_fn
        self._http = http
        self._owns_http = http is None
        self.connection_id: str | None = None
        self.protocol_version: str | None = None
        self._next_id = 0
        self._started = False
        self._closed = False
        self._cancelling: set[asyncio.Task[None]] = set()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(mounts=DIRECT_LOOPBACK)
        return self._http

    async def start(self) -> None:
        if self._started:
            raise McpError("client already started")
        self._started = True
        if self._headers_fn is not None:
            self.headers = {**self.headers, **await self._headers_fn()}
        result = await self.request("initialize", initialize_params(
            bare_elicitation=self.bare_elicitation))
        self.instructions = server_instructions(result)
        version = result.get("protocolVersion")
        self.protocol_version = version if isinstance(version, str) and version else None
        if self.connection_id is not None:
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized",
                              "params": {}}, self.timeout)

    async def request(self, method: str, params: dict[str, Any], *,
                      timeout: float | None = None) -> dict[str, Any]:
        self._next_id += 1
        req_id = self._next_id
        try:
            msg = await self._post({"jsonrpc": "2.0", "id": req_id, "method": method,
                                    "params": params},
                                   self.timeout if timeout is None else timeout)
        except asyncio.CancelledError:
            if method != "initialize" and self.broken is None:
                # An interrupt or a cancelled task: told without delaying it.
                task = asyncio.create_task(self._cancel_remote(req_id))
                self._cancelling.add(task)
                task.add_done_callback(self._cancelling.discard)
            raise
        if msg is None:
            raise McpError(f"{method}: no matching JSON-RPC response in body")
        return reply_result(method, msg)

    async def _cancel_remote(self, req_id: int) -> None:
        with contextlib.suppress(Exception):
            await self._post(cancelled_notice(req_id, "cancelled"), self.timeout)

    async def _post(self, body: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        retried = False
        while True:
            sent = await self.auth.header() if self.auth is not None else None
            try:
                return await self._post_once(body, timeout, sent)
            except _Refused as refused:
                if self.auth is None:
                    raise McpError(f"{body.get('method')} http error: 401 Unauthorized"
                                   ) from None
                if retried or not await self.auth.refused(sent):
                    raise McpAuthRequired(
                        f"{body.get('method')}: the server requires authentication",
                        refused.challenge) from None
                retried = True

    async def _post_once(self, body: dict[str, Any], timeout: float,
                         bearer: str | None) -> dict[str, Any] | None:
        method = body.get("method")
        held = self.connection_id
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", **self.headers}
        if bearer is not None:
            headers["Authorization"] = bearer
        if held is not None:
            headers[CONNECTION_ID_HEADER] = held
        if self.protocol_version is not None:
            headers[PROTOCOL_HEADER] = self.protocol_version
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        try:
            # The call's own timeout bounds it, not httpx's 5 s default
            # (a host's client included): a slow tool is no broken session.
            async with asyncio.timeout(timeout):
                async with self._client().stream("POST", self.url, content=data,
                                                 headers=headers, timeout=timeout) as resp:
                    if resp.status_code == 401:
                        raise _Refused(resp.headers.get("WWW-Authenticate", ""))
                    if resp.status_code >= 400:
                        if resp.status_code == 404 and held is not None \
                                and self.connection_id == held:
                            self.connection_id = None
                            self.broken = "the server ended the session"
                        raise McpError(f"{method} http error: {resp.status_code} "
                                       f"{resp.reason_phrase}".rstrip())
                    assigned = resp.headers.get(CONNECTION_ID_HEADER)
                    if assigned and assigned.strip() and self.connection_id is None:
                        self.connection_id = assigned.strip()
                    if "id" not in body or "method" not in body:
                        return None  # a notification or an answer: 202 and no body
                    return await self._read_reply(resp, method, body["id"])
        except TimeoutError:
            self.broken = f"{method}: timeout waiting for server response"
            raise _TransportFault(self.broken) from None
        except httpx.HTTPError as exc:
            self.broken = f"transport error: {exc}"
            raise McpError(f"{method} transport error: {exc}") from exc

    async def _read_reply(self, resp: httpx.Response, method: object,
                          req_id: object) -> dict[str, Any] | None:
        content_type = resp.headers.get("Content-Type", "").lower()
        if "text/event-stream" not in content_type:
            raw = bytearray()
            async for chunk in resp.aiter_bytes():
                raw += chunk
                if len(raw) > self.total_cap:
                    raise McpError("server output exceeded total cap")
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                raise McpError(f"{method}: empty response body")
            if text[0] == "{":
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise McpError(f"{method}: malformed JSON response: {exc}") from exc
                if isinstance(obj, dict):
                    return obj
                raise McpError(f"{method}: JSON-RPC response is not an object")
            for line in text.splitlines():
                found = _sse_message(line)
                if found is not None and _is_reply(found, req_id):
                    return found
            return None
        consumed = 0
        async for line in resp.aiter_lines():
            consumed += len(line) + 1
            if consumed > self.total_cap or len(line) > LINE_CAP:
                raise McpError("server output exceeded total cap")
            found = _sse_message(line)
            if found is None:
                continue
            if _is_reply(found, req_id):
                return found
            if "method" in found:
                await self._answer_server(found)
        return None

    async def _answer_server(self, msg: dict[str, Any]) -> None:
        """Answer a request the server made on a stream of ours (and note a
        notification)."""
        reply = await self.answer(msg)
        if reply is not None:
            await self._post(reply, self.timeout)

    async def close(self) -> None:
        """End the session (best-effort ``DELETE``) and close an owned
        transport. Idempotent; never raises."""
        if self._closed:
            return
        self._closed = True
        for task in list(self._cancelling):
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, _DELETE_TIMEOUT_S)
        held, self.connection_id = self.connection_id, None
        if held is not None and self._http is not None:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(min(self.timeout, _DELETE_TIMEOUT_S)):
                    await self._http.request(
                        "DELETE", self.url, headers={**self.headers, CONNECTION_ID_HEADER: held})
        if self._owns_http and self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()

