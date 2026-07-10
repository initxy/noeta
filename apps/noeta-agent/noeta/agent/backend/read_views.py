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
single workspace, no provider registry / skill catalog, so those capability
lists are empty (the composer degrades them gracefully, exactly as the legacy
observation-only server did). ``slash_commands`` is the one exception: it is
always sourced from the built-in catalog (``noeta.agent.commands``), which
every deployment carries regardless of provider/workspace wiring.
"""

from __future__ import annotations

from typing import Any, Optional

from noeta.sdk import effort_modes, envelope_to_dict, model_capabilities, permission_modes

from noeta.agent.backend.app import BackendHandler, Router
from noeta.agent.commands import list_commands


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


def _slash_commands_public() -> list[dict[str, Any]]:
    """Project the built-in slash-command catalog for the composer's menu.

    ``noeta.agent.commands.list_commands()`` is the unit-tested catalog
    (``tests/test_code_commands.py``); the frontend slash menu
    (``apps/web/src/app/ChatComposer.jsx``'s ``slashMenuItems`` / ``SlashMenu``)
    only reads ``name`` / ``description`` / ``argument_hint`` per command, so
    those are the only fields projected here — ``kind`` / ``skill`` / ``agent``
    are resolution-only details the composer never touches.
    """
    return [
        {
            "name": c.name,
            "description": c.description,
            "argument_hint": c.argument_hint,
        }
        for c in list_commands()
    ]


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
            "sandbox_enabled": room.sandbox_enabled,
            "browser_available": room.sandbox_enabled,
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
            "slash_commands": _slash_commands_public(),
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


def _genesis_parent_task_id(envelopes: list[Any]) -> Optional[str]:
    """Peek at a stream's genesis ``TaskCreated`` for its ``parent_task_id``.

    Every task's stream begins with its genesis ``TaskCreated`` at seq 0 (the
    same invariant ``stream.py``'s catch-up phase relies on — "a genesis
    envelope at seq == 0 is NOT skipped"), so checking ``envelopes[0]`` is
    enough; there is no need to run the full :func:`_fold_summary` fold (which
    walks every envelope and serializes each one through ``envelope_to_dict``)
    just to read one field. This lets :func:`_handle_list_tasks` skip a
    subtask's stream BEFORE paying for that fold, not after.

    NOTE (residual cost): the underlying ``EventLogReader.read`` has no
    first-event-only primitive, so the full stream is still fetched from
    storage either way — this only avoids the redundant per-envelope
    fold/serialize CPU work for streams that end up discarded.
    """
    if not envelopes:
        return None
    first = envelopes[0]
    if getattr(first, "type", None) == "TaskCreated":
        return getattr(first.payload, "parent_task_id", None)
    # Defensive fallback (should not happen — genesis is always seq 0): scan
    # the rest so a malformed/legacy stream doesn't silently mis-tree.
    for env in envelopes[1:]:
        if getattr(env, "type", None) == "TaskCreated":
            return getattr(env.payload, "parent_task_id", None)
    return None


def _handle_list_tasks(handler: BackendHandler, params: dict[str, str]) -> None:
    """``GET /tasks`` → the root-conversation session list (most-recent first).

    Each row carries its ``workspace`` (the durable per-session path, ``None`` ⇒
    the host default / scratch bucket) + a display ``workspace_name`` resolved
    from the registry, so the sidebar groups sessions by project without opening
    each stream.

    Every task stream is still read once (there is no cheaper way to learn a
    stream's genesis without reading it — see :func:`_genesis_parent_task_id`),
    but a subtask's stream is discarded right after that cheap peek: the full
    :func:`_fold_summary` fold (and the ``envelope_to_dict`` serialization it
    does per envelope) now only runs for streams that actually become a row,
    which matters most on a deployment with many/long-lived subtask streams.
    Root-session output stays byte-identical to the previous full-fold path.
    """
    room = handler.engine_room
    reg = handler.workspace_registry
    rows: list[dict[str, Any]] = []
    for summary in room.task_streams():
        task_id = getattr(summary, "task_id", None)
        if not isinstance(task_id, str):
            continue
        envelopes = room.events(task_id)
        # Roots only: a subtask rides its parent's multiplexed stream and is
        # never a top-level session row (docs/.../07-frontend-fold.md). Check
        # cheaply BEFORE the full fold so a subtask's (possibly long) history
        # is never folded/serialized just to be thrown away.
        if _genesis_parent_task_id(envelopes):
            continue
        folded = _fold_summary(envelopes)
        workspace = folded.get("workspace_dir")
        workspace_name = (
            reg.name_for_path(workspace) if reg is not None else None
        )
        rows.append(
            {"task_id": task_id, **folded, "workspace_name": workspace_name}
        )
    rows.sort(key=lambda r: r["last_seq"], reverse=True)
    handler.send_json(rows)


def _handle_task_preview(handler: BackendHandler, params: dict[str, str]) -> None:
    """``GET /tasks/{id}/preview`` → sandbox live-preview discovery payload.

    Returns ``200 {token, panels:{browser, terminal, code}}`` for a task
    whose session has a live sandbox container mounted; ``404`` for a task
    without a sandbox (non-sandbox deployment, or session not yet allocated).
    The frontend uses this to decide whether to show the preview panel
    picker (D4).
    """
    task_id = params.get("id", "")
    gw = handler.sandbox_preview_gateway
    if gw is None:
        handler.send_json({"error": "sandbox preview not available"}, status=404)
        return
    # The preview mount is keyed by root_task_id. For a subtask, we'd need
    # to walk up to the root; v1 only supports root-task discovery (the
    # frontend opens the panel from the active root conversation).
    info = gw.preview_info(task_id)
    if info is None:
        handler.send_json({"error": "no sandbox for this session"}, status=404)
        return
    handler.send_json(info)


def register_read_view_routes(router: Router) -> None:
    """Register the capabilities + session-list index views onto ``router``."""
    router.add("GET", "/capabilities", _handle_capabilities)
    router.add("GET", "/tasks", _handle_list_tasks)
    router.add("GET", "/tasks/{id}/preview", _handle_task_preview)
