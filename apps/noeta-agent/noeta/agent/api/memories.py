"""Space-memory management API: viewing / editing / archiving / deleting the
agent's long-term memories (a pool of markdown files).

Storage is the noeta MemoryStore directory (DATA_DIR/memories/<space_id>/,
see service.py _memory_root_for_task) — this module opens a MemoryStore on
the same directory for reads and writes, without going through an agent
session.

Permission model (differences from knowledge.py, and why):
- Space members can read AND edit / archive: memories are written by any
  member's session through the agent anyway, so "a member's session may write
  but the member personally may not edit" does not hold — editing is open to
  all members.
- Physical deletion is owner-only: archiving is the routine way to retire a
  memory (traceable; the agent uses it too); true deletion is irreversible,
  so it is restricted to the owner.
- Non-members -> 404 (hiding existence, following the spaces.py pattern).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from noeta.agent.auth.deps import CurrentUser, get_current_user
from noeta.agent.store.spaces import ROLE_OWNER, SpaceStore

router = APIRouter(prefix="/spaces/{space_id}/memories", tags=["memories"])


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


def _memory_store(request: Request, space_id: str):
    """The MemoryStore for this space's memory pool (the directory may not
    exist yet — a legitimate empty pool)."""
    from noeta.sdk import MemoryStore

    settings = request.app.state.settings
    return MemoryStore(settings.memories_path / space_id)


class WriteMemoryBody(BaseModel):
    """Full-text overwrite. When text carries frontmatter it lands verbatim
    (the same semantics as the memory_write tool)."""

    text: str = Field(min_length=1, max_length=64_000)


@router.get("")
async def list_memories(
    space_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _membership_or_404(request, space_id, user)
    store = _memory_store(request, space_id)
    memories = []
    for name, summary, mem_type in store.entries():
        item: dict = {"name": name, "description": summary, "type": mem_type}
        try:
            item["updated_at"] = store.path_for(name).stat().st_mtime
        except OSError:
            item["updated_at"] = None
        memories.append(item)
    return {"memories": memories}


@router.get("/{name}")
async def get_memory(
    space_id: str,
    name: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _membership_or_404(request, space_id, user)
    text = _memory_store(request, space_id).read(name)
    if text is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return {"name": name, "text": text}


@router.put("/{name}")
async def write_memory(
    space_id: str,
    name: str,
    body: WriteMemoryBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _membership_or_404(request, space_id, user)
    store = _memory_store(request, space_id)
    try:
        store.write(name, body.text)
    except ValueError:
        # MemoryStore's slug validation (kebab-case); an invalid name never
        # lands on disk.
        raise HTTPException(
            status_code=422, detail="memory names must be kebab-case slugs"
        )
    return {"name": name, "ok": True}


@router.post("/{name}/archive")
async def archive_memory(
    space_id: str,
    name: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _membership_or_404(request, space_id, user)
    dest = _memory_store(request, space_id).archive(name)
    if dest is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return {"name": name, "ok": True}


@router.delete("/{name}")
async def delete_memory(
    space_id: str,
    name: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _, role = _membership_or_404(request, space_id, user)
    if role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail="space owner permission required")
    store = _memory_store(request, space_id)
    try:
        path = store.path_for(name)
    except ValueError:
        raise HTTPException(status_code=404, detail="memory not found")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="memory not found")
    path.unlink()
    return {"name": name, "ok": True}
