"""Streamable HTTP session handling in the remote MCP client.

A 2025-spec Streamable HTTP server assigns an ``Mcp-Session-Id`` on
``initialize`` and rejects every later request that does not carry it, so a
client that drops the id handshakes fine and then fails every ``tools/list`` —
the whole remote connector class is unusable. These drive the real client
against an in-process HTTP server on loopback (the default urllib transport,
response headers and all), so the echo, the teardown ``DELETE`` and the
expiry path are exercised for real rather than through a mock of our own
request shape.

The stateless half is pinned alongside it: a server that assigns no id must
see exactly the requests it saw before, and a host-supplied
``(req, headers) -> bytes`` transport — the only shape that existed before
sessions — must keep working end to end.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator, Mapping, Optional

import pytest

from noeta.builtins.mcp.impl import McpHttpClient, build_mcp_tools
from noeta.builtins.mcp.impl.pool import McpConnectionPool
from noeta.protocols.tool import ToolContext
from noeta.runtime.mcp import McpError, McpHttpResponse, McpHttpServerSpec
from noeta.storage.memory import InMemoryContentStore
from tests._fixtures.fake_http_mcp_server import FakeHttpMcpServer


_ID_HEADER = "Mcp-Session-Id"


# ---------------------------------------------------------------------------
# an in-process Streamable HTTP MCP server on loopback
# ---------------------------------------------------------------------------


class _ServerState:
    """What the fake server enforces and what it saw.

    ``stateful`` mints an id on ``initialize`` and 404s any later request that
    does not carry a live one (what the reference Python SDK does by default).
    It also holds the lifecycle strictly: until the session has received
    ``notifications/initialized`` every request but the handshake is refused.
    The reference SDK is laxer than that — it marks a session initialized as
    soon as it has answered ``initialize`` — but the lifecycle allows the
    strict reading and other implementations take it, so the fake is the
    stricter server a client has to satisfy.

    ``expire_after`` drops the id after N answered requests (a server restart
    / session timeout); ``delete_status`` lets the teardown ``DELETE`` be
    refused with a 405.
    """

    def __init__(
        self,
        *,
        stateful: bool,
        expire_after: Optional[int],
        delete_status: int,
    ) -> None:
        self.stateful = stateful
        self.expire_after = expire_after
        self.delete_status = delete_status
        self.live: set[str] = set()
        self.ready: set[str] = set()
        self.issued: list[str] = []
        #: ``(jsonrpc method, the id header the request carried)`` per request.
        self.requests: list[tuple[str, Optional[str]]] = []
        #: the same, for notifications only (no JSON-RPC ``id`` on the wire).
        self.notifications: list[tuple[str, Optional[str]]] = []
        #: the id each ``DELETE`` carried.
        self.deletes: list[Optional[str]] = []
        self.answered = 0


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    @property
    def _state(self) -> _ServerState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, *args: Any) -> None:  # keep the test output clean
        pass

    def _send(
        self, status: int, body: bytes, extra: Optional[dict[str, str]] = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        state = self._state
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        if self.path != "/mcp":
            self._send(404, b'{"error":"no such endpoint"}')
            return
        method = str(req.get("method", ""))
        carried = self.headers.get(_ID_HEADER)
        state.requests.append((method, carried))
        if method == "initialize":
            extra = {}
            if state.stateful:
                assigned = f"mcp-{len(state.issued) + 1}"
                state.issued.append(assigned)
                state.live.add(assigned)
                extra[_ID_HEADER] = assigned
            self._send(200, _envelope(req, _initialize_result()), extra)
            return
        if state.stateful and (carried is None or carried not in state.live):
            self._send(404, b'{"error":"Session not found"}')
            return
        if "id" not in req:  # a notification: 202 Accepted, empty body
            state.notifications.append((method, carried))
            if method == "notifications/initialized" and carried is not None:
                state.ready.add(carried)
            self._send(202, b"")
            return
        if state.stateful and carried not in state.ready:
            self._send(400, b'{"error":"Received request before initialization"}')
            return
        state.answered += 1
        if state.expire_after is not None and state.answered >= state.expire_after:
            state.live.clear()  # the server dropped it; the next request 404s
        self._send(200, _envelope(req, _result_for(req)))

    def do_DELETE(self) -> None:
        state = self._state
        carried = self.headers.get(_ID_HEADER)
        state.deletes.append(carried)
        state.live.discard(carried or "")
        self._send(state.delete_status, b"")


def _initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "serverInfo": {"name": "fake-streamable", "version": "0"},
    }


def _result_for(req: dict[str, Any]) -> dict[str, Any]:
    method = str(req.get("method", ""))
    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": "echo",
                    "description": "echo the arguments back",
                    "inputSchema": {"type": "object"},
                }
            ]
        }
    if method == "tools/call":
        args = (req.get("params") or {}).get("arguments") or {}
        return {"content": [{"type": "text", "text": json.dumps(args, sort_keys=True)}]}
    return {}


def _envelope(req: dict[str, Any], result: dict[str, Any]) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": req.get("id"), "result": result}
    ).encode("utf-8")


class _FakeStreamableServer:
    """The loopback server plus the state the tests assert on."""

    def __init__(
        self,
        *,
        stateful: bool = True,
        expire_after: Optional[int] = None,
        delete_status: int = 200,
    ) -> None:
        self.state = _ServerState(
            stateful=stateful, expire_after=expire_after, delete_status=delete_status
        )
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.state = self.state  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/mcp"

    @property
    def assigned_id(self) -> str:
        return self.state.issued[-1]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _serve(**kw: Any) -> Iterator[_FakeStreamableServer]:
    server = _FakeStreamableServer(**kw)
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def stateful_server() -> Iterator[_FakeStreamableServer]:
    yield from _serve()


@pytest.fixture
def stateless_server() -> Iterator[_FakeStreamableServer]:
    yield from _serve(stateful=False)


def _spec(server: _FakeStreamableServer) -> McpHttpServerSpec:
    return McpHttpServerSpec(alias="remote", url=server.url)


def _ctx() -> ToolContext:
    return ToolContext(artifact_store=InMemoryContentStore())


# ---------------------------------------------------------------------------
# the assigned id is echoed on every later request
# ---------------------------------------------------------------------------


def test_assigned_id_is_echoed_on_every_later_request(
    stateful_server: _FakeStreamableServer,
) -> None:
    client = McpHttpClient(url=stateful_server.url)
    client.start()
    try:
        assert [t["name"] for t in client.list_tools()] == ["echo"]
        result = client.call_tool("echo", {"msg": "hi"})
        assert result["content"][0]["text"] == '{"msg": "hi"}'
    finally:
        client.shutdown()
    seen = stateful_server.state.requests
    assert [method for method, _ in seen] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    # The handshake carries no id (there is none yet); everything after it does.
    assert seen[0][1] is None
    assert [carried for _, carried in seen[1:]] == [stateful_server.assigned_id] * 3


def test_the_lifecycle_notification_is_sent_once_per_session(
    stateful_server: _FakeStreamableServer,
) -> None:
    """The fake refuses every request until the session has been announced,
    so a client that skips ``notifications/initialized`` cannot list tools.
    It goes out exactly once per session, carrying that session's id, and it
    is answered with ``202`` and an empty body — which must not be parsed."""
    client = McpHttpClient(url=stateful_server.url)
    client.start()
    try:
        for _ in range(3):
            assert [t["name"] for t in client.list_tools()] == ["echo"]
    finally:
        client.shutdown()
    assert stateful_server.state.notifications == [
        ("notifications/initialized", stateful_server.assigned_id)
    ]


def test_a_failed_lifecycle_notification_fails_the_connect() -> None:
    """A server that refuses the notification leaves a connection that can
    never list tools, so ``start`` fails the way a bad handshake does — the
    caller's existing skip / retire path takes it from there."""
    sent: list[str] = []

    def post(req: dict[str, Any], headers: Mapping[str, str]) -> McpHttpResponse:
        sent.append(str(req["method"]))
        if req["method"] == "initialize":
            return McpHttpResponse(
                body=_envelope(req, _initialize_result()),
                headers={_ID_HEADER: "refusing"},
            )
        return McpHttpResponse(body=b"", status=400)

    client = McpHttpClient(url="https://example.test/mcp", post=post)
    with pytest.raises(McpError):
        client.start()
    assert sent == ["initialize", "notifications/initialized"]


def test_an_injected_transport_may_carry_the_assigned_id() -> None:
    """A host that supplies its own transport joins a session by returning an
    ``McpHttpResponse`` instead of bytes — the richer shape is the only thing
    it has to opt into."""
    fake = FakeHttpMcpServer()

    def post(req: dict[str, Any], headers: Mapping[str, str]) -> McpHttpResponse:
        body = fake.post(req, headers)
        is_handshake = req["method"] == "initialize"
        assigned = {_ID_HEADER: "from-transport"} if is_handshake else {}
        return McpHttpResponse(body=body, headers=assigned)

    client = McpHttpClient(url="https://example.test/mcp", post=post)
    client.start()
    client.list_tools()
    client.shutdown()
    carried = [h.get(_ID_HEADER) for h in fake.seen_headers]
    assert carried == [None, "from-transport", "from-transport"]
    assert [c["method"] for c in fake.calls] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]


# ---------------------------------------------------------------------------
# teardown DELETEs the session, best-effort
# ---------------------------------------------------------------------------


def test_retiring_a_pooled_connection_deletes_the_session(
    stateful_server: _FakeStreamableServer,
) -> None:
    pool = McpConnectionPool(idle_ttl=None)
    client, _reused = pool.acquire(_spec(stateful_server))
    assert [t["name"] for t in client.list_tools()] == ["echo"]
    pool.retire(client)
    pool.release(client)  # last holder lets go → the connection closes
    assert pool.live_count() == 0
    assert stateful_server.state.deletes == [stateful_server.assigned_id]


def test_a_refused_delete_does_not_raise(
    request: pytest.FixtureRequest,
) -> None:
    server = _FakeStreamableServer(delete_status=405)
    request.addfinalizer(server.close)
    client = McpHttpClient(url=server.url)
    client.start()
    client.list_tools()
    client.shutdown()  # 405 Method Not Allowed → swallowed
    client.shutdown()  # idempotent: no second DELETE
    assert server.state.deletes == [server.assigned_id]


# ---------------------------------------------------------------------------
# a server that assigns no id is exactly as stateless as before
# ---------------------------------------------------------------------------


def test_a_stateless_server_sees_no_header_and_no_delete(
    stateless_server: _FakeStreamableServer,
) -> None:
    client = McpHttpClient(url=stateless_server.url)
    client.start()
    try:
        assert [t["name"] for t in client.list_tools()] == ["echo"]
        assert client.call_tool("echo", {})["content"][0]["text"] == "{}"
    finally:
        client.shutdown()
    assert all(carried is None for _, carried in stateless_server.state.requests)
    assert stateless_server.state.deletes == []
    # No id ⇒ no session to announce: the wire is what it was before sessions.
    assert stateless_server.state.notifications == []
    assert [method for method, _ in stateless_server.state.requests] == [
        "initialize",
        "tools/list",
        "tools/call",
    ]


def test_a_legacy_bytes_post_fn_still_works_end_to_end() -> None:
    """The pre-session transport shape — ``(req, headers) -> bytes`` — keeps
    building and calling tools; it simply runs stateless."""
    fake = FakeHttpMcpServer()
    spec = McpHttpServerSpec(alias="remote", url="https://example.test/mcp")
    tools, clients, skipped = build_mcp_tools((spec,), http_post=fake.post)
    try:
        assert skipped == []
        result = tools["mcp__remote__echo"].invoke({"msg": "hi"}, _ctx())
        assert result.success and '"msg": "hi"' in str(result.output)
    finally:
        for client in clients:
            client.shutdown()
    assert all(_ID_HEADER not in h for h in fake.seen_headers)


def test_a_bytes_returning_transport_is_accepted_verbatim() -> None:
    """Whatever the legacy shape returns is the body, unwrapped — a bytes
    return never grows headers or a status out of nowhere."""
    seen: list[bytes] = []

    def post(req: dict[str, Any], headers: Mapping[str, str]) -> bytes:
        is_handshake = req["method"] == "initialize"
        body = _envelope(req, _initialize_result() if is_handshake else _result_for(req))
        seen.append(body)
        return body

    client = McpHttpClient(url="https://example.test/mcp", post=post)
    client.start()
    assert [t["name"] for t in client.list_tools()] == ["echo"]
    client.shutdown()
    assert len(seen) == 2


# ---------------------------------------------------------------------------
# an expired session heals through the pool's retire-and-reconnect-once path
# ---------------------------------------------------------------------------


def test_an_expired_session_reconnects_through_the_pool(
    request: pytest.FixtureRequest,
) -> None:
    """The server drops the session after the first turn's ``tools/list``. The
    next build's ``tools/list`` 404s, which is the fault the pool already
    treats as a dead connection: retire, connect fresh once, handshake again
    for a new id — no new retry loop, and the turn keeps its tools."""
    server = _FakeStreamableServer(expire_after=1)
    request.addfinalizer(server.close)
    pool = McpConnectionPool(idle_ttl=None)
    spec = _spec(server)

    tools, clients, skipped = build_mcp_tools((spec,), pool=pool)
    assert "mcp__remote__echo" in tools and skipped == []
    pool.release_all(clients)

    tools, clients, skipped = build_mcp_tools((spec,), pool=pool)
    assert "mcp__remote__echo" in tools and skipped == []
    pool.release_all(clients)

    assert len(server.state.issued) == 2  # handshook again for a fresh id
    assert [method for method, _ in server.state.requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/list",  # 404: the session the server dropped
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    # Once per session, each carrying that session's own id.
    assert server.state.notifications == [
        ("notifications/initialized", server.state.issued[0]),
        ("notifications/initialized", server.state.issued[1]),
    ]
    # No DELETE chases a session the server already said is gone.
    assert server.state.deletes == []


def test_a_transport_that_reports_404_drops_the_session() -> None:
    """A transport that reports the status instead of raising still fails the
    call, and a 404 while an id is in play forgets it — so the retry the pool
    drives handshakes for a fresh one instead of replaying a dead id."""
    fake = FakeHttpMcpServer()
    sent: list[Optional[str]] = []

    def post(req: dict[str, Any], headers: Mapping[str, str]) -> McpHttpResponse:
        sent.append(headers.get(_ID_HEADER))
        if req["method"] == "initialize":
            return McpHttpResponse(
                body=fake.post(req, headers), headers={_ID_HEADER: "expiring"}
            )
        if "id" not in req:  # the lifecycle notification: 202, empty body
            return McpHttpResponse(body=b"", status=202)
        return McpHttpResponse(body=b'{"error":"Session not found"}', status=404)

    client = McpHttpClient(url="https://example.test/mcp", post=post)
    client.start()
    for _ in range(2):
        with pytest.raises(McpError):
            client.list_tools()
    client.shutdown()
    assert sent == [None, "expiring", "expiring", None]


def test_a_404_with_no_session_in_play_is_a_plain_fault(
    stateless_server: _FakeStreamableServer,
) -> None:
    """A stateless connection has no id to forget: a 404 is just a fault."""
    client = McpHttpClient(url=stateless_server.url.rsplit("/", 1)[0] + "/missing")
    with pytest.raises(McpError):
        client.start()
    client.shutdown()
    assert stateless_server.state.deletes == []
