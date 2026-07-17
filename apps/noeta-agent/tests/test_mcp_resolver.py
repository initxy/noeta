"""MCP resolver wiring: per-space connector → per-turn spec resolution.

Three layers:

* the store's ``resolve_spec`` builds the SDK connect spec (WITH credentials
  and tool subset) for enabled connectors only;
* the AgentService's token seam — ``_mcp_tokens_for_session`` computes the
  turn's ``<space_id>:<alias>`` token set from the session's space, and
  ``_resolve_mcp_spec`` (the callback wired as
  ``HostConfig.mcp_server_resolver``) maps a token back to the spec;
* end-to-end: a turn in a space with an enabled connector actually connects
  it (the engine handshakes the stub server at task start), while a space
  without connectors passes an empty set and never touches the network.
"""
from __future__ import annotations

from pathlib import Path

from noeta.sdk import McpHttpServerSpec, McpServerSpec

from noeta.agent.config import Settings
from noeta.agent.host.service import AgentService
from noeta.agent.store.mcp import McpConnectorStore
from noeta.agent.store.sessions import SessionStore

from tests.conftest import create_session, login, wait_status
from tests.test_mcp_api import StubMcpServer, stub_mcp  # noqa: F401 - fixture


# ----------------------------------------------------------- store resolve


def _store(tmp_path: Path) -> McpConnectorStore:
    return McpConnectorStore(tmp_path / "app.db")


def test_resolve_spec_builds_http_spec_with_credentials(tmp_path):
    store = _store(tmp_path)
    store.upsert(
        "space1",
        "remote",
        connector_type="http",
        url="https://mcp.example.com/mcp",
        headers={"Authorization": "Bearer secret"},
        tools=["echo"],
        created_by="alice",
    )
    spec = store.resolve_spec("space1", "remote")
    assert isinstance(spec, McpHttpServerSpec)
    assert spec.alias == "remote"  # the clean alias, not the scoped token
    assert spec.url == "https://mcp.example.com/mcp"
    assert spec.headers_dict() == {"Authorization": "Bearer secret"}
    assert spec.tool_subset == ("echo",)
    store.close()


def test_resolve_spec_builds_stdio_spec(tmp_path):
    store = _store(tmp_path)
    store.upsert(
        "space1",
        "local",
        connector_type="stdio",
        command="mytool",
        args=["--x"],
        env={"API_TOKEN": "env-secret"},
        created_by="alice",
    )
    spec = store.resolve_spec("space1", "local")
    assert isinstance(spec, McpServerSpec)
    assert spec.argv == ("mytool", "--x")
    assert spec.env_dict() == {"API_TOKEN": "env-secret"}
    store.close()


def test_resolve_spec_skips_disabled_unknown_and_other_space(tmp_path):
    store = _store(tmp_path)
    store.upsert(
        "space1",
        "remote",
        connector_type="http",
        url="https://x/mcp",
        created_by="alice",
    )
    store.set_enabled("space1", "remote", False)
    assert store.resolve_spec("space1", "remote") is None  # disabled
    assert store.resolve_spec("space1", "ghost") is None  # unconfigured
    # Space scoping: the same alias in another space does not resolve.
    assert store.resolve_spec("space2", "remote") is None
    store.close()


def test_same_alias_in_two_spaces_stays_isolated(tmp_path):
    store = _store(tmp_path)
    store.upsert(
        "space1", "github", connector_type="http",
        url="https://one.example/mcp", created_by="alice",
    )
    store.upsert(
        "space2", "github", connector_type="http",
        url="https://two.example/mcp", created_by="bob",
    )
    assert store.resolve_spec("space1", "github").url == "https://one.example/mcp"
    assert store.resolve_spec("space2", "github").url == "https://two.example/mcp"
    store.close()


# ------------------------------------------------- service token/resolver


def _service(tmp_path, monkeypatch) -> tuple[AgentService, SessionStore, McpConnectorStore]:
    """A bare AgentService (no client startup) with real stores — enough for
    the token/resolver seam, which only reads sqlite."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings = Settings()
    session_store = SessionStore(tmp_path / "app.db")
    mcp_store = McpConnectorStore(tmp_path / "app.db")
    service = AgentService(settings, session_store)
    service.attach_mcp_store(mcp_store)
    return service, session_store, mcp_store


def test_tokens_cover_enabled_connectors_of_the_session_space(
    tmp_path, monkeypatch
):
    service, sessions, mcp = _service(tmp_path, monkeypatch)
    session = sessions.create("alice", "mock-model", "space1")
    mcp.upsert(
        "space1", "beta", connector_type="http", url="https://b/mcp",
        created_by="alice",
    )
    mcp.upsert(
        "space1", "alpha", connector_type="http", url="https://a/mcp",
        created_by="alice",
    )
    mcp.upsert(
        "space1", "off", connector_type="http", url="https://off/mcp",
        enabled=False, created_by="alice",
    )
    # Another space's connector never enters this session's set.
    mcp.upsert(
        "space2", "other", connector_type="http", url="https://o/mcp",
        created_by="bob",
    )

    tokens = service._mcp_tokens_for_session(session.id)
    assert tokens == ("space1:alpha", "space1:beta")

    # The resolver maps a token back to the space's spec (clean alias).
    spec = service._resolve_mcp_spec("space1:alpha")
    assert isinstance(spec, McpHttpServerSpec)
    assert spec.alias == "alpha" and spec.url == "https://a/mcp"
    # Disabled / unknown / malformed tokens are skipped.
    assert service._resolve_mcp_spec("space1:off") is None
    assert service._resolve_mcp_spec("space1:ghost") is None
    assert service._resolve_mcp_spec("no-separator") is None


def test_session_without_connectors_gets_empty_token_set(tmp_path, monkeypatch):
    service, sessions, _mcp = _service(tmp_path, monkeypatch)
    session = sessions.create("alice", "mock-model", "space1")
    assert service._mcp_tokens_for_session(session.id) == ()
    # No store attached at all (mcp management not wired) degrades the same.
    bare = AgentService(Settings(), sessions)
    assert bare._mcp_tokens_for_session(session.id) == ()
    assert bare._resolve_mcp_spec("space1:alpha") is None


# ----------------------------------------------------------------- e2e


def test_enabled_connector_is_connected_on_turn(client, stub_mcp):  # noqa: F811
    """A session in a space with an enabled connector: the turn's engine
    build resolves the token → spec and handshakes the live server."""
    stub = stub_mcp()
    login(client, "alice")
    r = client.get("/api/v1/spaces")
    sid = next(s["id"] for s in r.json()["spaces"] if s["is_personal"])
    r = client.post(
        f"/api/v1/spaces/{sid}/mcp/servers",
        json={
            "alias": "remote",
            "type": "http",
            "url": stub.url,
            "headers": {"Authorization": "Bearer turn-secret"},
        },
    )
    assert r.status_code == 201, r.text

    session_id = create_session(client, sid)
    r = client.post(
        f"/api/v1/sessions/{session_id}/messages", json={"content": "hello"}
    )
    assert r.status_code == 202, r.text
    wait_status(client, session_id, {"idle", "waiting"})

    # The engine connected the connector at task start: handshake +
    # tools/list, with the credential header on the wire.
    assert "initialize" in stub.requests
    assert "tools/list" in stub.requests
    assert any(
        h.get("Authorization") == "Bearer turn-secret"
        for h in stub.seen_headers
    )


def test_disabled_connector_is_not_connected_on_turn(client, stub_mcp):  # noqa: F811
    stub = stub_mcp()
    login(client, "alice")
    r = client.get("/api/v1/spaces")
    sid = next(s["id"] for s in r.json()["spaces"] if s["is_personal"])
    client.post(
        f"/api/v1/spaces/{sid}/mcp/servers",
        json={"alias": "remote", "type": "http", "url": stub.url,
              "enabled": False},
    )

    session_id = create_session(client, sid)
    r = client.post(
        f"/api/v1/sessions/{session_id}/messages", json={"content": "hello"}
    )
    assert r.status_code == 202, r.text
    wait_status(client, session_id, {"idle", "waiting"})
    assert stub.requests == []  # never touched
