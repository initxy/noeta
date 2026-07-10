"""Backend workspace (project) acceptance — /workspaces CRUD + per-session bind.

* ``/capabilities`` advertises the workspace list (default first).
* ``/workspaces`` create (path-validated) / list / delete.
* ``POST /tasks`` with a chosen ``workspace`` welds the project's path into the
  session; ``GET /tasks`` reports it (so the sidebar groups by project). Zero
  mapping: the path lives in the durable stream, not a backend table.
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from typing import Any

from noeta.agent.backend import BackendConfig, EngineRoom, serve_backend
from noeta.agent.host.workspace_registry import WorkspaceRegistry
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.sdk import Options
from noeta.testing.fake_llm import FakeLLMProvider


def _provider() -> FakeLLMProvider:
    return FakeLLMProvider(
        responder=lambda req: LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="done")],
            usage=Usage(uncached=1, output=1),
        )
    )


def _room(workspace: Path, *, models: tuple[str, ...] = ()) -> EngineRoom:
    return EngineRoom(
        Options(
            system_prompt="finish each turn",
            name="main",
            allowed_tools=(),
            permission_mode="bypassPermissions",
        ),
        provider=_provider(),
        workspace_dir=workspace,
        model=models[0] if models else None,
        models=models,
    )


def _req(host: str, port: int, method: str, path: str, body: Any = None):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    payload = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if payload else {}
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, (json.loads(data) if data else None)


def _serve(tmp_path: Path, *, models: tuple[str, ...] = ()):
    default = tmp_path / "default_ws"
    default.mkdir()
    reg = WorkspaceRegistry(tmp_path / "workspaces.json", default_dir=default)
    reg.load()
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=default),
        engine_room=_room(default, models=models),
        workspace_registry=reg,
    )
    host, port = server.server_address[:2]
    return server, shutdown, host, port, reg, default


def test_capabilities_lists_workspaces_and_models(tmp_path: Path) -> None:
    server, shutdown, host, port, reg, _default = _serve(
        tmp_path, models=("gpt-a", "gpt-b")
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    reg.add(path=str(proj), name="proj")
    try:
        status, body = _req(host, port, "GET", "/capabilities")
        assert status == 200, body
        names = [w["name"] for w in body["workspaces"]]
        # Default first, then the user project.
        assert body["workspaces"][0]["is_default"] is True
        assert "proj" in names
        # The configured model list is the composer dropdown.
        assert body["models"] == ["gpt-a", "gpt-b"]
    finally:
        shutdown()


def test_workspaces_crud(tmp_path: Path) -> None:
    server, shutdown, host, port, _reg, _default = _serve(tmp_path)
    proj = tmp_path / "added"
    proj.mkdir()
    try:
        # Create.
        status, body = _req(host, port, "POST", "/workspaces", {"path": str(proj)})
        assert status == 201, body
        wid = body["id"]
        assert body["name"] == "added" and body["is_default"] is False

        # List shows default + the new one.
        status, body = _req(host, port, "GET", "/workspaces")
        assert status == 200
        assert wid in [w["id"] for w in body["workspaces"]]

        # Bad path → 400.
        status, body = _req(
            host, port, "POST", "/workspaces", {"path": str(tmp_path / "ghost")}
        )
        assert status == 400

        # Delete the user workspace → ok; delete default → 404.
        status, _ = _req(host, port, "DELETE", f"/workspaces/{wid}")
        assert status == 200
        default_id = next(
            w["id"]
            for w in _req(host, port, "GET", "/workspaces")[1]["workspaces"]
            if w["is_default"]
        )
        status, _ = _req(host, port, "DELETE", f"/workspaces/{default_id}")
        assert status == 404
    finally:
        shutdown()


def test_create_task_binds_chosen_workspace(tmp_path: Path) -> None:
    server, shutdown, host, port, reg, default = _serve(tmp_path)
    proj = tmp_path / "project_x"
    proj.mkdir()
    entry = reg.add(path=str(proj), name="project_x")
    try:
        # New session in the chosen project.
        status, body = _req(
            host, port, "POST", "/tasks",
            {"goal": "do a thing", "workspace": entry.id},
        )
        assert status == 202, body
        task_id = body["task_id"]

        # The session list reports the bound workspace (sidebar grouping).
        status, rows = _req(host, port, "GET", "/tasks")
        assert status == 200
        row = next(r for r in rows if r["task_id"] == task_id)
        assert row["workspace_dir"] == str(proj.resolve())
        assert row["workspace_name"] == "project_x"

        # An unknown workspace ref is rejected.
        status, _ = _req(
            host, port, "POST", "/tasks",
            {"goal": "x", "workspace": "bogus-id"},
        )
        assert status == 400
    finally:
        shutdown()


def test_single_model_followup_can_reselect_bound(tmp_path: Path) -> None:
    """A single-model deployment (``models=()`` + a host default) must let a
    follow-up turn re-select the bound model without tripping the per-turn
    selector allowlist — the configured default is always allowed (else the
    composer echoing the current model would fail every multi-turn session)."""
    ws = tmp_path / "solo_ws"
    ws.mkdir()
    room = EngineRoom(
        Options(
            system_prompt="finish each turn",
            name="main",
            allowed_tools=(),
            permission_mode="bypassPermissions",
        ),
        provider=_provider(),
        workspace_dir=ws,
        model="gpt-solo",
        models=(),
    )
    try:
        task_id = room.start(goal="first")
        # Echo the bound model on a follow-up: must NOT raise ModelSelectorError.
        room.send_goal(task_id, goal="again", model_selector="gpt-solo")
    finally:
        room.shutdown()


def test_create_task_without_workspace_uses_default(tmp_path: Path) -> None:
    """No workspace chosen ⇒ host default (byte-identical single-workspace path);
    the row's workspace_name resolves to the default bucket."""
    server, shutdown, host, port, _reg, default = _serve(tmp_path)
    try:
        status, body = _req(host, port, "POST", "/tasks", {"goal": "scratch"})
        assert status == 202
        task_id = body["task_id"]
        status, rows = _req(host, port, "GET", "/tasks")
        row = next(r for r in rows if r["task_id"] == task_id)
        # Host-default session: no welded path → workspace None, name = default.
        assert row["workspace_dir"] is None
        assert row["workspace_name"] == default.name
    finally:
        shutdown()
