"""Space endpoints: CRUD, member management, user search.

Permission model:
- Only members can see a space (non-member GET /spaces/{id} -> 404, hiding
  existence).
- Only owners can modify a space / add or remove members (member but not
  owner -> 403).
- A personal space cannot be renamed / given members / deleted (-> 400).
- The last owner cannot be removed or demoted (-> 400).
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from noeta.agent.auth.deps import CurrentUser, get_current_user
from noeta.agent.store.spaces import (
    ROLE_MEMBER,
    ROLE_OWNER,
    VALID_ROLES,
    LastOwnerError,
    PersonalSpaceError,
    SpaceStore,
)
from noeta.agent.store.users import UserStore

router = APIRouter(prefix="/spaces", tags=["spaces"])
users_router = APIRouter(prefix="/users", tags=["users"])


def _space_store(request: Request) -> SpaceStore:
    return request.app.state.space_store


def _user_store(request: Request) -> UserStore:
    return request.app.state.user_store


# --------------------------------------------------------- permission helpers
def require_space_member(request: Request, space_id: str, user: CurrentUser) -> str:
    """For collection-style endpoints (GET/POST /sessions?space_id=): space
    missing -> 404, exists but not a member -> 403; returns the role."""
    store = _space_store(request)
    if store.get_space(space_id) is None:
        raise HTTPException(status_code=404, detail="space not found")
    role = store.get_member_role(space_id, user.username)
    if role is None:
        raise HTTPException(status_code=403, detail="no access to this space")
    return role


def _membership_or_404(
    request: Request, space_id: str, user: CurrentUser
) -> tuple[dict, str]:
    """For detail/mutation endpoints: space missing or not a member is always
    404 (hiding existence)."""
    store = _space_store(request)
    space = store.get_space(space_id)
    role = store.get_member_role(space_id, user.username) if space else None
    if space is None or role is None:
        raise HTTPException(status_code=404, detail="space not found")
    return space, role


def _require_owner(role: str) -> None:
    if role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail="space owner permission required")


# ---------------------------------------------------------------- spaces
class CreateSpaceBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)


class UpdateSpaceBody(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=500)


@router.get("")
async def list_spaces(
    request: Request, user: CurrentUser = Depends(get_current_user)
) -> dict:
    return {"spaces": _space_store(request).list_spaces_for_user(user.username)}


@router.post("", status_code=201)
async def create_space(
    body: CreateSpaceBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    space_id = uuid.uuid4().hex
    space = _space_store(request).create_space(
        space_id, body.name.strip(), body.description, False, user.username
    )
    return {"space": {**space, "my_role": ROLE_OWNER, "member_count": 1}}


@router.get("/{space_id}")
async def get_space(
    space_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
) -> dict:
    space, role = _membership_or_404(request, space_id, user)
    members = _space_store(request).list_members(space_id)
    return {"space": {**space, "my_role": role, "members": members}}


@router.patch("/{space_id}")
async def update_space(
    space_id: str,
    body: UpdateSpaceBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)
    try:
        updated = _space_store(request).update_space(
            space_id, name=body.name, description=body.description
        )
    except PersonalSpaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if updated is None:  # deleted concurrently between validation and update
        raise HTTPException(status_code=404, detail="space not found")
    return {"space": {**updated, "my_role": role}}


@router.delete("/{space_id}")
async def delete_space(
    space_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
) -> dict:
    space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)
    if space["is_personal"]:
        raise HTTPException(status_code=400, detail="a personal space cannot be deleted")
    # Cascade session cleanup: go through agent_service for full deletion
    # (noeta task tree + sandbox + workspace). Deleting only the metadata rows
    # would leak state on the noeta side.
    service = request.app.state.agent_service
    for session in request.app.state.session_store.list_for_space(space_id):
        await service.delete_session(session)
    _space_store(request).delete_space(space_id)
    return {"ok": True}


# ---------------------------------------------------------------- members
class AddMemberBody(BaseModel):
    username: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None


class UpdateRoleBody(BaseModel):
    role: str


def _resolve_username(body: AddMemberBody) -> str:
    """username wins; otherwise the local part of the email (before the @)."""
    if body.username and body.username.strip():
        return body.username.strip()
    if body.email and body.email.strip():
        return body.email.strip().split("@", 1)[0]
    raise HTTPException(status_code=422, detail="username or email required")


@router.post("/{space_id}/members", status_code=201)
async def add_member(
    space_id: str,
    body: AddMemberBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)
    if space["is_personal"]:
        raise HTTPException(
            status_code=400, detail="cannot add members to a personal space"
        )
    new_role = body.role or ROLE_MEMBER
    if new_role not in VALID_ROLES:
        raise HTTPException(status_code=422, detail=f"invalid role: {new_role}")
    username = _resolve_username(body)
    # The invitee may never have logged in: create a placeholder row in the
    # users table first (never overwriting an existing profile). For email
    # invites also store the email; when that user later logs in under the same
    # username (the email local part) the profiles merge naturally.
    email = body.email.strip() if body.email and body.email.strip() else None
    _user_store(request).ensure_user(username, email=email)
    _space_store(request).add_member(space_id, username, new_role, user.username)
    return {"members": _space_store(request).list_members(space_id)}


@router.patch("/{space_id}/members/{member}")
async def update_member_role(
    space_id: str,
    member: str,
    body: UpdateRoleBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=422, detail=f"invalid role: {body.role}")
    try:
        _space_store(request).update_member_role(space_id, member, body.role)
    except LastOwnerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"members": _space_store(request).list_members(space_id)}


@router.delete("/{space_id}/members/{member}")
async def remove_member(
    space_id: str,
    member: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)
    try:
        _space_store(request).remove_member(space_id, member)
    except LastOwnerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"members": _space_store(request).list_members(space_id)}


# ----------------------------------------------------------------- search
@users_router.get("/search")
async def search_users(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=50),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Member search, backed by the local users table."""
    query = q.strip()
    local = _user_store(request).search_users(query, limit)
    return {"users": [u.to_api() for u in local]}
