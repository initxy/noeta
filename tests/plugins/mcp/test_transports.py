"""The remote transports beyond Streamable HTTP requests: server requests on
an HTTP stream, the legacy HTTP+SSE pair, WebSocket, and ``headersHelper``.
Every server is in-process (an httpx mock transport or a loopback socket)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from noeta.plugins.mcp.client import RequestContext
from noeta.plugins.mcp.config import McpError, McpServer
from noeta.plugins.mcp.http_client import PROTOCOL_HEADER, HttpClient
from noeta.plugins.mcp.pool import McpPool, connect, helper_headers
from noeta.plugins.mcp.sse_client import SseClient
from noeta.plugins.mcp.ws_client import WsClient, accept_key, frame, read_frame


def _result(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "initialize":
        return {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "t"}}
    if method == "tools/list":
        return {"tools": [{"name": "t"}]}
    if method == "tools/call":
        return {"content": [{"type": "text", "text": f"ran {params.get('name')}"}]}
    return {}


async def _elicit(params: dict[str, Any]) -> dict[str, Any]:
    return {"action": "accept", "content": {"echo": params.get("message")}}


# ---------------------------------------------------------------- Streamable HTTP


async def test_http_answers_a_server_request_on_the_stream_and_sends_its_version() -> None:
    posts: list[tuple[dict[str, Any], dict[str, str]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        posts.append((body, dict(request.headers)))
        if "method" not in body or "id" not in body:
            return httpx.Response(202)
        reply = {"jsonrpc": "2.0", "id": body["id"],
                 "result": _result(body["method"], body.get("params") or {})}
        if body["method"] != "tools/call":
            return httpx.Response(200, json=reply)

        async def stream() -> AsyncIterator[bytes]:
            ask = {"jsonrpc": "2.0", "id": "q1", "method": "elicitation/create",
                   "params": {"message": "name?"}}
            yield f"data: {json.dumps(ask)}\n\n".encode()
            yield b'data: {"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n\n'
            while not any(p.get("id") == "q1" and "result" in p for p, _ in posts):
                await asyncio.sleep(0.01)
            yield f"data: {json.dumps(reply)}\n\n".encode()

        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              content=stream())

    client = HttpClient("http://mcp.test/mcp",
                        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await client.start()
    with client.bound(RequestContext(elicit=_elicit)):
        result = await asyncio.wait_for(client.call_tool("t", {}), 5)
    await client.close()
    assert result["content"][0]["text"] == "ran t"
    answer = next(p for p, _ in posts if p.get("id") == "q1")
    assert answer["result"] == {"action": "accept", "content": {"echo": "name?"}}
    assert client.tools_changed
    init_headers = posts[0][1]
    assert PROTOCOL_HEADER.lower() not in init_headers
    assert posts[-1][1][PROTOCOL_HEADER.lower()] == "2025-06-18"
    caps = posts[0][0]["params"]["capabilities"]
    assert caps["elicitation"] == {"form": {}, "url": {}}


async def test_http_without_auth_reports_a_401() -> None:
    client = HttpClient("http://mcp.test/", http=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(401))))
    with pytest.raises(McpError, match="401"):
        await client.start()
    await client.close()


# ---------------------------------------------------------------- legacy SSE


class FakeSse:
    """A legacy HTTP+SSE server: GET opens the stream, POSTs go to the
    endpoint the first event names, replies come back on the stream."""

    def __init__(self, *, endpoint: bool = True) -> None:
        self.outbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.endpoint = endpoint
        self.headers: dict[str, str] = {}
        self.asking: object = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.headers = dict(request.headers)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                  content=self._stream())
        body = json.loads(request.content)
        self.posts.append((str(request.url), body))
        if "method" in body and "id" in body:
            if body["method"] == "tools/call" and body["params"]["name"] == "ask":
                self.asking = body["id"]
                await self.outbox.put({"jsonrpc": "2.0", "id": 9, "method": "roots/list"})
                return httpx.Response(202)
            if body["method"] == "tools/call" and body["params"]["name"] == "hang":
                return httpx.Response(202)
            await self.outbox.put({"jsonrpc": "2.0", "id": body["id"],
                                   "result": _result(body["method"], body["params"])})
        elif body.get("id") == 9:  # our answer to roots/list
            await self.outbox.put({"jsonrpc": "2.0", "id": self.asking,
                                   "result": {"content": [{"type": "text",
                                                           "text": json.dumps(body["result"])}]}})
        return httpx.Response(202)

    async def _stream(self) -> AsyncIterator[bytes]:
        yield b": hello\n\n"
        if self.endpoint:
            yield b"event: endpoint\ndata: /messages?session=abc\n\n"
        while True:
            msg = await self.outbox.get()
            if msg is None:
                return
            yield f"event: message\ndata: {json.dumps(msg)}\n\n".encode()


def sse_client(server: FakeSse, **kw: Any) -> SseClient:
    return SseClient("http://mcp.test/sse", headers={"X-Key": "k"},
                     http=httpx.AsyncClient(transport=httpx.MockTransport(server)), **kw)


async def test_sse_posts_to_the_endpoint_and_reads_replies_from_the_stream(
        tmp_path: Path) -> None:
    server = FakeSse()
    client = sse_client(server)
    await client.start()
    try:
        assert client.endpoint == "http://mcp.test/messages?session=abc"
        assert [t["name"] for t in await client.list_tools()] == ["t"]
        with client.bound(RequestContext(roots=[str(tmp_path)])):
            result = await asyncio.wait_for(client.call_tool("ask", {}), 5)
        roots = json.loads(result["content"][0]["text"])["roots"]
        assert roots == [{"uri": tmp_path.resolve().as_uri(), "name": tmp_path.name}]
        assert {url for url, _ in server.posts} == {"http://mcp.test/messages?session=abc"}
        assert server.headers["x-key"] == "k"
        assert [b.get("method") for _, b in server.posts][:2] == [
            "initialize", "notifications/initialized"]
        # A call that never gets its reply times out; the stream is still good.
        with pytest.raises(McpError, match="timeout"):
            await client.call_tool("hang", {}, timeout=0.1)
        assert client.broken is None
        # The stream ending breaks the connection.
        await server.outbox.put(None)
        for _ in range(100):
            if client.broken:
                break
            await asyncio.sleep(0.01)
        assert client.broken
        with pytest.raises(McpError, match="unusable"):
            await client.list_tools()
    finally:
        await client.close()


async def test_sse_without_an_endpoint_event_fails_to_start() -> None:
    client = sse_client(FakeSse(endpoint=False), timeout=0.2)
    with pytest.raises(McpError, match="endpoint"):
        await client.start()
    await client.close()


# ---------------------------------------------------------------- WebSocket


class FakeWs:
    """A loopback WebSocket MCP server."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> str:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}/mcp"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        self.requests.append(head)
        key = next(line.split(":", 1)[1].strip() for line in head.split("\r\n")
                   if line.lower().startswith("sec-websocket-key:"))
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\nSec-WebSocket-Protocol: mcp\r\n"
                      f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n").encode())
        writer.write(frame(0x9, b"hi", mask=False))  # a ping first
        try:
            while True:
                _final, opcode, payload = await read_frame(reader)
                if opcode == 0x8:
                    break
                if opcode != 0x1:
                    continue
                msg = json.loads(payload)
                if "method" not in msg or "id" not in msg:
                    continue
                if msg["method"] == "tools/call" and msg["params"]["name"] == "bye":
                    break
                reply = json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                    "result": _result(msg["method"], msg["params"])}).encode()
                # Split the reply in two fragments.
                half = len(reply) // 2
                writer.write(bytes([0x01, half]) + reply[:half] if half < 126 else
                             frame(0x1, reply, mask=False))
                if half < 126:
                    rest = reply[half:]
                    writer.write(bytes([0x80, len(rest)]) + rest)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        writer.close()

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()


async def test_ws_handshake_frames_and_close() -> None:
    fake = FakeWs()
    url = await fake.start()
    client = WsClient(url, headers={"Authorization": "Bearer k"})
    try:
        await client.start()
        assert [t["name"] for t in await client.list_tools()] == ["t"]
        assert (await client.call_tool("x", {}))["content"][0]["text"] == "ran x"
        head = fake.requests[0]
        assert "Sec-WebSocket-Protocol: mcp" in head and "Authorization: Bearer k" in head
        with pytest.raises(McpError):
            await client.call_tool("bye", {}, timeout=2)
        assert client.broken
    finally:
        await client.close()
        await fake.close()


async def test_ws_refused_handshake_and_bad_url() -> None:
    async def refuse(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(refuse, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = WsClient(f"ws://127.0.0.1:{port}/")
    with pytest.raises(McpError, match="refused"):
        await client.start()
    await client.close()
    server.close()
    with pytest.raises(McpError, match="ws://"):
        WsClient("http://x/")


def test_ws_frames_round_trip() -> None:
    long = b"x" * 70_000
    for payload in (b"", b"short", b"y" * 300, long):
        data = frame(0x1, payload)

        async def read(data: bytes = data) -> tuple[bool, int, bytes]:
            reader = asyncio.StreamReader()
            reader.feed_data(data)
            return await read_frame(reader)

        assert asyncio.run(read()) == (True, 0x1, payload)
    assert base64.b64decode(accept_key("dGhlIHNhbXBsZSBub25jZQ=="))  # RFC 6455's example
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


# ---------------------------------------------------------------- headersHelper


async def test_headers_helper_runs_per_connection(tmp_path: Path) -> None:
    script = tmp_path / "helper.py"
    script.write_text("import json, os\nprint(json.dumps({'Authorization': 'Bearer dyn',"
                      " 'X-Server': os.environ['NOETA_MCP_SERVER_NAME'] + ' '"
                      " + os.environ['NOETA_MCP_SERVER_URL']}))\n")
    server = McpServer(alias="h", url="http://mcp.test/mcp", headers={"Authorization": "x",
                                                                     "X-Static": "s"},
                       headers_helper=f"{sys.executable} {script}")
    seen: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"),
                                         "result": _result(body["method"], {})})

    pool = McpPool(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        await pool.use(server, None, lambda c: c.list_tools())
    finally:
        await pool.close()
    assert seen[0]["authorization"] == "Bearer dyn"
    assert seen[0]["x-static"] == "s"
    assert seen[0]["x-server"] == "h http://mcp.test/mcp"


@pytest.mark.parametrize("command", ["exit 3", "echo not-json", "echo '{\"a\": 1}'"])
async def test_a_failing_headers_helper_gives_no_headers(command: str) -> None:
    server = McpServer(alias="h", url="http://x", headers_helper=command)
    assert await helper_headers(server) == {}


async def test_a_headers_helper_inherits_the_environment_but_noetas_credentials(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOETA_API_KEY", "sk")
    monkeypatch.setenv("NOETA_STORE", "postgresql://noeta:pw@db/noeta")
    monkeypatch.setenv("SSO_TOKEN", "t")
    script = ("printf '{\"a\": \"%s|%s|%s\"}' \"$SSO_TOKEN\" "
              "\"${NOETA_API_KEY:-unset}\" \"${NOETA_STORE:-unset}\"")
    server = McpServer(alias="h", url="http://x", headers_helper=script)
    assert await helper_headers(server) == {"a": "t|unset|unset"}


async def test_a_slow_headers_helper_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("noeta.plugins.mcp.pool.HELPER_TIMEOUT_S", 0.2)
    assert await helper_headers(McpServer(alias="h", url="http://x",
                                          headers_helper="sleep 5")) == {}


def test_connect_picks_the_transport() -> None:
    assert isinstance(connect(McpServer(alias="a", type="sse", url="http://x")), SseClient)
    assert isinstance(connect(McpServer(alias="a", type="ws", url="ws://x")), WsClient)
    assert isinstance(connect(McpServer(alias="a", url="http://x")), HttpClient)
    assert os.environ.get("NOETA_MCP_SERVER_NAME") is None
