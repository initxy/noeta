"""Knowledge API: space knowledge-source CRUD + sync trigger/status.

Permission model (following the membership-check pattern in spaces.py):
- Space members can read (GET).
- Only the space owner can manage (POST/PATCH/DELETE/POST sync).
- Non-members accessing a space's knowledge sources -> 404 (hiding existence).
"""
from __future__ import annotations

import uuid
from functools import partial
from typing import Any, Optional

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from noeta.agent.auth.deps import CurrentUser, get_current_user
from noeta.agent.services import knowledge_resolve
from noeta.agent.store.knowledge import KnowledgeSourceStore
from noeta.agent.store.spaces import ROLE_OWNER, SpaceStore

router = APIRouter(prefix="/spaces/{space_id}/knowledge", tags=["knowledge"])


def _knowledge_store(request: Request) -> KnowledgeSourceStore:
    return request.app.state.knowledge_store


def _sync_manager(request: Request):
    return request.app.state.knowledge_sync_manager


def _space_store(request: Request) -> SpaceStore:
    return request.app.state.space_store


def _membership_or_404(
    request: Request, space_id: str, user: CurrentUser
) -> tuple[dict, str]:
    """Space missing or not a member -> 404 (hiding existence)."""
    store = _space_store(request)
    space = store.get_space(space_id)
    role = store.get_member_role(space_id, user.username) if space else None
    if space is None or role is None:
        raise HTTPException(status_code=404, detail="space not found")
    return space, role


def _require_owner(role: str) -> None:
    if role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail="space owner permission required")


def _validate_config(source_type: str, config: dict) -> None:
    """Validate the required config fields per source type."""
    if source_type == "git_repo":
        if not config.get("url"):
            raise HTTPException(status_code=422, detail="git_repo source requires url")
    elif source_type == "local_dir":
        if not config.get("path"):
            raise HTTPException(status_code=422, detail="local_dir source requires path")


# ---------------------------------------------------------------- models

class CreateSourceBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    type: str
    config: dict[str, Any] = Field(default_factory=dict)


class UpdateSourceBody(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    config: Optional[dict[str, Any]] = None


class ResolvePathsBody(BaseModel):
    """resolve-paths input: a list of knowledge/<source name>/<path>[#<heading anchor>]."""

    paths: list[str] = Field(
        min_length=1, max_length=knowledge_resolve.MAX_PATHS
    )


# ----------------------------------------------------------------- CRUD

@router.get("")
async def list_sources(
    space_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _membership_or_404(request, space_id, user)
    sources = _knowledge_store(request).list_sources(space_id)
    # doc_count / failed_count: written back into the config cache when a sync
    # completes (so list does not read the manifest / report on the fly).
    # failed_count = the export-failure count of the most recent sync, feeding
    # the "N docs not synced" badge.
    for s in sources:
        s["doc_count"] = s.get("config", {}).get("doc_count")
        s["failed_count"] = s.get("config", {}).get("failed_count")
    return {"sources": sources}


@router.post("", status_code=201)
async def create_source(
    space_id: str,
    body: CreateSourceBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)

    source_type = body.type.strip()
    _validate_config(source_type, body.config)

    source_id = uuid.uuid4().hex
    try:
        source = _knowledge_store(request).create_source(
            source_id=source_id,
            space_id=space_id,
            name=body.name.strip(),
            source_type=source_type,
            config=body.config,
            created_by=user.username,
        )
    except ValueError as exc:
        # name conflict
        raise HTTPException(status_code=409, detail=str(exc))
    return {"source": source}


@router.patch("/{source_id}")
async def update_source(
    space_id: str,
    source_id: str,
    body: UpdateSourceBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)

    store = _knowledge_store(request)
    source = store.get_source(source_id)
    if source is None or source["space_id"] != space_id:
        raise HTTPException(status_code=404, detail="knowledge source not found")

    # If the config changed and the type is known, re-validate.
    if body.config is not None:
        _validate_config(source["type"], body.config)

    old_name = source["name"]
    try:
        updated = store.update_source(
            source_id,
            name=body.name.strip() if body.name else None,
            config=body.config,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if updated is None:
        raise HTTPException(status_code=404, detail="knowledge source not found")
    # Name changed -> migrate the display name symlink (drop old, create new).
    if updated["name"] != old_name:
        _sync_manager(request).rename_source_symlink(source, old_name, updated["name"])
    return {"source": updated}


@router.delete("/{source_id}")
async def delete_source(
    space_id: str,
    source_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)

    store = _knowledge_store(request)
    source = store.get_source(source_id)
    if source is None or source["space_id"] != space_id:
        raise HTTPException(status_code=404, detail="knowledge source not found")
    # Clean up the materialized directory + the name symlink.
    _sync_manager(request).delete_source_files(source)
    store.delete_source(source_id)
    return {"ok": True}


# ----------------------------------------------------------------- sync

@router.post("/{source_id}/sync", status_code=202)
async def trigger_sync(
    space_id: str,
    source_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)

    store = _knowledge_store(request)
    source = store.get_source(source_id)
    if source is None or source["space_id"] != space_id:
        raise HTTPException(status_code=404, detail="knowledge source not found")

    # Run the sync on a background thread.
    try:
        updated = _sync_manager(request).start_sync(source_id, triggered_by=user.username)
    except ValueError as e:
        if "syncing" in str(e):
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=404, detail=str(e))
    return {"source": updated}


@router.get("/{source_id}/sync")
async def get_sync_status(
    space_id: str,
    source_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _membership_or_404(request, space_id, user)

    store = _knowledge_store(request)
    source = store.get_source(source_id)
    if source is None or source["space_id"] != space_id:
        raise HTTPException(status_code=404, detail="knowledge source not found")
    # Attach progress while syncing (in-memory state; not syncing / no record
    # -> null).
    progress = None
    if source["status"] == "syncing":
        progress = _sync_manager(request).get_progress(source_id)
    return {
        "status": source["status"],
        "last_sync_at": source["last_sync_at"],
        "last_error": source["last_error"],
        "progress": progress,
        "report": None,
    }


# ---------------------------------------------------------------- resolve

@router.post("/resolve-paths")
async def resolve_paths(
    space_id: str,
    body: ResolvePathsBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Batch-resolve knowledge reference paths that appear in sessions
    (citations; readable by members).

    The frontend collects knowledge/ paths from tool_call output / body
    footnotes and calls this endpoint to fill in title / origin link /
    excerpt; exists=False is handled by frontend degradation. Malformed path
    shapes (no knowledge/ prefix, directory traversal) -> 422 for the whole
    batch.
    """
    _membership_or_404(request, space_id, user)
    settings = request.app.state.settings
    store = _knowledge_store(request)
    try:
        # Per-entry file IO (frontmatter + anchor lookup); run in the thread
        # pool to keep the event loop unblocked.
        items = await anyio.to_thread.run_sync(
            partial(
                knowledge_resolve.resolve_paths,
                space_id=space_id,
                raw_paths=body.paths,
                knowledge_root=settings.knowledge_path,
                get_source_by_name=store.get_source_by_name,
                get_source_by_id=store.get_source,
            )
        )
    except knowledge_resolve.InvalidPathError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"items": items}
