"""The legacy HTTP+SSE MCP client (``"type": "sse"``).

The client opens a ``GET`` event stream on the server's URL; the server's
first ``endpoint`` event names (relative to that URL) where the client
``POST``\\ s each JSON-RPC message, and every reply, server request and
notification comes back as a ``message`` event on the stream. Requests are
multiplexed over it (:class:`~noeta.plugins.mcp.client.StreamClient`); the
stream ending breaks the connection. A ``401`` on either side goes to the
connection's ``auth`` as on Streamable HTTP. Headers are credentials: sent
on the wire only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import urllib.parse
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any

import httpx

from noeta.plugins.mcp.client import StreamClient
from noeta.plugins.mcp.config import DEFAULT_TIMEOUT_S, McpAuthRequired, McpError
from noeta.plugins.mcp.stdio import LINE_CAP
from noeta.toolkit.http import DIRECT_LOOPBACK

if TYPE_CHECKING:
    from noeta.plugins.mcp.http_client import HeadersFn
    from noeta.plugins.mcp.oauth import OAuth

__all__ = ["SseClient"]


def _origin(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    port = parts.port or {"http": 80, "https": 443}.get(parts.scheme.lower())
    return parts.scheme.lower(), f"{(parts.hostname or '').lower()}:{port}"


class SseClient(StreamClient):
    """A connection to one legacy HTTP+SSE endpoint. ``http`` lets a host
    route through its own ``httpx.AsyncClient``; otherwise the client owns
    one."""

    def __init__(self, url: str, *, headers: Mapping[str, str] | None = None,
                 timeout: float = DEFAULT_TIMEOUT_S, http: httpx.AsyncClient | None = None,
                 auth: OAuth | None = None, headers_fn: HeadersFn | None = None,
                 bare_elicitation: bool = False) -> None:
        if not url:
            raise McpError("mcp sse server url is empty")
        super().__init__(timeout=timeout, bare_elicitation=bare_elicitation)
        self.url = url
        self.headers = dict(headers or {})
        self.auth = auth
        self.endpoint: str | None = None
        self._headers_fn = headers_fn
        self._http = http
        self._owns_http = http is None
        self._stream: contextlib.AbstractAsyncContextManager[httpx.Response] | None = None
        self._response: httpx.Response | None = None
        self._events: AsyncIterator[tuple[str, str]] | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.timeout, mounts=DIRECT_LOOPBACK)
        return self._http

    async def _with_auth(self, headers: dict[str, str]) -> str | None:
        sent = await self.auth.header() if self.auth is not None else None
        if sent is not None:
            headers["Authorization"] = sent
        return sent

    async def _refused(self, sent: str | None, challenge: str, retried: bool) -> None:
        """A 401: return to retry with a renewed token, else raise."""
        if self.auth is None:
            raise McpError("http error: 401 Unauthorized")
        if retried or not await self.auth.refused(sent):
            raise McpAuthRequired("the server requires authentication", challenge)

    async def _open(self) -> None:
        if self._headers_fn is not None:
            self.headers = {**self.headers, **await self._headers_fn()}
        retried = False
        while True:
            headers = {"Accept": "text/event-stream", **self.headers}
            sent = await self._with_auth(headers)
            stream = self._client().stream("GET", self.url, headers=headers,
                                           timeout=httpx.Timeout(self.timeout, read=None))
            try:
                async with asyncio.timeout(self.timeout):
                    resp = await stream.__aenter__()
            except TimeoutError:
                raise McpError("timeout opening the event stream") from None
            except httpx.HTTPError as exc:
                raise McpError(f"transport error: {exc}") from exc
            if resp.status_code == 401:
                await stream.__aexit__(None, None, None)
                await self._refused(sent, resp.headers.get("WWW-Authenticate", ""), retried)
                retried = True
                continue
            if resp.status_code >= 400:
                await stream.__aexit__(None, None, None)
                raise McpError(f"event stream http error: {resp.status_code}")
            self._stream, self._response = stream, resp
            break
        self._events = events = self._read_events(resp)
        try:
            async with asyncio.timeout(self.timeout):
                async for event, data in events:
                    if event == "endpoint":
                        endpoint = urllib.parse.urljoin(self.url, data.strip())
                        # Every POST carries the headers and the token.
                        if _origin(endpoint) != _origin(self.url):
                            raise McpError(f"the endpoint event names another origin: "
                                           f"{endpoint}")
                        self.endpoint = endpoint
                        return
        except TimeoutError:
            raise McpError("the server sent no endpoint event") from None
        raise McpError("the event stream ended before its endpoint event")

    @staticmethod
    async def _read_events(resp: httpx.Response) -> AsyncIterator[tuple[str, str]]:
        event = "message"
        data: list[str] = []
        async for line in resp.aiter_lines():
            if len(line) > LINE_CAP:
                raise McpError("server event exceeded cap")
            if not line:
                if data:
                    yield event, "\n".join(data)
                event, data = "message", []
            elif line.startswith(":"):
                continue
            elif line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data.append(line[len("data:"):].lstrip(" "))

    async def _messages(self) -> AsyncIterator[dict[str, Any]]:
        assert self._events is not None
        async for event, data in self._events:
            if event != "message":
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj
        raise McpError("server closed the event stream")

    async def _write(self, msg: dict[str, Any]) -> None:
        if self.endpoint is None:
            raise McpError("client not started")
        body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        retried = False
        while True:
            headers = {"Content-Type": "application/json", **self.headers}
            sent = await self._with_auth(headers)
            try:
                resp = await self._client().post(self.endpoint, content=body, headers=headers)
            except httpx.HTTPError as exc:
                raise McpError(f"transport error: {exc}") from exc
            if resp.status_code == 401:
                await self._refused(sent, resp.headers.get("WWW-Authenticate", ""), retried)
                retried = True
                continue
            if resp.status_code >= 400:
                raise McpError(f"{msg.get('method') or 'reply'} http error: "
                               f"{resp.status_code}")
            return

    async def _send(self, msg: dict[str, Any]) -> None:
        try:
            await self._write(msg)
        except McpAuthRequired:
            raise  # the stream is fine; the person must sign in
        except McpError as exc:
            raise McpError(f"{msg.get('method') or 'reply'}: {exc}") from exc

    async def _shut(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.__aexit__(None, None, None)
        if self._owns_http and self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()
