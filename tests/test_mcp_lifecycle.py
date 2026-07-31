"""MCP transport id-matching and the cache's ownership of live MCP clients.

A spec-compliant Streamable-HTTP server may echo the JSON-RPC ``id`` as a
string (``"1"``) even though the client sent an int, so both sides are
normalised to ``str`` before comparison — otherwise every response fails to
match and the call raises ``McpError``.

Live MCP clients are owned by their cached Engine, and every path that drops an
Engine must shut those clients down or it orphans the spawned MCP subprocess and
leaks its fds: LRU eviction, overwrite, delete and clear all reap, reaping is
idempotent and swallows one client's failure so it cannot break the others, and
a build that connects MCP then fails discards its staged clients so the connect
is failure-atomic.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from noeta.client.host import _MAX_CACHED_ENGINES, _McpReapingEngineCache, SdkHost
from noeta.builtins.mcp.impl import McpHttpClient
from noeta.runtime.mcp import McpError


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
# engine cache reaps evicted entries' live MCP clients
# ---------------------------------------------------------------------------


class _FakeClient:
    """Stand-in for an McpStdioClient/McpHttpClient: records shutdown calls,
    shutdown is idempotent (mirrors the real clients)."""

    def __init__(self) -> None:
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1


class _BoomClient:
    """A client whose shutdown raises — eviction must not break."""

    def __init__(self) -> None:
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1
        raise RuntimeError("boom")


def test_eviction_shuts_down_clients() -> None:
    cache = _McpReapingEngineCache()
    client = _FakeClient()
    # Mirror the host flow: stage clients, then the resolver puts the engine.
    cache.stage([client])
    cache["k1"] = "engine-1"
    # A second put with no staged clients -> no clients adopted for k2.
    cache["k2"] = "engine-2"
    # Evict the oldest (k1) — its client must be shut down exactly once.
    cache.popitem(last=False)
    assert client.shutdowns == 1
    assert "k1" not in cache and "k2" in cache


def test_eviction_swallows_shutdown_errors() -> None:
    cache = _McpReapingEngineCache()
    boom = _BoomClient()
    good = _FakeClient()
    cache.stage([boom, good])
    cache["k1"] = "engine-1"
    # Eviction must not raise even though boom.shutdown() throws; the good
    # client must still be reaped (one bad client can't break eviction).
    cache.popitem(last=False)
    assert boom.shutdowns == 1
    assert good.shutdowns == 1


def test_no_clients_staged_is_safe() -> None:
    cache = _McpReapingEngineCache()
    cache["k1"] = "engine-1"  # no stage() — engine without MCP
    cache.popitem(last=False)  # must not raise
    assert len(cache) == 0


def test_overwrite_reaps_prior_clients() -> None:
    cache = _McpReapingEngineCache()
    first = _FakeClient()
    second = _FakeClient()
    cache.stage([first])
    cache["k1"] = "engine-1"
    # Rebuild under the same key with fresh clients -> the prior client is reaped.
    cache.stage([second])
    cache["k1"] = "engine-1b"
    assert first.shutdowns == 1
    assert second.shutdowns == 0


def test_del_and_clear_reap() -> None:
    cache = _McpReapingEngineCache()
    a, b = _FakeClient(), _FakeClient()
    cache.stage([a])
    cache["k1"] = "e1"
    cache.stage([b])
    cache["k2"] = "e2"
    del cache["k1"]
    assert a.shutdowns == 1
    cache.clear()
    assert b.shutdowns == 1
    assert len(cache) == 0


def test_lru_cap_constant_sane() -> None:
    # Guard the eviction-cap constant the host advertises stays positive.
    assert _MAX_CACHED_ENGINES > 0


# ---------------------------------------------------------------------------
# A build that connects MCP and then fails must not orphan the clients.
#
# ``_resolve_live_mcp_tools`` connects partway through ``_assemble_engine``;
# every later step (session inputs, provider lookup, policy factory) can raise,
# and the base resolver then never puts — so nothing adopts the staged clients.
# The build must reap them itself, or the subprocess + fds leak for the life of
# the process.
# ---------------------------------------------------------------------------


def test_discard_staged_reaps_unadopted_clients() -> None:
    cache = _McpReapingEngineCache()
    orphan = _FakeClient()
    cache.stage([orphan])
    # The build raised before any ``cache[key] = engine`` put.
    cache.discard_staged()
    assert orphan.shutdowns == 1
    # Discarding is one-shot: a later put must not adopt the dead clients.
    cache["k1"] = "engine-1"
    cache.popitem(last=False)
    assert orphan.shutdowns == 1


def test_discard_staged_without_staging_is_safe() -> None:
    cache = _McpReapingEngineCache()
    cache.discard_staged()  # must not raise
    cache.discard_staged()


def test_failed_engine_build_reaps_connected_mcp_clients() -> None:
    """End-to-end at the host seam: ``_assemble_engine`` raising after the MCP
    connect leaves no live client behind."""

    class _Host:
        """The two host methods under test, over a real reaping cache."""

        _engines = _McpReapingEngineCache()
        _stage_mcp_clients = SdkHost._stage_mcp_clients
        _discard_staged_mcp_clients = SdkHost._discard_staged_mcp_clients
        _build_engine = SdkHost._build_engine

        def _assemble_engine(self, *_a: Any, **_kw: Any) -> Any:
            # Stand in for ``_resolve_live_mcp_tools``: connect + stage...
            self._stage_mcp_clients([connected])
            # ...then fail the way a bad provider / policy factory would.
            raise RuntimeError("engine assembly failed after the MCP connect")

    connected = _FakeClient()
    host = _Host()
    with pytest.raises(RuntimeError):
        host._build_engine("spec", "model")
    assert connected.shutdowns == 1
