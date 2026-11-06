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

from noeta.plugins.mcp.config import McpError, McpServer
from noeta.plugins.mcp.http_client import CONNECTION_ID_HEADER, HttpClient
from noeta.plugins.mcp.pool import McpPool
from noeta.plugins.mcp.stdio import MAX_LIST_PAGES, StdioClient, collect_pages

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


async def test_stdio_timeout_cancels_the_call_not_the_connection(tmp_path: Path) -> None:
    log = tmp_path / "log.txt"
    client = stdio(FAKE_LOG=str(log))
    await client.start()
    with pytest.raises(McpError, match="timeout"):
        await client.call_tool("sleep", {"seconds": 1}, timeout=0.2)
    assert client.broken is None
    # The late reply is dropped; the next call gets its own.
    assert (await client.call_tool("echo", {"text": "x"}))["content"][0]["text"] == "echo: x"
    assert "notifications/cancelled" in log.read_text()
    pid = client.pid
    assert pid is not None
    await client.close()
    await client.close()  # idempotent
    assert client.proc is not None and client.proc.returncode is not None


async def test_stdio_a_cancelled_call_is_cancelled_on_the_server(tmp_path: Path) -> None:
    log = tmp_path / "log.txt"
    client = stdio(FAKE_LOG=str(log))
    await client.start()
    try:
        call = asyncio.create_task(client.call_tool("sleep", {"seconds": 0.3}))
        await asyncio.sleep(0.1)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        for _ in range(100):
            if "notifications/cancelled" in log.read_text():
                break
            await asyncio.sleep(0.02)
        assert "notifications/cancelled" in log.read_text()
        assert client.broken is None
    finally:
        await client.close()


async def test_stdio_close_ends_what_the_server_started(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    client = StdioClient(["sh", "-c", f'sleep 300 & echo $! > {pidfile}; exec "$0" "$1"',
                          sys.executable, FAKE])
    await client.start()
    grandchild = int(pidfile.read_text())
    await client.close()
    for _ in range(100):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.02)
    else:
        os.kill(grandchild, 9)
        pytest.fail("the server's child outlived it")


async def test_stdio_a_server_not_reading_stdin_breaks_the_connection() -> None:
    client = StdioClient(["sleep", "300"], timeout=0.2)
    await client._open()
    try:
        with pytest.raises(McpError, match="stopped reading"):
            await asyncio.wait_for(client._send(
                {"jsonrpc": "2.0", "method": "x", "params": {"a": "x" * 1_000_000}}), 5)
        assert client.broken
    finally:
        await asyncio.wait_for(client.close(), 10)


async def test_stdio_env_is_inherited_without_noetas_credentials(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "x")
    monkeypatch.setenv("NOETA_STORE", "postgresql://noeta:pw@db/noeta")
    log = tmp_path / "env.txt"
    script = tmp_path / "s.py"
    script.write_text("import os,sys\nopen(sys.argv[1],'w').write(repr(sorted(os.environ)))\n")
    client = StdioClient([sys.executable, str(script), str(log)], env={"MINE": "1"})
    with pytest.raises(McpError):
        await client.start()
    await client.close()
    names = log.read_text()
    assert "SECRET_TOKEN" in names and "MINE" in names and "NOETA_STORE" not in names


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
    assert client.broken  # the pool connects afresh
    await client.close()


async def test_pool_reconnects_after_the_server_ends_the_session() -> None:
    server = FakeHttp()
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    pool = McpPool(connect=lambda s: HttpClient(s.url or "", http=http))
    spec = McpServer(alias="remote", url="http://mcp.test/mcp")
    try:
        await pool.use(spec, None, lambda c: c.call_tool("t", {}))
        server.expired, server.session = True, "s-2"
        with pytest.raises(McpError, match="404"):
            await pool.use(spec, None, lambda c: c.call_tool("t", {}))
        result = await pool.use(spec, None, lambda c: c.call_tool("t", {}))
        assert result["content"][0]["text"] == "ok"
    finally:
        await pool.close()


async def test_http_a_cancelled_call_is_cancelled_on_the_server() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body.get("method"))
        if body.get("method") == "tools/call":
            await asyncio.sleep(10)
        if "id" not in body:
            return httpx.Response(202)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})

    client = HttpClient("http://mcp.test/",
                        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await client.start()
    call = asyncio.create_task(client.call_tool("t", {}))
    await asyncio.sleep(0.05)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    await client.close()
    assert seen == ["initialize", "tools/call", "notifications/cancelled"]
    assert client.broken is None


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


async def test_http_a_call_is_bounded_by_its_own_timeout_not_httpxs_default() -> None:
    # httpx gives up on a read after 5 s by default: a slow tool failed and
    # its session was dropped.
    timeouts: dict[str, object] = {}
    server = FakeHttp()

    async def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        timeouts[method] = request.extensions["timeout"]
        return await server(request)

    client = HttpClient("http://mcp.test/mcp", timeout=30, http=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    await client.start()
    await client.call_tool("t", {}, timeout=600)
    await client.close()
    assert timeouts["initialize"] == {"connect": 30, "read": 30, "write": 30, "pool": 30}
    assert timeouts["tools/call"] == {"connect": 600, "read": 600, "write": 600, "pool": 600}


# ---------------------------------------------------------------- pool


def fake_server(alias: str = "fake", **env: str) -> McpServer:
    return McpServer(alias=alias, command=[sys.executable, FAKE], env=env)


async def _pid(client: Any) -> int:
    result = await client.call_tool("echo", {"text": ""})
    return int(result["structuredContent"]["pid"])


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


async def test_a_cancelled_starter_does_not_cancel_the_tasks_waiting_on_it() -> None:
    gate = asyncio.Event()
    starts = 0

    class SlowFirst(StdioClient):
        async def start(self) -> None:
            nonlocal starts
            starts += 1
            if starts == 1:
                await gate.wait()  # cancelled here
            await super().start()

    pool = McpPool(connect=lambda server: SlowFirst(server.command or []))
    server = fake_server()
    try:
        starter = asyncio.create_task(pool.use(server, None, _pid))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(pool.use(server, None, _pid))
        await asyncio.sleep(0.05)
        starter.cancel()
        assert await asyncio.wait_for(waiter, 10) > 0
        assert starter.cancelled() and starts == 2
    finally:
        await pool.close()


async def test_a_failed_connect_leaves_nothing_behind() -> None:
    pool = McpPool()
    with pytest.raises(McpError):
        await pool.use(McpServer(alias="x", command=["/no/such"]), None, _pid)
    assert len(pool) == 0


def test_server_spec_validation() -> None:
    from noeta.plugins.mcp.config import McpConfigError

    with pytest.raises(McpConfigError):
        McpServer(alias="Bad Alias", command=["x"])
    with pytest.raises(McpConfigError):
        McpServer(alias="a")
    with pytest.raises(McpConfigError):
        McpServer(alias="a", command=["x"], url="http://x")
    with pytest.raises(McpConfigError):
        McpServer(alias="a", command=["x"], call_timeout=0)
    assert os.path.exists(FAKE)


async def test_pool_shares_per_scope_and_reconnects_a_broken_one() -> None:
    pool = McpPool()
    server = fake_server()
    try:
        a = await pool.use(server, None, _pid)
        assert await pool.use(server, None, _pid) == a
        assert await pool.use(server, "scope-b", _pid) != a
        with pytest.raises(McpError):
            await pool.use(server, None, lambda c: c.call_tool("die", {}))
        assert await pool.use(server, None, _pid) != a  # the dead one was dropped
    finally:
        await pool.close()
    assert len(pool) == 0
