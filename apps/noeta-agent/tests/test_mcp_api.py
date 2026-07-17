"""Per-space MCP connector API: CRUD + credential scrubbing + membership
authorization + discovery menus / error mapping.

Ports the retired app's management-surface behaviors onto the space-scoped
API (the old ``/mcp/servers`` global registry surface, re-scoped per D9
item 1):

* credentials (header/env VALUES) are stored server-side and NEVER echoed on
  any read path — every response carries name lists only;
* POST = create/replace, PUT = merge edit (omitted fields keep the stored
  credentials), PUT tools sets/clears the subset;
* discovery menus connect to the live connector; a connect/handshake failure
  maps to 502, a bad config to 400, an unknown alias to 404.

New in the re-scope: connectors carry an ``enabled`` flag (PATCH toggles it)
and the whole surface sits under space membership auth (member = read,
owner = manage, non-member = 404).
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.conftest import login


# ---------------------------------------------------------------- helpers


def _new_team(client, name: str = "mcp-team") -> str:
    r = client.post("/api/v1/spaces", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["space"]["id"]


def _servers_url(sid: str) -> str:
    return f"/api/v1/spaces/{sid}/mcp/servers"


def _create_http(client, sid: str, alias: str = "remote", **overrides) -> dict:
    body = {
        "alias": alias,
        "type": "http",
        "url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": "Bearer secret-token"},
    }
    body.update(overrides)
    r = client.post(_servers_url(sid), json=body)
    assert r.status_code == 201, r.text
    return r.json()["server"]


class StubMcpServer:
    """A local HTTP MCP stub speaking the request-response JSON-RPC subset
    (``initialize`` / ``tools/list`` / ``prompts/list`` / ``resources/list``).

    Records the method of every JSON-RPC request and the headers it rode
    with, so tests can prove the credential header was injected on the wire
    (and never anywhere else). ``mode="fail"`` answers every POST with a 500
    (a handshake fault the API maps to 502)."""

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.requests: list[str] = []
        self.seen_headers: list[dict[str, str]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # noqa: ARG002 - quiet
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                stub.requests.append(str(payload.get("method", "")))
                stub.seen_headers.append(dict(self.headers.items()))
                if stub.mode == "fail":
                    self.send_response(500)
                    self.end_headers()
                    return
                result = stub._result_for(payload.get("method", ""))
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}/mcp"
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )
        self._thread.start()

    def _result_for(self, method: str) -> dict:
        if method == "initialize":
            return {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "stub", "version": "0"},
            }
        if method == "tools/list":
            return {
                "tools": [
                    {"name": "echo", "description": "echo the arguments back",
                     "inputSchema": {"type": "object"}},
                    {"name": "add", "description": "add two numbers",
                     "inputSchema": {"type": "object"}},
                ]
            }
        if method == "prompts/list":
            return {
                "prompts": [
                    {
                        "name": "summarize",
                        "description": "Summarize a topic",
                        "arguments": [
                            {"name": "topic", "required": True},
                        ],
                    }
                ]
            }
        if method == "resources/list":
            return {
                "resources": [
                    {
                        "uri": "mem://notes/readme",
                        "name": "readme",
                        "description": "project readme",
                        "mimeType": "text/plain",
                    }
                ]
            }
        return {}

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def stub_mcp():
    servers: list[StubMcpServer] = []

    def _make(mode: str = "ok") -> StubMcpServer:
        server = StubMcpServer(mode=mode)
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.stop()


def _closed_port_url() -> str:
    """A URL nothing listens on (bind, read the port, close)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}/mcp"


# -------------------------------------------------- CRUD + credential scrub


def test_crud_lifecycle_and_credential_scrub(client):
    login(client, "alice")
    sid = _new_team(client)

    # Empty list.
    r = client.get(_servers_url(sid))
    assert r.status_code == 200 and r.json()["servers"] == []

    # Create an http connector with a credential header: the value is
    # scrubbed to the header NAME on the way back.
    public = _create_http(client, sid)
    assert public["alias"] == "remote"
    assert public["header_names"] == ["Authorization"]
    assert public["enabled"] is True
    assert public["tools"] is None
    assert "secret-token" not in json.dumps(public)

    # Create a stdio connector with an env secret: same scrub rule.
    r = client.post(
        _servers_url(sid),
        json={
            "alias": "local",
            "type": "stdio",
            "command": "mytool",
            "args": ["--x"],
            "env": {"API_TOKEN": "env-secret"},
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["server"]["env_names"] == ["API_TOKEN"]
    assert "env-secret" not in r.text

    # List shows both, alias-sorted, still scrubbed.
    r = client.get(_servers_url(sid))
    assert [s["alias"] for s in r.json()["servers"]] == ["local", "remote"]
    assert "secret-token" not in r.text and "env-secret" not in r.text

    # Merge edit: set a tool subset; the url + stored credential survive
    # untouched (the credential need not be re-pasted).
    r = client.put(
        f"{_servers_url(sid)}/remote", json={"tools": ["echo", "add"]}
    )
    assert r.status_code == 200, r.text
    merged = r.json()["server"]
    assert merged["tools"] == ["echo", "add"]
    assert merged["url"] == "https://mcp.example.com/mcp"
    assert merged["header_names"] == ["Authorization"]
    assert "secret-token" not in r.text

    # The tools sub-resource clears the subset (null = all advertised tools).
    r = client.put(f"{_servers_url(sid)}/remote/tools", json={"tools": None})
    assert r.status_code == 200 and r.json()["server"]["tools"] is None

    # Enable toggle.
    r = client.patch(f"{_servers_url(sid)}/remote", json={"enabled": False})
    assert r.status_code == 200 and r.json()["server"]["enabled"] is False

    # Delete + idempotent 404.
    assert client.delete(f"{_servers_url(sid)}/remote").status_code == 200
    assert client.delete(f"{_servers_url(sid)}/remote").status_code == 404


def test_post_replaces_existing_alias(client):
    login(client, "alice")
    sid = _new_team(client)
    _create_http(client, sid, tools=["echo"])

    # Re-POST the same alias: replace (new url, subset cleared).
    r = client.post(
        _servers_url(sid),
        json={"alias": "remote", "type": "http", "url": "https://new.example/mcp"},
    )
    assert r.status_code == 201, r.text
    replaced = r.json()["server"]
    assert replaced["url"] == "https://new.example/mcp"
    assert replaced["tools"] is None
    assert replaced["header_names"] == []


def test_create_validation(client):
    login(client, "alice")
    sid = _new_team(client)

    # Unknown transport type.
    r = client.post(
        _servers_url(sid), json={"alias": "x", "type": "ws", "url": "https://x"}
    )
    assert r.status_code == 400
    # http without url / stdio without command.
    assert (
        client.post(_servers_url(sid), json={"alias": "x", "type": "http"})
        .status_code
        == 400
    )
    assert (
        client.post(_servers_url(sid), json={"alias": "x", "type": "stdio"})
        .status_code
        == 400
    )
    # A bad alias fails the SDK alias rule (^[a-z0-9_-]{1,32}$).
    r = client.post(
        _servers_url(sid),
        json={"alias": "Bad Alias", "type": "http", "url": "https://x"},
    )
    assert r.status_code == 400
    # A tool subset with an empty entry is rejected.
    r = client.post(
        _servers_url(sid),
        json={"alias": "ok", "type": "http", "url": "https://x", "tools": [""]},
    )
    assert r.status_code == 400


def test_edit_unknown_alias_404(client):
    login(client, "alice")
    sid = _new_team(client)
    assert (
        client.put(f"{_servers_url(sid)}/ghost", json={"url": "https://x"})
        .status_code
        == 404
    )
    assert (
        client.patch(f"{_servers_url(sid)}/ghost", json={"enabled": False})
        .status_code
        == 404
    )
    assert (
        client.put(f"{_servers_url(sid)}/ghost/tools", json={"tools": ["a"]})
        .status_code
        == 404
    )


# ------------------------------------------------- membership authorization


def test_member_reads_owner_manages_non_member_404(client):
    login(client, "alice")
    sid = _new_team(client)
    _create_http(client, sid)
    client.post(f"/api/v1/spaces/{sid}/members", json={"username": "bob"})

    # A member reads the (scrubbed) list but cannot manage.
    login(client, "bob")
    r = client.get(_servers_url(sid))
    assert r.status_code == 200
    assert "secret-token" not in r.text
    assert (
        client.post(
            _servers_url(sid),
            json={"alias": "mine", "type": "http", "url": "https://x"},
        ).status_code
        == 403
    )
    assert (
        client.put(f"{_servers_url(sid)}/remote", json={"url": "https://x"})
        .status_code
        == 403
    )
    assert (
        client.patch(f"{_servers_url(sid)}/remote", json={"enabled": False})
        .status_code
        == 403
    )
    assert (
        client.put(f"{_servers_url(sid)}/remote/tools", json={"tools": ["a"]})
        .status_code
        == 403
    )
    assert client.delete(f"{_servers_url(sid)}/remote").status_code == 403

    # A non-member sees nothing (404 hides existence) on read AND manage.
    login(client, "mallory")
    assert client.get(_servers_url(sid)).status_code == 404
    assert (
        client.post(
            _servers_url(sid),
            json={"alias": "mine", "type": "http", "url": "https://x"},
        ).status_code
        == 404
    )
    assert client.delete(f"{_servers_url(sid)}/remote").status_code == 404


def test_personal_space_owner_manages(client):
    """A personal space's owner is its owner-role member: full manage."""
    login(client, "alice")
    r = client.get("/api/v1/spaces")
    sid = next(s["id"] for s in r.json()["spaces"] if s["is_personal"])
    public = _create_http(client, sid, alias="personal")
    assert public["alias"] == "personal"
    assert client.delete(f"{_servers_url(sid)}/personal").status_code == 200


# ------------------------------------------------------- discovery menus


def test_discovery_menus_over_stub(client, stub_mcp):
    stub = stub_mcp()
    login(client, "alice")
    sid = _new_team(client)
    _create_http(client, sid, url=stub.url)

    # Tool menu: full advertised set, name-sorted.
    r = client.get(f"{_servers_url(sid)}/remote/tools")
    assert r.status_code == 200, r.text
    assert [t["name"] for t in r.json()["tools"]] == ["add", "echo"]
    # The handshake ran and the credential header rode on the wire (and only
    # there — the response body never carries it).
    assert stub.requests[:2] == ["initialize", "tools/list"]
    assert all(
        h.get("Authorization") == "Bearer secret-token"
        for h in stub.seen_headers
    )
    assert "secret-token" not in r.text

    # The menu ignores the stored subset (it must show every candidate).
    client.put(f"{_servers_url(sid)}/remote/tools", json={"tools": ["echo"]})
    r = client.get(f"{_servers_url(sid)}/remote/tools")
    assert [t["name"] for t in r.json()["tools"]] == ["add", "echo"]

    # Prompt menu carries the slash-command token.
    r = client.get(f"{_servers_url(sid)}/remote/prompts")
    assert r.status_code == 200, r.text
    assert [p["noeta_name"] for p in r.json()["prompts"]] == [
        "mcp__remote__summarize"
    ]

    # Resource menu carries the mention token.
    r = client.get(f"{_servers_url(sid)}/remote/resources")
    assert r.status_code == 200, r.text
    assert [res["noeta_ref"] for res in r.json()["resources"]] == [
        "remote:mem://notes/readme"
    ]

    # Unknown alias → 404.
    assert client.get(f"{_servers_url(sid)}/ghost/tools").status_code == 404


def test_discovery_error_mapping(client, stub_mcp):
    login(client, "alice")
    sid = _new_team(client)

    # A handshake fault (HTTP 500 from the server) maps to 502.
    failing = stub_mcp(mode="fail")
    _create_http(client, sid, alias="broken", url=failing.url)
    r = client.get(f"{_servers_url(sid)}/broken/tools")
    assert r.status_code == 502, r.text

    # A connect fault (nothing listening) maps to 502 as well.
    _create_http(client, sid, alias="dead", url=_closed_port_url())
    assert client.get(f"{_servers_url(sid)}/dead/prompts").status_code == 502

    # stdio connectors have no server-side discovery → 400.
    client.post(
        _servers_url(sid),
        json={"alias": "local", "type": "stdio", "command": "mytool"},
    )
    r = client.get(f"{_servers_url(sid)}/local/tools")
    assert r.status_code == 400
    assert "http" in r.json()["detail"]

    # Discovery follows membership auth too: non-member → 404.
    login(client, "mallory")
    assert client.get(f"{_servers_url(sid)}/broken/tools").status_code == 404
