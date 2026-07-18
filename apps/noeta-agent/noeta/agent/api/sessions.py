"""Session endpoints: CRUD (including template / workflow starts), message /
answer / cancel (202 async driving), workflow advance (preview/confirm), the
SSE event stream (per-task filtering), and the sandbox file surface."""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Optional

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from noeta.agent.api.image_input import ImageInputError, build_image_blocks
from noeta.agent.api.spaces import require_space_member
from noeta.agent.auth.deps import CurrentUser, get_current_user
from noeta.agent.host.service import AgentService, SessionBusyError
from noeta.agent.host.translator import UIEvent
from noeta.agent.store.sessions import Session, SessionStore
from noeta.agent.store.spaces import ROLE_OWNER, SpaceStore
from noeta.agent.store.templates import TemplateStore
from noeta.agent.workflow.handoff import generate_handoff
from noeta.agent.workflow.service import (
    build_workflow_snapshot,
    next_node_index,
    node_goal,
    workflow_view,
)
from noeta.agent.workflow.templates import render_prompt

router = APIRouter(prefix="/sessions", tags=["sessions"])

_HEARTBEAT_SECONDS = 15.0
_FILE_CLIP_BYTES = 200 * 1024


def _store(request: Request) -> SessionStore:
    return request.app.state.session_store


def _spaces(request: Request) -> SpaceStore:
    return request.app.state.space_store


def _templates(request: Request) -> TemplateStore:
    return request.app.state.template_store


def _service(request: Request) -> AgentService:
    return request.app.state.agent_service


def _own_session(request: Request, session_id: str, user: CurrentUser) -> Session:
    """Session visibility = the requester is a member of the session's space;
    otherwise 404 (hiding existence)."""
    session = _store(request).get(session_id)
    if session is None or not _spaces(request).is_member(
        session.space_id, user.username
    ):
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _session_payload(request: Request, session: Session) -> dict:
    """session -> API response: workflow sessions carry the workflow view
    (the data source for the tab bar)."""
    out = session.to_api()
    snapshot = session.workflow
    if snapshot is not None:
        tasks = _store(request).list_session_tasks(session.id)
        out["workflow"] = workflow_view(snapshot, tasks)
    return out


def _resolve_task(
    request: Request, session: Session, task_id: Optional[str]
) -> Optional[str]:
    """Validate that task_id belongs to this session (a workflow node task or
    session.task_id); otherwise 404."""
    if not task_id:
        return None
    if task_id == session.task_id:
        return task_id
    row = _store(request).get_session_task_by_task_id(task_id)
    if row is None or row["session_id"] != session.id:
        raise HTTPException(status_code=404, detail="task not found")
    return task_id


def _check_required_params(params_def: list[dict], values: dict) -> None:
    missing = [
        p["name"] for p in params_def
        if p.get("required") and not str(values.get(p["name"]) or "").strip()
    ]
    if missing:
        raise HTTPException(
            status_code=422, detail=f"missing required params: {', '.join(missing)}"
        )


# ------------------------------------------------------------------- CRUD
class CreateSessionBody(BaseModel):
    space_id: str = Field(min_length=1)
    model: Optional[str] = None
    # Template starts: template_id for a single template / workflow_template_id
    # for a workflow, mutually exclusive (both empty = plain session). params
    # holds the parameter values for the single template / the workflow's first
    # node.
    template_id: Optional[str] = None
    workflow_template_id: Optional[str] = None
    params: dict = Field(default_factory=dict)


@router.get("")
async def list_sessions(
    request: Request,
    space_id: str = Query(min_length=1),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    require_space_member(request, space_id, user)
    sessions = _store(request).list_for_space(space_id)
    return {"sessions": [s.to_api() for s in sessions]}


@router.post("", status_code=201)
async def create_session(
    body: CreateSessionBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    require_space_member(request, body.space_id, user)
    settings = request.app.state.settings
    from noeta.agent.models_config import get_default_model, validate_model

    if body.model:
        try:
            validate_model(settings, body.model)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"unknown model: {body.model}")
        model = body.model
    else:
        # The space agent-config default model wins (invalid / unset falls back
        # to the platform default).
        space_default = request.app.state.agent_config_store.get(
            body.space_id
        ).get("default_model")
        if space_default:
            try:
                validate_model(settings, space_default)
                model = space_default
            except ValueError:
                model = get_default_model(settings).id
        else:
            model = get_default_model(settings).id
    if body.template_id and body.workflow_template_id:
        raise HTTPException(
            status_code=422,
            detail="template_id and workflow_template_id are mutually exclusive",
        )
    store = _store(request)

    # ---- Single-template start: first message = the prompt with params substituted
    if body.template_id:
        tpl = _templates(request).get_template(body.space_id, body.template_id)
        if tpl is None:
            raise HTTPException(status_code=422, detail="template not found")
        _check_required_params(tpl["params"], body.params)
        session = store.create(
            user.username, model, body.space_id, template_id=tpl["id"]
        )
        # Use the template name as the title directly (deterministic, saves one
        # LLM title generation).
        store.update(session.id, title=tpl["name"][:40], title_generated=1)
        session = store.get(session.id) or session
        _service(request).send_message(
            session, render_prompt(tpl["prompt"], body.params), model
        )
        return {"session": _session_payload(request, store.get(session.id) or session)}

    # ---- Workflow start: snapshot the definition + start the first node
    if body.workflow_template_id:
        tstore = _templates(request)
        wf = tstore.get_workflow(body.space_id, body.workflow_template_id)
        if wf is None:
            raise HTTPException(status_code=422, detail="workflow not found")
        templates_by_id: dict[str, dict] = {}
        for n in wf["nodes"]:
            tpl = tstore.get_template(body.space_id, n["template_id"])
            if tpl is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"workflow references a missing template: {n['template_id']}",
                )
            templates_by_id[tpl["id"]] = tpl
        snapshot = build_workflow_snapshot(wf, templates_by_id)
        first = snapshot["nodes"][0]
        _check_required_params(first["params"], body.params)
        session = store.create(
            user.username, model, body.space_id,
            workflow_json=json.dumps(snapshot, ensure_ascii=False),
        )
        store.update(session.id, title=wf["name"][:40], title_generated=1)
        session = store.get(session.id) or session
        _service(request).start_workflow_node(
            session, 0, node_goal(first, body.params, None), params=body.params
        )
        return {"session": _session_payload(request, store.get(session.id) or session)}

    session = store.create(user.username, model, body.space_id)
    return {"session": _session_payload(request, session)}


@router.get("/{session_id}")
async def get_session(
    session_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
) -> dict:
    session = _own_session(request, session_id, user)
    return {"session": _session_payload(request, session)}


@router.delete("/{session_id}")
async def delete_session(
    session_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
) -> dict:
    session = _own_session(request, session_id, user)
    # Members can see it, but only the creator or a space owner can delete.
    role = _spaces(request).get_member_role(session.space_id, user.username)
    if session.user != user.username and role != ROLE_OWNER:
        raise HTTPException(
            status_code=403,
            detail="only the creator or a space owner can delete a session",
        )
    await _service(request).delete_session(session)
    return {"ok": True}


# ------------------------------------------------------------------- driving
class MessageBody(BaseModel):
    # content may be empty only for an image-only message (checked in the
    # handler — pydantic cannot see the cross-field rule).
    content: str = Field(default="", max_length=32000)
    model: Optional[str] = None
    effort: Optional[str] = None  # OpenAI Responses API reasoning effort (low/medium/high)
    # Workflow sessions: target node task (omitted = the most recently started
    # node); ignored for plain sessions.
    task_id: Optional[str] = None
    # Composer image attachments: [{media_type, data_base64}]. Kept as raw
    # dicts — build_image_blocks (api/image_input.py) owns the whole
    # validate → decode → store pass and maps every violation to a 400
    # (matching the retired app's contract), not a pydantic 422.
    images: Optional[list[dict]] = None


@router.post("/{session_id}/messages", status_code=202)
async def post_message(
    session_id: str,
    body: MessageBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    session = _own_session(request, session_id, user)
    if not body.content.strip() and not body.images:
        raise HTTPException(
            status_code=422, detail="message needs text content or images"
        )
    settings = request.app.state.settings
    from noeta.agent.models_config import get_models, validate_model

    chosen = body.model or session.model
    try:
        validate_model(settings, chosen)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"unknown model: {chosen}")
    # effort: when absent, take the space agent-config default; when passed
    # explicitly it must be in the model's efforts list (422 otherwise); an
    # invalid space default is silently ignored.
    effort = body.effort
    model_def = next((m for m in get_models(settings) if m.id == chosen), None)
    if not effort and session.space_id:
        space_effort = request.app.state.agent_config_store.get(
            session.space_id
        ).get("default_effort")
        if space_effort and model_def is not None and space_effort in model_def.efforts:
            effort = space_effort
    if body.effort:
        if model_def is None or body.effort not in model_def.efforts:
            raise HTTPException(
                status_code=422,
                detail=f"model {chosen} does not support effort: {body.effort}",
            )
    task_id = _resolve_task(request, session, body.task_id)
    # Decode + store the attachments before seeding: a bad attachment is the
    # client's fault → 400, the turn is never seeded. Decode + sqlite write
    # (up to 5MB per image) runs in the thread pool, off the event loop.
    service = _service(request)
    try:
        images = await anyio.to_thread.run_sync(
            build_image_blocks, service, body.images
        )
    except ImageInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        service.send_message(
            session, body.content, chosen, effort=effort, task_id=task_id,
            images=images,
        )
    except SessionBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail="a turn is in progress or a follow-up question is pending"
            if str(exc) != "waiting"
            else "a follow-up question is pending; answer it or stop first",
        )
    return {"status": "started"}


class AnswerBody(BaseModel):
    question_id: str = Field(min_length=1)
    answers: dict = Field(default_factory=dict)
    # Workflow sessions: the node task the question belongs to (omitted = the
    # most recent waiting node).
    task_id: Optional[str] = None


@router.post("/{session_id}/answer", status_code=202)
async def post_answer(
    session_id: str,
    body: AnswerBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    session = _own_session(request, session_id, user)
    task_id = _resolve_task(request, session, body.task_id)
    try:
        _service(request).answer(
            session, body.question_id, body.answers, task_id=task_id
        )
    except SessionBusyError:
        raise HTTPException(status_code=409, detail="no follow-up question is pending")
    return {"status": "answered"}


class CancelBody(BaseModel):
    # Workflow sessions: the node task to stop (omitted = the most recent node).
    task_id: Optional[str] = None


@router.post("/{session_id}/cancel")
async def post_cancel(
    session_id: str,
    request: Request,
    body: Optional[CancelBody] = None,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    session = _own_session(request, session_id, user)
    task_id = _resolve_task(request, session, body.task_id if body else None)
    _service(request).cancel(session, task_id=task_id)
    return {"ok": True}


# ------------------------------------------------------------- workflow advance
class AdvanceConfirmBody(BaseModel):
    node_index: int = Field(ge=1)
    params: dict = Field(default_factory=dict)
    summary: str = Field(default="", max_length=32000)
    handoff_doc: str = Field(default="", max_length=64000)  # full handoff document markdown


def _workflow_or_422(session: Session) -> dict:
    snapshot = session.workflow
    if snapshot is None:
        raise HTTPException(status_code=422, detail="not a workflow session")
    return snapshot


@router.post("/{session_id}/advance/preview")
async def advance_preview(
    session_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
) -> dict:
    """Advance stage one: extract the previous node's full transcript
    (including tool calls) -> standalone handoff generation -> return prefilled
    params + handoff summary + the full handoff document.
    Idempotent and re-entrant: cancel and click again to regenerate."""
    session = _own_session(request, session_id, user)
    snapshot = _workflow_or_422(session)
    store = _store(request)
    tasks = store.list_session_tasks(session.id)
    nxt = next_node_index(snapshot, tasks)
    if nxt is None:
        raise HTTPException(status_code=409, detail="already at the last node")
    if nxt == 0:
        raise HTTPException(
            status_code=409, detail="the workflow has not started its first node"
        )
    prev = next((t for t in tasks if t["node_index"] == nxt - 1), None)
    if prev is None or not prev["task_id"]:
        raise HTTPException(status_code=409, detail="the previous node has not started")
    if prev["status"] == "running":
        raise HTTPException(status_code=409, detail="the previous node is still running")

    service = _service(request)
    # include_tools=True: extract the tool-call summary, feeding the full
    # handoff document generation.
    transcript = await service.task_transcript(prev["task_id"], include_tools=True)
    node = snapshot["nodes"][nxt]
    settings = request.app.state.settings
    sid, model = session.id, session.model
    result = await anyio.to_thread.run_sync(
        lambda: generate_handoff(
            settings, transcript, node.get("prompt", ""),
            node.get("params", []), model, sid,
        )
    )
    return {
        "node_index": nxt,
        "node_name": node.get("name", f"Node {nxt + 1}"),
        "param_defs": node.get("params", []),
        "params": result.params,
        "summary": result.summary,
        "handoff_doc": result.handoff_doc,  # full handoff markdown, for frontend preview
        "degraded": result.degraded,
    }


@router.post("/{session_id}/advance/confirm", status_code=202)
async def advance_confirm(
    session_id: str,
    body: AdvanceConfirmBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Advance stage two: after the user confirms, start the next node's task
    (goal = prompt with params substituted + the handoff summary section + a
    document path reference), and save the handoff document under the
    workspace handoff/ directory. The previous node is not cancelled and is
    unaware of the advance."""
    session = _own_session(request, session_id, user)
    snapshot = _workflow_or_422(session)
    store = _store(request)
    tasks = store.list_session_tasks(session.id)
    nxt = next_node_index(snapshot, tasks)
    if nxt is None or body.node_index != nxt:
        raise HTTPException(
            status_code=409,
            detail="the node has already started or the advance state changed; refresh",
        )
    node = snapshot["nodes"][nxt]
    _check_required_params(node.get("params", []), body.params)
    values = {k: str(v) for k, v in body.params.items() if v is not None}

    # Save the handoff document into the workspace (best-effort).
    service = _service(request)
    handoff_path: Optional[str] = None

    def _write_handoff_doc() -> Optional[str]:
        """Save the handoff document to workspace/handoff/<idx>-<node name>.md;
        returns the relative path."""
        try:
            root = service.session_workspace_path(session.id)
            if not root.exists():
                return None
            handoff_dir = root / "handoff"
            handoff_dir.mkdir(parents=True, exist_ok=True)
            handoff_dir.chmod(0o777)
            safe_name = str(node.get("name", f"node-{nxt + 1}")).replace("/", "-")[:40]
            doc_path = handoff_dir / f"{nxt}-{safe_name}.md"

            # Assemble the document: the user-edited handoff_doc is the body,
            # with params and summary as appendices.
            lines: list[str] = []
            if body.handoff_doc.strip():
                lines.append(body.handoff_doc.strip())
            else:
                lines.append(
                    f"# Handoff: entering node {nxt + 1} - {node.get('name', '')}"
                )
            if values:
                lines.append("")
                lines.append("---")
                lines.append("")
                lines.append("## Params")
                lines.append("")
                for k, v in values.items():
                    lines.append(f"- {k}: {v}")
            if body.summary.strip():
                lines.append("")
                lines.append("## Handoff summary")
                lines.append("")
                lines.append(body.summary.strip())

            doc_path.write_text("\n".join(lines), encoding="utf-8")
            # Return the path relative to the workspace root (so the agent can
            # read it with the read tool).
            return f"handoff/{nxt}-{safe_name}.md"
        except OSError:
            return None

    handoff_path = await anyio.to_thread.run_sync(_write_handoff_doc)
    goal = node_goal(node, values, body.summary, handoff_path)

    service.start_workflow_node(session, nxt, goal, params=values)
    return {
        "status": "started",
        "workflow": workflow_view(snapshot, store.list_session_tasks(session.id)),
        "handoff_path": handoff_path,
    }


# ------------------------------------------------------------------- SSE
def _sse_frame(ev: UIEvent) -> str:
    lines = []
    if ev.seq is not None:
        lines.append(f"id: {ev.seq}")
    lines.append(f"event: {ev.type}")
    lines.append(f"data: {json.dumps(ev.data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


@router.get("/{session_id}/events")
async def session_events(
    session_id: str,
    request: Request,
    since_seq: Optional[int] = Query(default=None),
    task_id: Optional[str] = Query(default=None),
    user: CurrentUser = Depends(get_current_user),
) -> StreamingResponse:
    """SSE event stream. Workflow sessions are per-tab: with task_id, only
    that node task is replayed and live frames are filtered by the `_task`
    tag (untagged session-level frames — workflow_update / session_meta and
    the like — reach every tab)."""
    session = _own_session(request, session_id, user)
    service = _service(request)
    filter_task = _resolve_task(request, session, task_id)
    # Workflow sessions default-lock to the latest node task: each task's seq
    # counts from 0 independently, so a mixed stream would collide on last_seq
    # dedup — the default stream must keep single-task semantics.
    if filter_task is None and session.workflow is not None:
        filter_task = session.task_id

    def _match(ev: UIEvent) -> bool:
        if filter_task is None:
            return True
        tag = ev.data.get("_task") if isinstance(ev.data, dict) else None
        return tag is None or tag == filter_task

    async def stream() -> AsyncIterator[str]:
        # Subscribe before replaying: no live event is missed; the overlap is
        # deduped by seq.
        q = service.subscribe(session.id)
        last_seq = since_seq if since_seq is not None else -1
        try:
            # Emit a comment frame immediately: buffering proxies (Vite dev
            # proxy / reverse proxies) wait for the first body byte before
            # forwarding headers; replay goes through the single-worker queue
            # and can be held up by an active turn for a long time, so the
            # first byte must not wait for it.
            yield ": connected\n\n"
            # Re-fetch the session after subscribing: when attaching mid first
            # turn, task_id may have just been bound; using the stale snapshot
            # from the start of the request would miss the replay (losing the
            # leading events).
            fresh = _store(request).get(session.id) or session
            for ev in await service.replay(fresh, since_seq, task_id=filter_task):
                if ev.seq is not None and ev.seq <= last_seq:
                    continue
                if ev.seq is not None:
                    last_seq = max(last_seq, ev.seq)
                yield _sse_frame(ev)
            # Replay-complete signal: the frontend uses it to end the skeleton
            # state (synthetic event, no id).
            yield _sse_frame(UIEvent(seq=None, type="replay_done", data={}))
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if not _match(ev):
                    continue
                if ev.seq is not None:
                    if ev.seq <= last_seq:
                        continue
                    last_seq = max(last_seq, ev.seq)
                yield _sse_frame(ev)
        finally:
            service.unsubscribe(session.id, q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# Raw trace events (raw-events) moved to the admin console: see
# GET /admin/sessions/{id}/raw-events (gated by require_admin, no longer open
# to regular users).


# ------------------------------------------------------------------- files
# File surface = the session workspace directory on the host
# (workspaces/<session_id>, bind-mounted into the container at /workspace in
# sandbox mode; agent output lands there): list/read go straight to the host
# directory, no longer proxied through the container.
@router.get("/{session_id}/files")
async def list_files(
    session_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
) -> dict:
    session = _own_session(request, session_id, user)
    service = _service(request)
    if not service.sandbox_available:
        return {"files": []}  # sandbox disabled: pure conversation mode, no file surface
    files = await service.sandbox_list_files(session.id)
    return {
        "files": [
            {"path": f.path, "size": f.size, "mtime": f.mtime} for f in files
        ]
    }


@router.get("/{session_id}/files/content")
async def read_file(
    session_id: str,
    request: Request,
    path: str = Query(min_length=1),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    from noeta.agent.host.workspace_files import resolve_within

    session = _own_session(request, session_id, user)
    service = _service(request)
    if not service.sandbox_available:
        raise HTTPException(status_code=404, detail="file not found")
    if resolve_within(service.session_workspace_path(session.id), path) is None:
        raise HTTPException(status_code=400, detail="invalid path")
    content = await service.sandbox_read_file(session.id, path)
    if content is None:
        raise HTTPException(status_code=404, detail="file not found")
    truncated = len(content) > _FILE_CLIP_BYTES
    if truncated:
        content = content[:_FILE_CLIP_BYTES]
    # mtime comes from the listing endpoint (read carries no metadata); default
    # to 0 when unavailable (the frontend does not render mtime).
    mtime = 0.0
    listing = await service.sandbox_list_files(session.id)
    mtime = next((f.mtime for f in listing if f.path == path), 0.0)
    return {"path": path, "content": content, "truncated": truncated, "mtime": mtime}


@router.get("/{session_id}/preview")
async def sandbox_preview(
    session_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
) -> dict:
    """Sandbox live-preview discovery endpoint: ``{token, port, panels}``.

    The frontend uses it to build the panel iframes at
    ``http://<same hostname>:<port>/sandbox-preview/<token>/<sub>`` (a separate
    origin, see sandbox_preview.py). If the session has no sandbox container
    (disabled / not yet allocated / already released) -> 404 and the frontend
    hides the preview panel.
    """
    session = _own_session(request, session_id, user)
    service = _service(request)
    # May trigger a docker lookup (lazy-mount fallback); run in the thread pool
    # to keep it off the event loop.
    info = await anyio.to_thread.run_sync(
        service.sandbox_preview_info, session.id
    )
    if info is None:
        raise HTTPException(status_code=404, detail="this session has no sandbox preview")
    return info
