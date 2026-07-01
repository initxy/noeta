"""T6 remaining acceptance — preview gateway + MCP management on the new backend.

The two remaining peripheral services, wired through noeta.sdk's HostConfig:

* ``/preview/<token>/...`` — the HTML-app preview gateway, prefix-routed in the
  handler dispatch (single-port: served from THIS server, with the /api shim
  injected into HTML and CORS on the /api proxy).
* ``/mcp/servers*`` — the MCP connector config CRUD + discovery, over the
  reused ``McpServerRegistry`` (credentials stored host-side, scrubbed on read).

Both are decoupled from the core task protocol (``/stream`` + ``/tasks/*``).
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from typing import Any, Optional

from noeta.agent.backend import BackendConfig, EngineRoom, serve_backend
from noeta.agent.host.mcp_registry import McpServerRegistry
from noeta.agent.host.preview_gateway import PreviewGateway
from noeta.sdk import Options
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.protocols.messages import LLMResponse, TextBlock, Usage


def _room(workspace: Path) -> EngineRoom:
    return EngineRoom(
        Options(
            system_prompt="finish",
            name="main",
            allowed_tools=(),
            permission_mode="bypassPermissions",
        ),
        provider=FakeLLMProvider(
            responses=[
                LLMResponse(
                    stop_reason="end_turn",
                    content=[TextBlock(text="ok")],
                    usage=Usage(uncached=1, output=1),
                )
            ]
        ),
        workspace_dir=workspace,
    )


def _req(
    host: str, port: int, method: str, path: str, body: Optional[Any] = None
) -> tuple[int, bytes, str]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    payload = json.dumps(body) if body is not None else None
    conn.request(method, path, body=payload)
    resp = conn.getresponse()
    data = resp.read()
    ctype = resp.getheader("Content-Type", "")
    conn.close()
    return resp.status, data, ctype


def _options(
    host: str, port: int, path: str, request_headers: str
) -> tuple[int, dict[str, str]]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request(
        "OPTIONS",
        path,
        headers={"Access-Control-Request-Headers": request_headers},
    )
    resp = conn.getresponse()
    resp.read()
    headers = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, headers


# ---------------------------------------------------------------------------
# MCP management
# ---------------------------------------------------------------------------


def test_mcp_crud_lifecycle_and_credential_scrub(tmp_path: Path) -> None:
    reg = McpServerRegistry(tmp_path / "mcp.json")
    reg.load()
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=_room(tmp_path),
        mcp_registry=reg,
    )
    host, port = server.server_address[:2]
    try:
        # Empty list.
        status, body, _ = _req(host, port, "GET", "/mcp/servers")
        assert status == 200 and json.loads(body)["servers"] == []

        # Create an http connector with a credential header.
        status, body, _ = _req(
            host,
            port,
            "POST",
            "/mcp/servers",
            {
                "alias": "remote",
                "type": "http",
                "url": "https://x/mcp",
                "headers": {"Authorization": "Bearer secret"},
            },
        )
        assert status == 201, body
        public = json.loads(body)
        # Credential value scrubbed → only the header NAME is echoed.
        assert public["header_names"] == ["Authorization"]
        assert "secret" not in body.decode()

        # Create a stdio connector.
        status, _, _ = _req(
            host,
            port,
            "POST",
            "/mcp/servers",
            {"alias": "local", "type": "stdio", "command": "mytool", "args": ["--x"]},
        )
        assert status == 201

        # List shows both (alias-sorted).
        status, body, _ = _req(host, port, "GET", "/mcp/servers")
        assert [s["alias"] for s in json.loads(body)["servers"]] == ["local", "remote"]

        # Edit merges: set a tool subset, keep the url + credential untouched.
        status, body, _ = _req(
            host, port, "PUT", "/mcp/servers/remote", {"tools": ["a", "b"]}
        )
        assert status == 200
        merged = json.loads(body)
        assert merged["tools"] == ["a", "b"]
        assert merged["url"] == "https://x/mcp"
        assert merged["header_names"] == ["Authorization"]

        # Set-tools sub-resource clears the subset.
        status, body, _ = _req(
            host, port, "PUT", "/mcp/servers/remote/tools", {"tools": None}
        )
        assert status == 200 and json.loads(body)["tools"] is None

        # Delete + idempotent 404.
        assert _req(host, port, "DELETE", "/mcp/servers/remote")[0] == 200
        assert _req(host, port, "DELETE", "/mcp/servers/remote")[0] == 404

        # Bad input → 400; unknown alias edit → 404.
        assert _req(host, port, "POST", "/mcp/servers", {"type": "http"})[0] == 400
        assert _req(host, port, "PUT", "/mcp/servers/ghost", {"url": "h"})[0] == 404
    finally:
        shutdown()


def test_mcp_discovery_endpoints_via_fake_transport(tmp_path: Path) -> None:
    from tests._fixtures.fake_http_mcp_server import FakeHttpMcpServer

    fake = FakeHttpMcpServer(mode="echo")
    reg = McpServerRegistry(tmp_path / "mcp.json", http_post=fake.post)
    reg.load()
    reg.upsert_http(alias="remote", url="https://x/mcp")

    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=_room(tmp_path),
        mcp_registry=reg,
    )
    host, port = server.server_address[:2]
    try:
        # Tool menu (full advertised set).
        status, body, _ = _req(host, port, "GET", "/mcp/servers/remote/tools")
        assert status == 200, body
        assert [t["name"] for t in json.loads(body)["tools"]]

        # Prompt menu (slash names + arg schema).
        status, body, _ = _req(host, port, "GET", "/mcp/servers/remote/prompts")
        assert status == 200, body
        prompts = json.loads(body)["prompts"]
        assert [p["noeta_name"] for p in prompts] == ["mcp__remote__summarize"]

        # Unknown alias → 404.
        assert _req(host, port, "GET", "/mcp/servers/nope/prompts")[0] == 404
    finally:
        shutdown()


def test_mcp_routes_return_503_without_registry(tmp_path: Path) -> None:
    # No mcp_registry wired ⇒ the MCP routes report "not configured".
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=_room(tmp_path),
    )
    host, port = server.server_address[:2]
    try:
        assert _req(host, port, "GET", "/mcp/servers")[0] == 503
        # Core protocol unaffected by the absent peripheral service.
        assert _req(host, port, "GET", "/health")[0] == 200
    finally:
        shutdown()


# ---------------------------------------------------------------------------
# Preview gateway
# ---------------------------------------------------------------------------


def test_preview_serves_app_with_api_shim_and_cors(tmp_path: Path) -> None:
    # Build a tiny HTML app in the workspace and mount it on the gateway
    # (simulating what the open_app tool does on the agent side).
    app_dir = tmp_path / "myapp"
    app_dir.mkdir()
    (app_dir / "index.html").write_text(
        "<html><head><title>demo</title></head><body>hi</body></html>",
        encoding="utf-8",
    )
    gateway = PreviewGateway()
    mount = gateway.mount(
        workspace_dir=tmp_path,
        app_rel="myapp",
        proxy_to="http://127.0.0.1:1/upstream",
        task_id="t1",
    )

    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=_room(tmp_path),
        app_gateway=gateway,
    )
    host, port = server.server_address[:2]
    try:
        # The render URL is the relative single-port path.
        assert mount.url == f"/preview/{mount.token}/"

        # GET the app index → 200 HTML with the /api rewrite shim spliced in.
        status, body, ctype = _req(host, port, "GET", mount.url)
        assert status == 200
        assert ctype.startswith("text/html")
        text = body.decode()
        assert "data-noeta-api-shim" in text
        assert "hi" in text

        # OPTIONS preflight on the /api proxy → 204 + permissive CORS that echoes
        # the requested headers (so app-specific headers pass the sandbox).
        status, headers = _options(
            host, port, f"/preview/{mount.token}/api/thing", "X-App-Id"
        )
        assert status == 204
        assert headers.get("access-control-allow-origin") == "*"
        assert headers.get("access-control-allow-headers") == "X-App-Id"

        # Unknown token → 404 (still the gateway's response, not a fall-through).
        assert _req(host, port, "GET", "/preview/badtoken/")[0] == 404
    finally:
        shutdown()


def test_preview_absent_gateway_falls_through_to_404(tmp_path: Path) -> None:
    # No app_gateway wired ⇒ /preview is not special-cased; it 404s as a normal
    # unknown route (core protocol unaffected).
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=_room(tmp_path),
    )
    host, port = server.server_address[:2]
    try:
        assert _req(host, port, "GET", "/preview/x/")[0] == 404
        assert _req(host, port, "GET", "/health")[0] == 200
    finally:
        shutdown()
