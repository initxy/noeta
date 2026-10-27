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
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Mapping
from typing import Any

import httpx

from noeta.ext.mcp.config import DEFAULT_TIMEOUT_S, McpError
from noeta.ext.mcp.stdio import LINE_CAP, TOTAL_CAP, McpClient, initialize_params

__all__ = ["CONNECTION_ID_HEADER", "HttpClient"]

CONNECTION_ID_HEADER = "Mcp-Session-Id"
#: Teardown is bounded: a server that stopped answering must not hold up a
#: retire or a shutdown.
_DELETE_TIMEOUT_S = 5.0


def _sse_reply(line: str, req_id: object) -> dict[str, Any] | None:
    """The reply to ``req_id`` carried by one SSE line, or ``None``. Ids
    compare as strings (a server may echo ``"1"`` for ``1``)."""
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
    if not isinstance(obj, dict) or "method" in obj or str(obj.get("id")) != str(req_id):
        return None
    return obj


class _TransportFault(McpError):
    pass


class HttpClient(McpClient):
    """A connection to one remote MCP endpoint. ``http`` lets a host route
    through its own ``httpx.AsyncClient`` (proxy, mTLS); otherwise the client
    owns one."""

    def __init__(self, url: str, *, headers: Mapping[str, str] | None = None,
                 timeout: float = DEFAULT_TIMEOUT_S, total_cap: int = TOTAL_CAP,
                 http: httpx.AsyncClient | None = None) -> None:
        if not url:
            raise McpError("mcp http server url is empty")
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.total_cap = total_cap
        self._http = http
        self._owns_http = http is None
        self.connection_id: str | None = None
        self.broken: str | None = None
        self._next_id = 0
        self._started = False
        self._closed = False

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient()
        return self._http

    async def start(self) -> None:
        if self._started:
            raise McpError("client already started")
        self._started = True
        await self.request("initialize", initialize_params())
        if self.connection_id is not None:
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized",
                              "params": {}}, self.timeout)

    async def request(self, method: str, params: dict[str, Any], *,
                      timeout: float | None = None) -> dict[str, Any]:
        self._next_id += 1
        req_id = self._next_id
        msg = await self._post({"jsonrpc": "2.0", "id": req_id, "method": method,
                                "params": params},
                               self.timeout if timeout is None else timeout)
        if msg is None:
            raise McpError(f"{method}: no matching JSON-RPC response in body")
        if msg.get("error") is not None:
            raise McpError(f"{method} error: {msg['error']}")
        result = msg.get("result")
        if not isinstance(result, dict):
            raise McpError(f"{method} result is not an object")
        return result

    async def _post(self, body: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        method = body.get("method")
        held = self.connection_id
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", **self.headers}
        if held is not None:
            headers[CONNECTION_ID_HEADER] = held
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        try:
            async with asyncio.timeout(timeout):
                async with self._client().stream("POST", self.url, content=data,
                                                 headers=headers) as resp:
                    if resp.status_code >= 400:
                        if resp.status_code == 404 and held is not None \
                                and self.connection_id == held:
                            self.connection_id = None
                        raise McpError(f"{method} http error: {resp.status_code} "
                                       f"{resp.reason_phrase}".rstrip())
                    assigned = resp.headers.get(CONNECTION_ID_HEADER)
                    if assigned and assigned.strip() and self.connection_id is None:
                        self.connection_id = assigned.strip()
                    if "id" not in body:
                        return None  # a notification: 202 and no body
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
                found = _sse_reply(line, req_id)
                if found is not None:
                    return found
            return None
        consumed = 0
        async for line in resp.aiter_lines():
            consumed += len(line) + 1
            if consumed > self.total_cap or len(line) > LINE_CAP:
                raise McpError("server output exceeded total cap")
            found = _sse_reply(line, req_id)
            if found is not None:
                return found
        return None

    async def close(self) -> None:
        """End the session (best-effort ``DELETE``) and close an owned
        transport. Idempotent; never raises."""
        if self._closed:
            return
        self._closed = True
        held, self.connection_id = self.connection_id, None
        if held is not None and self._http is not None:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(min(self.timeout, _DELETE_TIMEOUT_S)):
                    await self._http.request(
                        "DELETE", self.url, headers={**self.headers, CONNECTION_ID_HEADER: held})
        if self._owns_http and self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()

