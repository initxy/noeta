"""Backend read-view acceptance — /capabilities + the session-list index.

The two index projections the UI shell needs beside the core task protocol:

* ``GET /capabilities`` — composer enums (agents / models / permission & effort
  modes / mcp servers) + the per-model vision gate, projected through noeta.sdk.
* ``GET /tasks`` — the root-conversation session list with a stream-folded
  status / closed / title; subtasks are filtered out (they ride the root's
  multiplexed stream).
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from typing import Any, Optional

from noeta.agent.backend import BackendConfig, EngineRoom, serve_backend
from noeta.agent.host.mcp_registry import McpServerRegistry
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.sdk import Options
from noeta.testing.fake_llm import FakeLLMProvider


def _provider(n: int = 6) -> FakeLLMProvider:
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text=f"reply-{i}")],
                usage=Usage(uncached=1, output=1),
            )
            for i in range(n)
        ]
    )


def _room(workspace: Path, *, model: Optional[str] = None) -> EngineRoom:
    return EngineRoom(
        Options(
            system_prompt="finish each turn",
            name="main",
            allowed_tools=(),
            permission_mode="bypassPermissions",
        ),
        provider=_provider(),
        workspace_dir=workspace,
        model=model,
    )


def _get(host: str, port: int, path: str) -> tuple[int, Any]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, (json.loads(data) if data else None)


# ---------------------------------------------------------------------------
# GET /capabilities
# ---------------------------------------------------------------------------


def test_capabilities_projects_enums_and_agents(tmp_path: Path) -> None:
    reg = McpServerRegistry(tmp_path / "mcp.json")
    reg.load()
    reg.upsert_http(alias="remote", url="https://x/mcp")
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=_room(tmp_path, model="opus"),
        mcp_registry=reg,
    )
    host, port = server.server_address[:2]
    try:
        status, body = _get(host, port, "/capabilities")
        assert status == 200, body
        # Always a command host → composer enabled.
        assert body["command_in"] is True and body["chat"] is True
        # The compiled registry's main agent is advertised.
        assert "main" in body["agents"]
        # Canonical enums via noeta.sdk.
        assert body["permission_modes"] == [
            "acceptEdits",
            "bypassPermissions",
            "default",
        ]
        assert body["effort_modes"] == ["high", "low", "max", "medium", "xhigh"]
        # The single bound model + its vision flag.
        assert body["models"] == ["opus"]
        assert body["model_capabilities"]["opus"]["supports_vision"] in (True, False)
        # The configured MCP connector (credential-scrubbed) is listed.
        assert [s["alias"] for s in body["mcp_servers"]] == ["remote"]
        # Unwired surfaces degrade to empty (thin local backend).
        assert body["workspaces"] == [] and body["providers"] == {}
        # Sandbox browser not activated → flags absent-false.
        assert body["sandbox_enabled"] is False
        assert body["browser_available"] is False
    finally:
        shutdown()


def test_capabilities_no_model_no_mcp(tmp_path: Path) -> None:
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=_room(tmp_path),  # no model, no mcp registry
    )
    host, port = server.server_address[:2]
    try:
        status, body = _get(host, port, "/capabilities")
        assert status == 200
        assert body["models"] == [] and body["model_capabilities"] == {}
        assert body["mcp_servers"] == []
    finally:
        shutdown()


def test_capabilities_sandbox_enabled(tmp_path: Path) -> None:
    """sandbox_enabled=True → /capabilities advertises sandbox + browser flags."""
    room = EngineRoom.official(
        provider=_provider(),
        workspace_dir=tmp_path,
        sandbox_browser=True,
    )
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=room,
    )
    host, port = server.server_address[:2]
    try:
        status, body = _get(host, port, "/capabilities")
        assert status == 200
        assert body["sandbox_enabled"] is True
        assert body["browser_available"] is True
        # Direction A: web subagent is registered in the agent list.
        assert "web" in body["agents"]
    finally:
        shutdown()


def test_task_preview_no_sandbox_returns_404(tmp_path: Path) -> None:
    """Without a sandbox gateway, GET /tasks/{id}/preview returns 404."""
    room = _room(tmp_path)
    # Create a task so we have an id.
    tid = room.start(goal="hello")
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=room,
    )
    host, port = server.server_address[:2]
    try:
        status, body = _get(host, port, f"/tasks/{tid}/preview")
        assert status == 404
    finally:
        shutdown()


def test_preview_info_panels_pin_container_paths() -> None:
    """W7-pinned panel sub-paths (live-verified against the AIO container).

    * browser — noVNC defaults its WS to ``ws://<host>/websockify``, escaping
      the token prefix; the ``?path=`` query param must steer it back inside.
    * terminal — no trailing slash: the page resolves its PTY WS relative to
      the URL, so ``.../terminal/`` would aim at ``terminal/v1/shell/ws``
      (404 upstream) while ``.../terminal`` lands on ``<prefix>/v1/shell/ws``.
    """
    from noeta.agent.host.sandbox_preview_gateway import SandboxPreviewGateway

    gw = SandboxPreviewGateway()
    mount = gw.mount_root("task-root", "http://127.0.0.1:9999", {})
    info = gw.preview_info("task-root")
    assert info is not None and info["token"] == mount.token
    panels = info["panels"]
    assert panels["browser"].startswith("vnc/index.html?")
    assert f"path=sandbox-preview/{mount.token}/websockify" in panels["browser"]
    assert panels["terminal"] == "terminal"
    assert panels["code"] == "code-server/"


# ---------------------------------------------------------------------------
# GET /tasks — session list
# ---------------------------------------------------------------------------


def test_session_list_folds_status_and_title(tmp_path: Path) -> None:
    room = _room(tmp_path)
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=room,
    )
    host, port = server.server_address[:2]
    try:
        # Empty before any conversation.
        status, body = _get(host, port, "/tasks")
        assert status == 200 and body == []

        # Drive two root conversations.
        t1 = room.start(goal="first conversation goal")
        t2 = room.start(goal="second\nmultiline goal")

        status, body = _get(host, port, "/tasks")
        assert status == 200
        ids = [r["task_id"] for r in body]
        assert set(ids) == {t1, t2}
        rows = {r["task_id"]: r for r in body}
        # Title is the genesis goal's first line.
        assert rows[t1]["title"] == "first conversation goal"
        assert rows[t2]["title"] == "second"
        # An interactive turn parks on a trailing suspend → "waiting", not closed.
        assert rows[t1]["status"] == "waiting"
        assert rows[t1]["closed"] is False
        assert rows[t1]["parent_task_id"] is None
        # Ordered most-recent-first (t2 has the higher high-water seq).
        assert ids[0] == t2

        # Close t1 → folded closed flag flips; status stays waiting (orthogonal).
        room.close(t1, reason="done")
        status, body = _get(host, port, "/tasks")
        rows = {r["task_id"]: r for r in body}
        assert rows[t1]["closed"] is True
        assert rows[t1]["status"] == "waiting"
    finally:
        shutdown()
