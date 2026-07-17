"""Admin console endpoints (prefix /admin, all gated by require_admin).

Deep-module orientation: one router collects every admin endpoint; the data
comes from narrow query methods on the stores plus existing assembly functions
(bypassing space membership checks — only admin is checked). Everything is
read-only except the builtin-skill write operations (which reuse the
consolidated /skills) and dynamic-config writes. Non-admins uniformly get 404
via require_admin.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from noeta.agent.api.space_skills import assemble_space_skills
from noeta.agent.auth.deps import CurrentUser, require_admin
from noeta.agent.config import Settings
from noeta.agent.config_registry import (
    CONFIG_REGISTRY,
    coerce_config,
    list_config,
)
from noeta.agent.store.knowledge import KnowledgeSourceStore
from noeta.agent.store.sessions import SessionStore
from noeta.agent.store.spaces import SpaceStore
from noeta.agent.store.users import UserStore

router = APIRouter(prefix="/admin", tags=["admin"])


# --------------------------------------------------------------- store access
def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _users(request: Request) -> UserStore:
    return request.app.state.user_store


def _sessions(request: Request) -> SessionStore:
    return request.app.state.session_store


def _spaces(request: Request) -> SpaceStore:
    return request.app.state.space_store


def _knowledge(request: Request) -> KnowledgeSourceStore:
    return request.app.state.knowledge_store


# ---------------------------------------------------------------- overview
@router.get("/stats")
async def stats(
    request: Request, admin: CurrentUser = Depends(require_admin)
) -> dict:
    """Platform entity-count overview."""
    session_by_status = _sessions(request).count_by_status()
    knowledge_by_status = _knowledge(request).count_by_status()
    skill_store = request.app.state.skill_store
    return {
        "users": _users(request).count_users(),
        "spaces": _spaces(request).count_spaces(),
        "sessions": {
            "total": sum(session_by_status.values()),
            "by_status": session_by_status,
        },
        "knowledge_sources": {
            "total": sum(knowledge_by_status.values()),
            "by_status": knowledge_by_status,
        },
        # Builtin = rows with space_id="*" in the skills table; space skills =
        # real-space rows (same table).
        "builtin_skills": skill_store.count_builtin(),
        "space_skills": skill_store.count_space(),
    }


# ---------------------------------------------------------------- users
@router.get("/users")
async def list_users(
    request: Request,
    q: str = Query(default=""),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    admin: CurrentUser = Depends(require_admin),
) -> dict:
    store = _users(request)
    q = q.strip()
    users = store.list_users(offset, limit, q)
    return {
        "users": [
            {**u.to_api(), "created_at": u.created_at, "updated_at": u.updated_at}
            for u in users
        ],
        "total": store.count_users(q),
        "offset": offset,
        "limit": limit,
    }


# ---------------------------------------------------------------- sessions
@router.get("/sessions")
async def list_sessions(
    request: Request,
    user: Optional[str] = Query(default=None),
    space_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    admin: CurrentUser = Depends(require_admin),
) -> dict:
    store = _sessions(request)
    space_store = _spaces(request)
    sessions = store.list_all(offset, limit, user=user, space_id=space_id, status=status)
    # Resolve every space name this page touches up front (the page size is
    # bounded, so per-space get_space calls are acceptable).
    space_names: dict[str, Optional[str]] = {}
    for s in sessions:
        if s.space_id not in space_names:
            space = space_store.get_space(s.space_id)
            space_names[s.space_id] = space["name"] if space else None
    return {
        "sessions": [
            {
                **s.to_api(),
                "user": s.user,
                "space_name": space_names.get(s.space_id),
            }
            for s in sessions
        ],
        "total": store.count_all(user=user, space_id=space_id, status=status),
        "offset": offset,
        "limit": limit,
    }


@router.get("/sessions/{session_id}/raw-events")
async def raw_events(
    session_id: str,
    request: Request,
    cursor: Optional[str] = Query(default=None),
    admin: CurrentUser = Depends(require_admin),
) -> dict:
    """Raw noeta envelopes for a session (Trace page; root + full subtask tree).

    cursor is the {task_id: last_seq} JSON echoed back by the previous response
    (each task stream counts seq independently, so a single since_seq cannot
    express subtree progress); omitted = full replay.
    Moved here from the regular user endpoints: no space-membership check, only
    the admin gate.
    """
    session = _sessions(request).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    parsed: Optional[dict[str, int]] = None
    if cursor:
        try:
            parsed = {str(k): int(v) for k, v in json.loads(cursor).items()}
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=422, detail="invalid cursor")
    return await request.app.state.agent_service.raw_events(session, parsed)


# ---------------------------------------------------------------- spaces + drilldown
@router.get("/spaces")
async def list_spaces(
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    admin: CurrentUser = Depends(require_admin),
) -> dict:
    store = _spaces(request)
    return {
        "spaces": store.list_all_spaces(offset, limit),
        "total": store.count_spaces(),
        "offset": offset,
        "limit": limit,
    }


def _space_or_404(request: Request, space_id: str) -> dict:
    space = _spaces(request).get_space(space_id)
    if space is None:
        raise HTTPException(status_code=404, detail="space not found")
    return space


@router.get("/spaces/{space_id}/members")
async def space_members(
    space_id: str, request: Request, admin: CurrentUser = Depends(require_admin)
) -> dict:
    _space_or_404(request, space_id)
    return {"members": _spaces(request).list_members(space_id)}


@router.get("/spaces/{space_id}/knowledge")
async def space_knowledge(
    space_id: str, request: Request, admin: CurrentUser = Depends(require_admin)
) -> dict:
    _space_or_404(request, space_id)
    return {"sources": _knowledge(request).list_sources(space_id)}


@router.get("/spaces/{space_id}/skills")
async def space_skills(
    space_id: str, request: Request, admin: CurrentUser = Depends(require_admin)
) -> dict:
    """Space skills (builtin union space uploads, with enabled/group state;
    read-only).

    Reuses the assembly function from space_skills (builtin rows + space-skill
    registry as the authority), only bypassing the space membership check in
    favor of the admin gate.
    """
    _space_or_404(request, space_id)
    return {"skills": assemble_space_skills(request, space_id)}


# ---------------------------------------------------------------- dynamic config
class ConfigPutBody(BaseModel):
    value: object


def _config_store(request: Request):
    return request.app.state.app_config_store


@router.get("/config")
async def get_config(
    request: Request, admin: CurrentUser = Depends(require_admin)
) -> dict:
    return {"items": list_config(_config_store(request), _settings(request))}


@router.put("/config/{key}")
async def put_config(
    key: str,
    body: ConfigPutBody,
    request: Request,
    admin: CurrentUser = Depends(require_admin),
) -> dict:
    if key not in CONFIG_REGISTRY:
        raise HTTPException(status_code=404, detail="unknown config key")
    try:
        value = coerce_config(key, body.value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid config value: {exc}")
    _config_store(request).set(key, value, admin.username)
    items = list_config(_config_store(request), _settings(request))
    item = next((i for i in items if i["key"] == key), None)
    return {"item": item}
