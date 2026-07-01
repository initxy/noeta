"""read_views — composer capabilities + the session-list index (ancillary read views).

The core task protocol is the
SSE stream + the command endpoints; everything else is an ancillary service. Two of
those are read-only *index* projections the UI shell needs before (or beside) any
single conversation's stream:

* ``GET /capabilities`` — the composer's selectable enums (agents / models /
  permission & effort modes / mcp servers) + the per-model vision gate. None of
  this lives on a task's event stream, so it is a projection, not a fold of the
  stream. Everything is read through ``noeta.sdk`` (the capability helpers) + the
  host's MCP registry — the backend never names a runtime internal (D2).

* ``GET /tasks`` — the session list: the ROOT conversations this process is
  driving, each with a stream-folded ``status`` / ``closed`` / ``title`` so the
  sidebar can render without opening every stream. Subtasks are filtered out
  (they ride their root's multiplexed stream — docs/.../07-frontend-fold.md);
  the frontend keys the active conversation by ``task_id`` and folds the rest
  from ``GET /stream?task=<root>``.

This is a thin server-side projection (like the file tree in
:mod:`noeta.agent.backend.resource_services`), explicitly allowed for non-event
index views — the per-conversation UI state still folds on the frontend (D7).
The thin backend advertises only what it actually wires: a single model + a
single workspace, no provider registry / workspace registry / skill catalog, so
those capability lists are empty (the composer degrades them gracefully, exactly
as the legacy observation-only server did).
"""

from __future__ import annotations

from typing import Any, Optional

from noeta.sdk import effort_modes, envelope_to_dict, model_capabilities, permission_modes

from noeta.agent.backend.app import BackendHandler, Router


_MAX_TITLE_CHARS = 80

# Lifecycle event → folded conversation status (mirror of the frontend reducer's
# STATUS_FOR_TYPE, docs/.../07-frontend-fold.md). An interactive turn ends on a
# trailing TaskSuspended → "waiting" (NOT terminal); only the one-shot run path
# reaches a terminal TaskCompleted / TaskFailed.
_STATUS_FOR_TYPE = {
    "TaskCreated": "created",
    "TaskStarted": "running",
    "TaskWoken": "running",
    "TaskSuspended": "waiting",
    "TaskCompleted": "completed",
    "TaskFailed": "failed",
    "TaskCancelled": "cancelled",
}


# ---------------------------------------------------------------------------
# GET /capabilities
# ---------------------------------------------------------------------------


def _mcp_servers_public(handler: BackendHandler) -> list[dict[str, Any]]:
    reg = handler.mcp_registry
    if reg is None:
        return []
    try:
        return [e.as_public_dict() for e in reg.list_all()]
    except Exception:
        return []


def _workspaces_public(handler: BackendHandler) -> list[dict[str, Any]]:
    reg = handler.workspace_registry
    if reg is None:
        return []
    try:
        return [e.as_public_dict() for e in reg.list_all()]
    except Exception:
        return []


def _handle_capabilities(handler: BackendHandler, params: dict[str, str]) -> None:
    """``GET /capabilities`` → the composer's selectable surface (read-only)."""
    room = handler.engine_room
    # The configured model list (the composer's model dropdown). Falls back to
    # the single host-bound model when no list is configured; empty ⇒ the
    # gateway default (no concrete selector to advertise).
    models = room.models or ([room.model] if room.model else [])
    handler.send_json(
        {
            # The new backend always drives turns (it is the command host), so
            # the composer is always enabled — the old observation-only "no
            # driver ⇒ command_in False" mode does not exist here.
            "command_in": True,
            "chat": True,
            "agents": room.agent_names(),
            "models": models,
            "model_capabilities": model_capabilities(models),
            "permission_modes": list(permission_modes()),
            "effort_modes": list(effort_modes()),
            "mcp_servers": _mcp_servers_public(handler),
            # The
            # workspace (project) list, default first. Single provider / no skill
            # catalog stay unwired (empty lists degrade gracefully).
            "workspaces": _workspaces_public(handler),
            "skills": [],
            "slash_commands": [],
            "providers": {},
            "default_provider": "",
        }
    )


# ---------------------------------------------------------------------------
# GET /tasks — the session list (root conversations)
# ---------------------------------------------------------------------------


def _fold_summary(envelopes: list[Any]) -> dict[str, Any]:
    """Fold a task's raw envelope stream into a session-list summary.

    Thin status/closed/title fold (the per-conversation UI state still folds on
    the frontend, D7). Reads the canonical envelope dicts (the same shape the
    frontend reducer sees) so the projection stays free of runtime internals.
    """
    status = "unknown"
    closed = False
    title: Optional[str] = None
    agent_name: Optional[str] = None
    parent_task_id: Optional[str] = None
    workspace: Optional[str] = None
    last_seq = -1
    for env in envelopes:
        d = envelope_to_dict(env)
        seq = d.get("seq")
        if isinstance(seq, int) and seq > last_seq:
            last_seq = seq
        etype = d.get("type")
        payload = d.get("payload") or {}
        next_status = _STATUS_FOR_TYPE.get(etype)
        if next_status is not None:
            status = next_status
        if etype == "TaskCreated":
            goal = payload.get("goal")
            if isinstance(goal, str):
                title = _title_from_goal(goal)
            agent_name = payload.get("agent_name")
            parent_task_id = payload.get("parent_task_id")
        elif etype == "TaskHostBound":
            # The per-session workspace
            # absolute path, welded once at creation; the sidebar groups by it.
            # The field name matches the frontend's groupSessionsByWorkspace,
            # which keys rows on ``workspace_dir`` against ``options.workspaces``.
            ws = payload.get("workspace_dir")
            if isinstance(ws, str) and ws:
                workspace = ws
        elif etype == "ConversationClosed":
            closed = True
        elif etype == "ConversationReopened":
            closed = False
    return {
        "status": status,
        "closed": closed,
        "title": title,
        "agent_name": agent_name,
        "parent_task_id": parent_task_id,
        "workspace_dir": workspace,
        "last_seq": last_seq,
    }


def _title_from_goal(goal: str) -> Optional[str]:
    """A short session label from the genesis goal (first line, truncated)."""
    first = goal.strip().splitlines()[0].strip() if goal.strip() else ""
    if not first:
        return None
    return first if len(first) <= _MAX_TITLE_CHARS else first[: _MAX_TITLE_CHARS - 1] + "…"


def _handle_list_tasks(handler: BackendHandler, params: dict[str, str]) -> None:
    """``GET /tasks`` → the root-conversation session list (most-recent first).

    Each row carries its ``workspace`` (the durable per-session path, ``None`` ⇒
    the host default / scratch bucket) + a display ``workspace_name`` resolved
    from the registry, so the sidebar groups sessions by project without opening
    each stream.
    """
    room = handler.engine_room
    reg = handler.workspace_registry
    rows: list[dict[str, Any]] = []
    for summary in room.task_streams():
        task_id = getattr(summary, "task_id", None)
        if not isinstance(task_id, str):
            continue
        folded = _fold_summary(room.events(task_id))
        # Roots only: a subtask rides its parent's multiplexed stream and is
        # never a top-level session row (docs/.../07-frontend-fold.md).
        if folded["parent_task_id"]:
            continue
        workspace = folded.get("workspace_dir")
        workspace_name = (
            reg.name_for_path(workspace) if reg is not None else None
        )
        rows.append(
            {"task_id": task_id, **folded, "workspace_name": workspace_name}
        )
    rows.sort(key=lambda r: r["last_seq"], reverse=True)
    handler.send_json(rows)


def register_read_view_routes(router: Router) -> None:
    """Register the capabilities + session-list index views onto ``router``."""
    router.add("GET", "/capabilities", _handle_capabilities)
    router.add("GET", "/tasks", _handle_list_tasks)
