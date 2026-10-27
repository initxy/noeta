"""The stdio and Streamable HTTP clients, and the connection pool."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from noeta.ext.mcp.config import McpError, McpServer
from noeta.ext.mcp.http_client import CONNECTION_ID_HEADER, HttpClient
from noeta.ext.mcp.pool import McpPool
from noeta.ext.mcp.stdio import MAX_LIST_PAGES, StdioClient, collect_pages

FAKE = str(Path(__file__).with_name("fake_server.py"))


def stdio(**env: str) -> StdioClient:
    return StdioClient([sys.executable, FAKE], env=env)


# ---------------------------------------------------------------- stdio


async def test_stdio_handshake_list_and_call() -> None:
    client = stdio()
    await client.start()
    try:
        names = [t["name"] for t in await client.list_tools()]
        assert names[:3] == ["echo", "fail", "image"]
        result = await client.call_tool("echo", {"text": "hi"})
        assert result["content"][0]["text"] == "echo: hi"
        # A server-side error result is not a connection fault.
        assert (await client.call_tool("fail", {}))["isError"] is True
        assert client.broken is None
        with pytest.raises(McpError, match="tools/call error"):
            await client.call_tool("nope", {})
        assert client.broken is None
    finally:
        await client.close()


async def test_stdio_answers_ping_and_skips_notifications() -> None:
    client = stdio(FAKE_PING="1")
    await client.start()
    try:
        assert (await client.call_tool("echo", {"text": "x"}))["content"][0]["text"] == "echo: x"
    finally:
        await client.close()


async def test_stdio_follows_every_page(tmp_path: Path) -> None:
    tools = [{"name": f"t{i}", "inputSchema": {"type": "object"}} for i in range(7)]
    client = stdio(FAKE_TOOLS=json.dumps(tools), FAKE_PAGE="3")
    await client.start()
    try:
        assert [t["name"] for t in await client.list_tools()] == [f"t{i}" for i in range(7)]
    finally:
        await client.close()


async def test_a_cursor_loop_stops() -> None:
    calls = 0

    async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"tools": [{"name": "x"}], "nextCursor": f"c{calls}"}

    out = await collect_pages(request, "tools/list", "tools")
    assert calls == MAX_LIST_PAGES and len(out) == MAX_LIST_PAGES

    async def repeat(method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"tools": [{"name": "x"}], "nextCursor": "same"}

    assert len(await collect_pages(repeat, "tools/list", "tools")) == 2


async def test_stdio_dead_server_breaks_the_connection() -> None:
    client = stdio()
    await client.start()
    try:
        with pytest.raises(McpError, match="closed stdout"):
            await client.call_tool("die", {})
        assert client.broken
        with pytest.raises(McpError, match="unusable"):
            await client.call_tool("echo", {})
    finally:
        await client.close()


async def test_stdio_timeout_breaks_and_close_kills() -> None:
    client = stdio()
    await client.start()
    with pytest.raises(McpError, match="timeout"):
        await client.call_tool("sleep", {"seconds": 30}, timeout=0.3)
    assert client.broken
    pid = client.pid
    assert pid is not None
    await client.close()
    await client.close()  # idempotent
    assert client.proc is not None and client.proc.returncode is not None


async def test_stdio_env_is_scrubbed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "x")
    log = tmp_path / "env.txt"
    script = tmp_path / "s.py"
    script.write_text("import os,sys\nopen(sys.argv[1],'w').write(repr(sorted(os.environ)))\n")
    client = StdioClient([sys.executable, str(script), str(log)], env={"MINE": "1"})
    with pytest.raises(McpError):
        await client.start()
    await client.close()
    names = log.read_text()
    assert "SECRET_TOKEN" not in names and "MINE" in names


async def test_spawn_failure_is_an_mcp_error() -> None:
    client = StdioClient(["/no/such/binary"])
    with pytest.raises(McpError, match="spawn failed"):
        await client.start()


# ---------------------------------------------------------------- http


class FakeHttp:
    """A Streamable HTTP server as an httpx transport handler."""

    def __init__(self, *, session: str | None = "s-1", sse: bool = False,
                 hang_after_reply: bool = False, pages: int = 1) -> None:
        self.session = session
        self.sse = sse
        self.hang = hang_after_reply
        self.pages = pages
        self.seen: list[tuple[str, str | None, str | None]] = []
        self.expired = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        sid = request.headers.get(CONNECTION_ID_HEADER)
        if request.method == "DELETE":
            self.seen.append(("DELETE", None, sid))
            return httpx.Response(200)
        body = json.loads(request.content)
        method = body.get("method")
        self.seen.append(("POST", method, sid))
        if self.session and method != "initialize" and sid != self.session:
            return httpx.Response(400 if not self.expired else 404)
        if "id" not in body:
            return httpx.Response(202)
        result: dict[str, Any] = {}
        if method == "tools/list":
            n = int((body.get("params") or {}).get("cursor") or 0)
            result = {"tools": [{"name": f"t{n}"}]}
            if n + 1 < self.pages:
                result["nextCursor"] = str(n + 1)
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "ok"}]}
        reply = {"jsonrpc": "2.0", "id": str(body["id"]), "result": result}
        headers = {CONNECTION_ID_HEADER: self.session} if self.session and method == "initialize" \
            else {}
        if not self.sse:
            return httpx.Response(200, json=reply, headers=headers)

        async def stream() -> AsyncIterator[bytes]:
            yield b": keep-alive\n\n"
            yield b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            yield f"data: {json.dumps(reply)}\n\n".encode()
            if self.hang:
                await asyncio.sleep(3600)

        return httpx.Response(200, headers={**headers, "Content-Type": "text/event-stream"},
                              content=stream())


def http_client(server: FakeHttp, **kw: Any) -> HttpClient:
    return HttpClient("http://mcp.test/mcp", headers={"Authorization": "Bearer k"},
                      http=httpx.AsyncClient(transport=httpx.MockTransport(server)), **kw)


async def test_http_connection_id_is_joined_echoed_and_deleted() -> None:
    server = FakeHttp()
    client = http_client(server)
    await client.start()
    assert (await client.call_tool("t", {}))["content"][0]["text"] == "ok"
    await client.close()
    assert server.seen == [
        ("POST", "initialize", None),
        ("POST", "notifications/initialized", "s-1"),
        ("POST", "tools/call", "s-1"),
        ("DELETE", None, "s-1"),
    ]


async def test_http_stateless_server_sees_no_notification_and_no_delete() -> None:
    server = FakeHttp(session=None)
    client = http_client(server)
    await client.start()
    await client.call_tool("t", {})
    await client.close()
    assert [m for _, m, _ in server.seen] == ["initialize", "tools/call"]


async def test_http_sse_reply_returns_at_the_matching_id() -> None:
    server = FakeHttp(sse=True, hang_after_reply=True)
    client = http_client(server, timeout=5)
    await client.start()
    result = await asyncio.wait_for(client.call_tool("t", {}), 2)
    assert result["content"][0]["text"] == "ok"
    await client.close()


async def test_http_pagination_and_expired_connection() -> None:
    server = FakeHttp(pages=3)
    client = http_client(server)
    await client.start()
    assert [t["name"] for t in await client.list_tools()] == ["t0", "t1", "t2"]
    server.expired = True
    server.session = "s-2"
    with pytest.raises(McpError, match="404"):
        await client.call_tool("t", {})
    assert client.connection_id is None
    await client.close()


async def test_http_timeout_breaks_the_connection() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200)

    client = HttpClient("http://mcp.test/", timeout=0.2,
                        http=httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    with pytest.raises(McpError, match="timeout"):
        await client.start()
    assert client.broken
    await client.close()


# ---------------------------------------------------------------- pool


def fake_server(alias: str = "fake", **env: str) -> McpServer:
    return McpServer(alias=alias, command=[sys.executable, FAKE], env=env)


async def _pid(client: Any) -> int:
    result = await client.call_tool("echo", {"text": ""})
    return int(result["structuredContent"]["pid"])


async def test_pool_shares_per_scope_and_reconnects_a_broken_one() -> None:
    pool = McpPool()
    server = fake_server()
    try:
        a = await pool.use(server, None, _pid)
        assert await pool.use(server, None, _pid) == a
        assert await pool.use(server, "tenant-b", _pid) != a
        with pytest.raises(McpError):
            await pool.use(server, None, lambda c: c.call_tool("die", {}))
        assert await pool.use(server, None, _pid) != a  # the dead one was dropped
    finally:
        await pool.close()
    assert len(pool) == 0


async def test_pool_retries_a_list_once_on_a_reused_dead_connection() -> None:
    pool = McpPool()
    server = fake_server()
    try:
        async with pool.acquire(server) as client:
            assert isinstance(client, StdioClient) and client.proc is not None
            client.proc.kill()
            await client.proc.wait()
        tools = await pool.use(server, None, lambda c: c.list_tools(), retry=True)
        assert tools
    finally:
        await pool.close()


async def test_pool_expires_idle_connections() -> None:
    now = [0.0]
    pool = McpPool(idle_ttl=10, clock=lambda: now[0])
    server = fake_server()
    try:
        first = await pool.use(server, None, _pid)
        now[0] = 100
        assert await pool.use(server, None, _pid) != first
    finally:
        await pool.close()


async def test_pool_retire_closes_after_the_holder_is_done() -> None:
    pool = McpPool()
    server = fake_server()
    try:
        async with pool.acquire(server) as client:
            await pool.retire("fake")
            assert len(pool) == 0
            assert await _pid(client) > 0  # still usable by its holder
        assert isinstance(client, StdioClient) and client.proc is not None
        assert client.proc.returncode is not None or client._closed
    finally:
        await pool.close()


async def test_a_failed_connect_leaves_nothing_behind() -> None:
    pool = McpPool()
    with pytest.raises(McpError):
        await pool.use(McpServer(alias="x", command=["/no/such"]), None, _pid)
    assert len(pool) == 0


def test_server_spec_validation() -> None:
    from noeta.ext.mcp.config import McpConfigError

    with pytest.raises(McpConfigError):
        McpServer(alias="Bad Alias", command=["x"])
    with pytest.raises(McpConfigError):
        McpServer(alias="a")
    with pytest.raises(McpConfigError):
        McpServer(alias="a", command=["x"], url="http://x")
    with pytest.raises(McpConfigError):
        McpServer(alias="a", command=["x"], call_timeout=0)
    assert os.path.exists(FAKE)
