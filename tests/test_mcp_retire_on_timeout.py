"""A timed-out / dead MCP connection is retired, and ``call_timeout_s``.

After a ``tools/call`` timeout the stdio pipe may still carry the late reply,
so the connection marks itself broken: later calls on it fail at once rather
than each waiting out the timeout again, and the tool wrapper retires it
from the pool so the next build connects fresh. ``call_timeout_s`` on the
spec bounds tool calls only; the handshake and list calls keep the default.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from noeta.builtins.mcp.impl import McpConnectionPool, build_mcp_tools
from noeta.protocols.tool import ToolContext
from noeta.runtime.mcp import McpConfigError, McpHttpServerSpec, McpServerSpec
from noeta.storage.memory import InMemoryContentStore

_FAKE = str(Path(__file__).parent / "_fixtures" / "fake_mcp_server.py")


def _ctx() -> ToolContext:
    return ToolContext(artifact_store=InMemoryContentStore())


def _spec(mode: str, **kw: object) -> McpServerSpec:
    return McpServerSpec(
        alias="fake", argv=(sys.executable, "-u", _FAKE, mode), **kw  # type: ignore[arg-type]
    )


def test_timeout_retires_pooled_connection_and_fails_fast() -> None:
    pool = McpConnectionPool(idle_ttl=None)
    spec = _spec("slow", call_timeout_s=0.5)  # the fake sleeps 5 s per call
    tools, clients, _ = build_mcp_tools((spec,), pool=pool)
    try:
        (tool,) = tools.values()
        t0 = time.monotonic()
        first = tool.invoke({"msg": "hi"}, _ctx())
        assert not first.success and "timeout" in (first.summary or "")
        assert time.monotonic() - t0 < 3.0
        # Retired: the next acquire connects a fresh client.
        fresh, reused = pool.acquire(spec)
        assert not reused and fresh is not clients[0]
        pool.release(fresh)
        # The broken connection no longer waits the timeout again.
        t1 = time.monotonic()
        second = tool.invoke({"msg": "hi"}, _ctx())
        assert not second.success and time.monotonic() - t1 < 0.3
    finally:
        pool.release_all(clients)
        pool.shutdown()


def test_eof_retires_pooled_connection() -> None:
    pool = McpConnectionPool(idle_ttl=None)
    spec = _spec("die")
    tools, clients, _ = build_mcp_tools((spec,), pool=pool)
    try:
        (tool,) = tools.values()
        assert not tool.invoke({}, _ctx()).success
        fresh, reused = pool.acquire(spec)
        assert not reused
        pool.release(fresh)
    finally:
        pool.release_all(clients)
        pool.shutdown()


def test_server_error_result_keeps_connection() -> None:
    pool = McpConnectionPool(idle_ttl=None)
    spec = _spec("error")
    tools, clients, _ = build_mcp_tools((spec,), pool=pool)
    try:
        (tool,) = tools.values()
        assert not tool.invoke({}, _ctx()).success
        again, reused = pool.acquire(spec)
        assert reused and again is clients[0]
        pool.release(again)
    finally:
        pool.release_all(clients)
        pool.shutdown()


def test_call_timeout_does_not_shorten_handshake_or_list() -> None:
    # A tiny call budget still lets the connect + list complete.
    tools, clients, _ = build_mcp_tools((_spec("echo", call_timeout_s=0.001),))
    try:
        assert "mcp__fake__echo" in tools
        assert clients[0]._timeout_s == 30.0  # noqa: SLF001
        assert clients[0]._call_timeout_s == 0.001  # noqa: SLF001
    finally:
        for c in clients:
            c.shutdown()


@pytest.mark.parametrize("bad", [0, -1.0])
def test_call_timeout_must_be_positive(bad: float) -> None:
    with pytest.raises(McpConfigError):
        _spec("echo", call_timeout_s=bad)
    with pytest.raises(McpConfigError):
        McpHttpServerSpec(alias="h", url="http://127.0.0.1/", call_timeout_s=bad)


# -- HTTP: the call budget reaches the client's own transport ---------------


class _SlowCall(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "id" not in req:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if req["method"] == "tools/call":
            time.sleep(2.0)
        result = {"tools": [{"name": "t"}]} if req["method"] == "tools/list" else {}
        body = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


@pytest.fixture
def slow_http() -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _SlowCall)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    finally:
        srv.shutdown()


def test_http_call_timeout_and_retire(slow_http: str) -> None:
    pool = McpConnectionPool(idle_ttl=None)
    spec = McpHttpServerSpec(alias="h", url=slow_http, call_timeout_s=0.3)
    tools, clients, _ = build_mcp_tools((spec,), pool=pool)
    try:
        (tool,) = tools.values()
        t0 = time.monotonic()
        res = tool.invoke({}, _ctx())
        assert not res.success and time.monotonic() - t0 < 1.5
        _, reused = pool.acquire(spec)
        assert not reused
    finally:
        pool.shutdown()
