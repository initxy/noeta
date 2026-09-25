"""HTTP transport: an SSE reply returns at the matching JSON-RPC id.

A server may keep a ``text/event-stream`` open after the reply (keep-alive
comments, pushes). The client's own transport used to read to end of stream,
with the timeout only bounding the gap between reads, so such a stream never
returned. It now stops at the reply; a server request carrying our id is not
mistaken for it; and a stream that never carries the reply ends at the
timeout. Also covers the stdio side of that rule: a server ``ping`` whose id
equals the pending request's is answered, not read as the reply.
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

from noeta.builtins.mcp.impl import McpHttpClient, McpStdioClient
from noeta.runtime.mcp import McpError

_FAKE = str(Path(__file__).parent / "_fixtures" / "fake_mcp_server.py")


def _handler(mode: str) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
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
            result = {"tools": [{"name": "t"}]} if req["method"] == "tools/list" else {}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                if mode == "never" and req["method"] != "initialize":
                    for _ in range(40):
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        time.sleep(0.1)
                    return
                if mode == "server_request":
                    ping = {"jsonrpc": "2.0", "id": req["id"], "method": "ping"}
                    self.wfile.write(f"data: {json.dumps(ping)}\n\n".encode())
                reply = {"jsonrpc": "2.0", "id": req["id"], "result": result}
                self.wfile.write(f"event: message\ndata: {json.dumps(reply)}\n\n".encode())
                self.wfile.flush()
                for _ in range(40):  # keep the stream open ~4 s
                    time.sleep(0.1)
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except OSError:
                pass

    return H


def _serve(mode: str) -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(mode))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    finally:
        srv.shutdown()


@pytest.fixture
def open_stream() -> Iterator[str]:
    yield from _serve("open")


@pytest.fixture
def request_first() -> Iterator[str]:
    yield from _serve("server_request")


@pytest.fixture
def never_replies() -> Iterator[str]:
    yield from _serve("never")


def test_sse_reply_returns_before_stream_closes(open_stream: str) -> None:
    c = McpHttpClient(url=open_stream, timeout_s=3.0)
    t0 = time.monotonic()
    c.start()
    assert [t["name"] for t in c.list_tools()] == ["t"]
    assert time.monotonic() - t0 < 1.5


def test_sse_server_request_with_our_id_is_not_the_reply(request_first: str) -> None:
    c = McpHttpClient(url=request_first, timeout_s=3.0)
    c.start()
    assert [t["name"] for t in c.list_tools()] == ["t"]


def test_sse_without_reply_ends_at_timeout(never_replies: str) -> None:
    c = McpHttpClient(url=never_replies, timeout_s=1.0)
    c.start()
    t0 = time.monotonic()
    with pytest.raises(McpError):
        c.list_tools()
    assert time.monotonic() - t0 < 3.0
    assert c.broken


def test_stdio_ping_with_colliding_id_is_answered() -> None:
    c = McpStdioClient(argv=[sys.executable, "-u", _FAKE, "ping"])
    c.start()
    try:
        result = c.call_tool("echo", {})
        pong = json.loads(result["content"][0]["text"])
        assert pong["result"] == {} and "method" not in pong
        assert not c.broken
    finally:
        c.shutdown()
