"""workspace_service — the workspace (project) management routes (ancillary service).

The CRUD
face over the host's workspace registry
(``noeta.agent.host.workspace_registry.WorkspaceRegistry``). The codex-style
frontend lists these as "projects", picks one when creating a session, and groups
the session pane by workspace. Per-session binding is a separate concern: the
chosen workspace's absolute path is forwarded to ``POST /tasks`` and welded into
durable ``TaskHostBound`` by the driver (zero mapping — a resumed turn
fold-resolves it).

Endpoints (all under ``/workspaces``):

* ``GET    /workspaces``        — list workspaces (default first, ``is_default``)
* ``POST   /workspaces``        — register a workspace by absolute path
* ``DELETE /workspaces/{id}``   — remove the registry entry (NOT the directory)

A bad path → ``400``; an absent registry → ``503``; deleting the default / an
unknown id → ``404``.
"""

from __future__ import annotations

from typing import Any, Optional

from noeta.agent.backend.app import BackendHandler, Router
from noeta.agent.host.workspace_registry import WorkspaceConfigError


def _registry_or_503(handler: BackendHandler) -> Optional[Any]:
    reg = handler.workspace_registry
    if reg is None:
        handler.send_json(
            {"error": "workspace registry is not configured"}, status=503
        )
    return reg


def _handle_list(handler: BackendHandler, params: dict[str, str]) -> None:
    reg = _registry_or_503(handler)
    if reg is None:
        return
    handler.send_json(
        {"workspaces": [e.as_public_dict() for e in reg.list_all()]}
    )


def _handle_create(handler: BackendHandler, params: dict[str, str]) -> None:
    reg = _registry_or_503(handler)
    if reg is None:
        return
    body = handler.read_json_body()
    path = body.get("path")
    name = body.get("name")
    if not isinstance(path, str) or not path:
        handler.send_json({"error": "'path' is required"}, status=400)
        return
    if name is not None and not isinstance(name, str):
        handler.send_json({"error": "'name' must be a string"}, status=400)
        return
    try:
        entry = reg.add(path=path, name=name)
    except WorkspaceConfigError as exc:
        handler.send_json({"error": str(exc)}, status=400)
        return
    handler.send_json(entry.as_public_dict(), status=201)


def _handle_delete(handler: BackendHandler, params: dict[str, str]) -> None:
    reg = _registry_or_503(handler)
    if reg is None:
        return
    removed = reg.remove(params["id"])
    if not removed:
        handler.send_json(
            {"error": "not found or not removable", "id": params["id"]}, status=404
        )
        return
    handler.send_json({"ok": True, "id": params["id"]})


def register_workspace_routes(router: Router) -> None:
    """Register the workspace (project) management routes onto ``router``."""
    router.add("GET", "/workspaces", _handle_list)
    router.add("POST", "/workspaces", _handle_create)
    router.add("DELETE", "/workspaces/{id}", _handle_delete)
