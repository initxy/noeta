"""task_protocol — the core task protocol routes (T5).

The small, hard core of the
front/back contract — ① one SSE multiplexed envelope stream and ② a handful of command
endpoints aligned to the ``Client`` verbs. Commands return ``202`` + an ack only;
every visible change is observed through the stream (single source of truth).
The ancillary resource services (files / content / preview / mcp) are NOT here (T6).
"""

from __future__ import annotations

from typing import Any, Optional

from noeta.agent.backend.app import BackendHandler, Router
from noeta.agent.backend.image_input import ImageInputError, build_image_blocks
from noeta.agent.backend.stream import stream_frames


def _enabled_mcp(body: dict[str, Any]) -> tuple[str, ...]:
    """The turn's enabled MCP aliases from the request body (clean strings)."""
    raw = body.get("enabled_mcp")
    if not isinstance(raw, list):
        return ()
    return tuple(str(a) for a in raw if isinstance(a, str) and a)


def _opt_str(body: dict[str, Any], key: str) -> Optional[str]:
    """A non-empty string body field, else ``None`` (the per-turn selector idiom)."""
    v = body.get(key)
    return v if isinstance(v, str) and v else None


def _resolve_workspace_or_400(
    handler: BackendHandler, body: dict[str, Any]
) -> tuple[Optional[str], bool]:
    """Resolve ``body['workspace']`` (a workspace id or path) → an absolute path.

    The chosen project's path is welded
    into durable ``TaskHostBound`` and fold-resolved on later turns. Returns
    ``(path, ok)``: ``(None, True)`` when no workspace is given (the host-default
    / scratch bucket, byte-identical to the single-workspace path); a given but
    unresolvable ref sends a 400 and returns ``(None, False)``.
    """
    ref = body.get("workspace")
    if ref is None or ref == "":
        return None, True
    if not isinstance(ref, str):
        handler.send_json({"error": "'workspace' must be a string"}, status=400)
        return None, False
    reg = handler.workspace_registry
    path = reg.resolve(ref) if reg is not None else None
    if path is None:
        handler.send_json({"error": f"unknown workspace: {ref!r}"}, status=400)
        return None, False
    return path, True


def _image_blocks_or_400(
    handler: BackendHandler, body: dict[str, Any]
) -> Optional[list[Any]]:
    """Decode the body's ``images`` → ``ImageBlock``s, or send a 400 and return None.

    A bad attachment (non-whitelisted MIME / illegal base64 / over 5MB) is the
    client's fault, so the handler replies 400 and the caller returns early — the
    task is neither created nor advanced. ``None``/empty ``images`` → ``[]`` (the
    text-only path is unchanged).
    """
    try:
        return build_image_blocks(handler.engine_room, body.get("images"))
    except ImageInputError as exc:
        handler.send_json({"error": str(exc)}, status=400)
        return None


def _handle_stream(handler: BackendHandler, params: dict[str, str]) -> None:
    root = handler.query_params().get("task")
    if not root:
        handler.send_json({"error": "query param 'task' is required"}, status=400)
        return
    last_id = handler.headers.get("Last-Event-ID")
    handler.stream_sse(stream_frames(handler.engine_room, root, last_id))


def _handle_create_task(handler: BackendHandler, params: dict[str, str]) -> None:
    body = handler.read_json_body()
    images = _image_blocks_or_400(handler, body)
    if images is None:
        return
    workspace_dir, ok = _resolve_workspace_or_400(handler, body)
    if not ok:
        return
    task_id = handler.engine_room.start(
        goal=str(body.get("goal", "")),
        agent=body.get("agent"),
        images=images,
        permission_mode=body.get("permission_mode"),
        enabled_mcp=_enabled_mcp(body),
        workspace_dir=workspace_dir,
        model_selector=_opt_str(body, "model"),
        effort=_opt_str(body, "effort"),
    )
    handler.send_json({"task_id": task_id}, status=202)


def _handle_send_goal(handler: BackendHandler, params: dict[str, str]) -> None:
    body = handler.read_json_body()
    images = _image_blocks_or_400(handler, body)
    if images is None:
        return
    handler.engine_room.send_goal(
        params["id"],
        goal=str(body.get("goal", "")),
        images=images,
        permission_mode=body.get("permission_mode"),
        enabled_mcp=_enabled_mcp(body),
        model_selector=_opt_str(body, "model"),
        effort=_opt_str(body, "effort"),
    )
    handler.send_json({"task_id": params["id"]}, status=202)


def _handle_approve(handler: BackendHandler, params: dict[str, str]) -> None:
    body = handler.read_json_body()
    handler.engine_room.approve(
        params["id"], call_id=str(body.get("call_id", "")), reason=body.get("reason")
    )
    handler.send_json({"task_id": params["id"]}, status=202)


def _handle_deny(handler: BackendHandler, params: dict[str, str]) -> None:
    body = handler.read_json_body()
    handler.engine_room.deny(
        params["id"], call_id=str(body.get("call_id", "")), reason=body.get("reason")
    )
    handler.send_json({"task_id": params["id"]}, status=202)


def _handle_answer(handler: BackendHandler, params: dict[str, str]) -> None:
    body = handler.read_json_body()
    handler.engine_room.answer(
        params["id"],
        question_id=str(body.get("question_id", "")),
        answers=dict(body.get("answers", {})),
    )
    handler.send_json({"task_id": params["id"]}, status=202)


def _handle_cancel(handler: BackendHandler, params: dict[str, str]) -> None:
    body = handler.read_json_body()
    handler.engine_room.cancel(
        params["id"],
        reason=str(body.get("reason", "cancelled")),
        cascade=bool(body.get("cascade", False)),
    )
    handler.send_json({"task_id": params["id"]}, status=202)


def _handle_close(handler: BackendHandler, params: dict[str, str]) -> None:
    body = handler.read_json_body()
    handler.engine_room.close(params["id"], reason=body.get("reason"))
    handler.send_json({"task_id": params["id"]}, status=202)


def _handle_reopen(handler: BackendHandler, params: dict[str, str]) -> None:
    body = handler.read_json_body()
    handler.engine_room.reopen(params["id"], reason=body.get("reason"))
    handler.send_json({"task_id": params["id"]}, status=202)


def _handle_delete_task(handler: BackendHandler, params: dict[str, str]) -> None:
    """``DELETE /tasks/{id}`` — hard-delete a session (task + subtask tree).

    Unlike the command verbs above (which return ``202`` and let the change land
    via the stream), a delete purges the stream itself, so it answers
    synchronously: ``200`` with the purged ids, ``409`` when a task in the tree
    is actively running, ``404`` when the root is unknown.
    """
    result = handler.engine_room.delete_task(params["id"])
    if result.get("ok"):
        handler.send_json(result, status=200)
        return
    status = 409 if result.get("reason") == "running" else 404
    handler.send_json(result, status=status)


def register_task_routes(router: Router) -> None:
    """Register the SSE stream + the command endpoints onto ``router``."""
    router.add("GET", "/stream", _handle_stream)
    router.add("POST", "/tasks", _handle_create_task)
    router.add("POST", "/tasks/{id}/messages", _handle_send_goal)
    router.add("POST", "/tasks/{id}/approve", _handle_approve)
    router.add("POST", "/tasks/{id}/deny", _handle_deny)
    router.add("POST", "/tasks/{id}/answer", _handle_answer)
    router.add("POST", "/tasks/{id}/cancel", _handle_cancel)
    router.add("POST", "/tasks/{id}/close", _handle_close)
    router.add("POST", "/tasks/{id}/reopen", _handle_reopen)
    router.add("DELETE", "/tasks/{id}", _handle_delete_task)
