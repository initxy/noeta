"""MCP transport id-matching and the connection pool's lifecycle.

A spec-compliant Streamable-HTTP server may echo the JSON-RPC ``id`` as a
string (``"1"``) even though the client sent an int, so both sides are
normalised to ``str`` before comparison — otherwise every response fails to
match and the call raises ``McpError``.

Live MCP connections live in the host's ``McpConnectionPool``, keyed by
server identity and shared by every per-turn Engine build that names the
server: acquire counts a holder, release uncounts, an idle holder-less
connection expires on the TTL, ``invalidate`` retires a connection without
pulling it from under a turn, ``shutdown`` closes everything, and the Engine
build releases its leases whether it succeeds (the Engine's finalizer) or
fails (the ``except``). The stdio client serializes exchanges so two turns on
one connection never interleave.
"""

from __future__ import annotations

import gc
import json
import sys
import threading
from pathlib import Path
from typing import Any, Mapping

import pytest

from noeta.client.host import SdkHost
from noeta.builtins.mcp.impl import McpHttpClient, McpStdioClient, build_mcp_tools
from noeta.builtins.mcp.impl import pool as pool_mod
from noeta.builtins.mcp.impl.pool import McpConnectionPool
from noeta.runtime.mcp import McpError, McpServerSpec


# ---------------------------------------------------------------------------
# SSE id matching tolerates a string-echoed JSON-RPC id
# ---------------------------------------------------------------------------


def _sse_post_with_string_id(req: dict[str, Any], headers: Mapping[str, str]) -> bytes:
    """Echo back an SSE one-shot body whose JSON-RPC id is the STRING form of
    the int the client sent (``1`` -> ``"1"``), per the Streamable-HTTP shape."""
    sid = str(req["id"])
    method = req["method"]
    if method == "initialize":
        result: dict[str, Any] = {"protocolVersion": "2024-11-05", "capabilities": {}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo"}]}
    else:  # tools/call
        result = {"content": [{"type": "text", "text": "ok"}]}
    obj = {"jsonrpc": "2.0", "id": sid, "result": result}
    return f"event: message\ndata: {json.dumps(obj)}\n\n".encode("utf-8")


def _client(post: Any) -> McpHttpClient:
    return McpHttpClient(url="https://example.test/mcp", post=post)


def test_sse_string_id_is_matched() -> None:
    # A server that echoes the int id as the string "1" must still handshake:
    # "1" == 1 is False, so the ids are compared as strings.
    c = _client(_sse_post_with_string_id)
    c.start()
    assert [t["name"] for t in c.list_tools()] == ["echo"]
    assert c.call_tool("echo", {})["content"][0]["text"] == "ok"


def test_extract_response_matches_string_id() -> None:
    c = _client(_sse_post_with_string_id)
    body = b'event: message\ndata: {"jsonrpc":"2.0","id":"7","result":{"ok":true}}\n\n'
    # req_id is the int 7; the server echoed "7" — must still match.
    msg = c._extract_response("tools/call", body, 7)
    assert msg["result"] == {"ok": True}


def test_extract_response_matches_int_id() -> None:
    # An int id echoed as an int must match too.
    c = _client(_sse_post_with_string_id)
    body = b'data: {"jsonrpc":"2.0","id":3,"result":{"ok":true}}\n\n'
    msg = c._extract_response("tools/call", body, 3)
    assert msg["result"] == {"ok": True}


def test_extract_response_no_match_still_raises() -> None:
    c = _client(_sse_post_with_string_id)
    body = b'data: {"jsonrpc":"2.0","id":"99","result":{}}\n\n'
    with pytest.raises(McpError):
        c._extract_response("tools/call", body, 1)


# ---------------------------------------------------------------------------
# the connection pool
# ---------------------------------------------------------------------------


class _FakeClient:
    """Stand-in for a stdio/HTTP client: records start/shutdown, lists one
    tool, and can be told to fail its next ``tools/list`` (a server that died
    between turns)."""

    def __init__(self, spec: Any) -> None:
        self.spec = spec
        self.started = 0
        self.shutdowns = 0
        self.fail_list = False

    def start(self) -> None:
        self.started += 1

    def list_tools(self) -> list[dict[str, Any]]:
        if self.fail_list:
            raise McpError("server closed stdout (process exited?)")
        return [{"name": "echo", "inputSchema": {"type": "object"}}]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "ok"}]}

    def shutdown(self) -> None:
        self.shutdowns += 1


def _spec(alias: str = "fake", env: tuple[tuple[str, str], ...] = ()) -> McpServerSpec:
    return McpServerSpec(alias=alias, argv=("srv",), env=env)


@pytest.fixture
def fake_connect(monkeypatch: pytest.MonkeyPatch) -> list[_FakeClient]:
    """Route the pool's connects to ``_FakeClient``; returns every client made."""
    made: list[_FakeClient] = []

    def connect(spec: Any, *, spawn: Any, http_post: Any) -> _FakeClient:
        client = _FakeClient(spec)
        made.append(client)
        return client

    monkeypatch.setattr(pool_mod, "_connect_client", connect)
    return made


def test_pool_shares_one_connection_per_server_identity(
    fake_connect: list[_FakeClient],
) -> None:
    pool = McpConnectionPool(idle_ttl=None)
    a, reused_a = pool.acquire(_spec())
    b, reused_b = pool.acquire(_spec())
    assert a is b and not reused_a and reused_b
    assert pool.holders_of(a) == 2 and a.started == 1
    # A different credential env is a different connection.
    c, _ = pool.acquire(_spec(env=(("TOKEN", "t2"),)))
    assert c is not a
    assert len(fake_connect) == 2
    pool.release(a)
    pool.release(a)
    # Holder-less but no TTL: it stays open for the next build.
    assert pool.holders_of(a) == 0 and a.shutdowns == 0
    assert pool.acquire(_spec())[0] is a


def test_pool_partitions_by_scope(fake_connect: list[_FakeClient]) -> None:
    """The host's scope (a tenant, a workspace) is part of the identity: the
    same spec in two scopes is two connections, ``None`` is the shared one,
    and ``invalidate`` by alias retires the alias in every scope."""
    pool = McpConnectionPool(idle_ttl=None)
    shared, _ = pool.acquire(_spec())
    a, reused_a = pool.acquire(_spec(), "tenant-a")
    a_again, reused_again = pool.acquire(_spec(), "tenant-a")
    b, _ = pool.acquire(_spec(), "tenant-b")
    assert a is a_again and reused_again and not reused_a
    assert len({id(shared), id(a), id(b)}) == 3 and pool.live_count() == 3
    for c in (shared, a, a_again, b):
        pool.release(c)
    pool.invalidate("fake")
    assert shared.shutdowns == a.shutdowns == b.shutdowns == 1
    assert pool.live_count() == 0


def test_pool_expires_an_idle_connection(fake_connect: list[_FakeClient]) -> None:
    now = [0.0]
    pool = McpConnectionPool(idle_ttl=10.0, clock=lambda: now[0])
    a, _ = pool.acquire(_spec("a"))
    pool.release(a)
    now[0] += 5.0
    assert pool.acquire(_spec("b"))[0] is not a  # a different server
    assert a.shutdowns == 0  # not idle long enough
    now[0] += 6.0
    pool.acquire(_spec("c"))  # any pool operation sweeps
    assert a.shutdowns == 1
    # A held connection never expires, however long it idles.
    b, _ = pool.acquire(_spec("b"))
    now[0] += 1000.0
    pool.acquire(_spec("d"))
    assert b.shutdowns == 0


def test_invalidate_keeps_a_held_connection_until_release(
    fake_connect: list[_FakeClient],
) -> None:
    pool = McpConnectionPool(idle_ttl=None)
    held, _ = pool.acquire(_spec("fake"))
    idle, _ = pool.acquire(_spec("other"))
    pool.release(idle)
    pool.invalidate()
    # The idle one closed at once; the held one is still usable.
    assert idle.shutdowns == 1 and held.shutdowns == 0
    assert held.call_tool("echo", {})["content"][0]["text"] == "ok"
    # The next build connects afresh rather than reusing the retired one.
    fresh, reused = pool.acquire(_spec("fake"))
    assert fresh is not held and not reused
    pool.release(held)
    assert held.shutdowns == 1
    assert pool.live_count() == 1  # only ``fresh``


def test_invalidate_by_alias_leaves_other_servers_alone(
    fake_connect: list[_FakeClient],
) -> None:
    pool = McpConnectionPool(idle_ttl=None)
    a, _ = pool.acquire(_spec("a"))
    b, _ = pool.acquire(_spec("b"))
    pool.release(a)
    pool.release(b)
    pool.invalidate("a")
    assert a.shutdowns == 1 and b.shutdowns == 0
    assert pool.acquire(_spec("b"))[0] is b


def test_shutdown_closes_everything_including_held(
    fake_connect: list[_FakeClient],
) -> None:
    pool = McpConnectionPool(idle_ttl=None)
    a, _ = pool.acquire(_spec("a"))
    b, _ = pool.acquire(_spec("b"))
    pool.release(b)
    pool.shutdown()
    assert a.shutdowns == 1 and b.shutdowns == 1
    assert pool.live_count() == 0
    pool.release(a)  # a late finalizer is harmless
    assert a.shutdowns == 1


def test_shutdown_swallows_one_bad_client(fake_connect: list[_FakeClient]) -> None:
    pool = McpConnectionPool(idle_ttl=None)
    bad, _ = pool.acquire(_spec("bad"))
    good, _ = pool.acquire(_spec("good"))

    def boom() -> None:
        bad.shutdowns += 1
        raise RuntimeError("boom")

    bad.shutdown = boom  # type: ignore[method-assign]
    pool.shutdown()
    assert bad.shutdowns == 1 and good.shutdowns == 1


def test_build_reconnects_a_stale_pooled_connection_once(
    fake_connect: list[_FakeClient],
) -> None:
    """A reused connection whose ``tools/list`` fails (the server died between
    turns) is retired and the server connected fresh; a fresh connection that
    fails is skipped for the turn."""
    pool = McpConnectionPool(idle_ttl=None)
    tools, clients, skipped = build_mcp_tools((_spec(),), pool=pool, skip_on_failure=True)
    assert set(tools) == {"mcp__fake__echo"} and not skipped
    (first,) = clients
    pool.release(first)
    first.fail_list = True
    tools, clients, skipped = build_mcp_tools((_spec(),), pool=pool, skip_on_failure=True)
    assert set(tools) == {"mcp__fake__echo"} and not skipped
    (second,) = clients
    assert second is not first and first.shutdowns == 1
    assert len(fake_connect) == 2
    # A fresh connection that cannot list is a skip, released and retired.
    pool.release(second)
    pool.invalidate()
    fake_connect.clear()
    fail_next = {"armed": True}
    original_start = _FakeClient.start

    def start_failing(self: _FakeClient) -> None:
        original_start(self)
        if fail_next.pop("armed", False):
            self.fail_list = True

    _FakeClient.start = start_failing  # type: ignore[method-assign]
    try:
        tools, clients, skipped = build_mcp_tools(
            (_spec(),), pool=pool, skip_on_failure=True
        )
    finally:
        _FakeClient.start = original_start  # type: ignore[method-assign]
    assert not tools and not clients
    assert [s.alias for s in skipped] == ["fake"]
    assert fake_connect[0].shutdowns == 1
    assert pool.live_count() == 0


def test_build_keeps_a_pooled_connection_on_a_config_error(
    fake_connect: list[_FakeClient],
) -> None:
    """An unmappable tool name (empty) is a configuration fault, not the
    connection's: the pooled client is released intact — still live, not
    retired — so the next build after the fix reuses it instead of
    reconnecting."""
    from noeta.runtime.mcp import McpConfigError

    pool = McpConnectionPool(idle_ttl=None)
    spec = _spec("fake")
    client, _ = pool.acquire(spec)
    pool.release(client)
    client.list_tools = lambda: [  # type: ignore[method-assign]
        {"name": "", "inputSchema": {"type": "object"}},
    ]
    with pytest.raises(McpConfigError):
        build_mcp_tools((spec,), pool=pool)
    assert pool.holders_of(client) == 0 and client.shutdowns == 0
    assert pool.live_count() == 1
    assert pool.acquire(spec)[0] is client


def test_build_fail_fast_releases_every_acquired_connection(
    fake_connect: list[_FakeClient],
) -> None:
    pool = McpConnectionPool(idle_ttl=None)
    collision = McpServerSpec(alias="fake", argv=("srv",))
    with pytest.raises(Exception):
        # A duplicate alias is a hard configuration error after the first
        # server connected — its connection must be released, not leaked.
        build_mcp_tools((_spec("fake"), collision), pool=pool)
    (only,) = fake_connect
    assert pool.holders_of(only) == 0


# ---------------------------------------------------------------------------
# the Engine build's leases: released on failure, released with the Engine
# ---------------------------------------------------------------------------


class _LeaseHost:
    """The two host methods under test over a real pool."""

    _build_engine = SdkHost._build_engine

    def __init__(self, pool: McpConnectionPool, *, fail: bool) -> None:
        self.pool = pool
        self.fail = fail

    def _mcp_pool_get(self) -> McpConnectionPool:
        return self.pool

    def _assemble_engine(self, *_a: Any, mcp_leases: list[Any], **_kw: Any) -> Any:
        client, _ = self.pool.acquire(_spec())
        mcp_leases.append(client)
        if self.fail:
            raise RuntimeError("engine assembly failed after the MCP connect")
        return _Engine()


class _Engine:
    """A weak-referenceable stand-in for the built Engine."""


def test_failed_engine_build_releases_its_leases(
    fake_connect: list[_FakeClient],
) -> None:
    pool = McpConnectionPool(idle_ttl=None)
    host = _LeaseHost(pool, fail=True)
    with pytest.raises(RuntimeError):
        host._build_engine("spec", "model")
    (client,) = fake_connect
    assert pool.holders_of(client) == 0
    # The connection itself stays pooled for the next build.
    assert client.shutdowns == 0 and pool.live_count() == 1


def test_engine_releases_its_leases_when_it_goes(
    fake_connect: list[_FakeClient],
) -> None:
    pool = McpConnectionPool(idle_ttl=None)
    host = _LeaseHost(pool, fail=False)
    engine = host._build_engine("spec", "model")
    (client,) = fake_connect
    assert pool.holders_of(client) == 1
    pool.invalidate()  # retired but held: still open
    assert client.shutdowns == 0
    del engine
    gc.collect()
    assert pool.holders_of(client) == 0 and client.shutdowns == 1


# ---------------------------------------------------------------------------
# a shared stdio connection serializes concurrent exchanges
# ---------------------------------------------------------------------------

_FAKE = str(Path(__file__).parent / "_fixtures" / "fake_mcp_server.py")


def test_stdio_client_serializes_concurrent_calls() -> None:
    """Two threads calling one stdio client each get their own reply — the
    exchange lock keeps a shared pooled connection from interleaving lines."""
    client = McpStdioClient(argv=[sys.executable, "-u", _FAKE, "echo"])
    client.start()
    mismatches: list[str] = []

    def worker(tag: str) -> None:
        for i in range(20):
            msg = f"{tag}-{i}"
            result = client.call_tool("echo", {"msg": msg})
            text = result["content"][0]["text"]
            if json.loads(text) != {"msg": msg}:
                mismatches.append(f"{msg} got {text}")

    try:
        threads = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b")]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
    finally:
        client.shutdown()
    assert not mismatches
