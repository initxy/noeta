"""Health check / model list / ContentStore content endpoint."""
from __future__ import annotations

import string

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from noeta.agent.auth.deps import CurrentUser, get_current_user, require_admin

router = APIRouter(tags=["misc"])

_HEX_CHARS = set(string.hexdigits.lower())


@router.get("/health")
async def health(request: Request) -> dict:
    return {"ok": True, "provider": request.app.state.agent_service.provider_name}


@router.get("/models")
async def models(
    request: Request, user: CurrentUser = Depends(get_current_user)
) -> dict:
    settings = request.app.state.settings
    from noeta.agent.models_config import get_models

    return {
        "models": [m.to_api() for m in get_models(settings)],
        "provider": settings.effective_provider,
    }


@router.get("/capabilities")
async def capabilities(
    request: Request, user: CurrentUser = Depends(get_current_user)
) -> dict:
    """Snapshot of the current agent capability switches, for optional frontend
    queries / future integration (the frontend does not depend on it today)."""
    return {"capabilities": request.app.state.agent_service.capabilities}


@router.get("/content/{content_hash}")
async def content_by_hash(
    content_hash: str,
    request: Request,
    admin: CurrentUser = Depends(require_admin),
) -> Response:
    """Raw ContentStore bytes (the Trace page dereferences ContentRefs).

    The only consumer is the admin Trace view (useContentBody); since Trace moved
    into the admin console this endpoint is gated by require_admin (non-admins get
    404). Content is fetched by unguessable content hash (SHA-256). Content-Type
    is fixed to octet-stream: the noeta ContentStore has no metadata read
    interface, so media_type is decided by the ContentRef.media_type the frontend
    holds.
    """
    if len(content_hash) != 64 or not set(content_hash) <= _HEX_CHARS:
        raise HTTPException(status_code=404, detail="content not found")
    body = await request.app.state.agent_service.get_content_by_hash(content_hash)
    if body is None:
        raise HTTPException(status_code=404, detail="content not found")
    return Response(content=body, media_type="application/octet-stream")
