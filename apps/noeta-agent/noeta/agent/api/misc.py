"""Health check / model list / ContentStore content endpoint."""
from __future__ import annotations

import string

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from noeta.agent.auth.deps import CurrentUser, get_current_user

router = APIRouter(tags=["misc"])

_HEX_CHARS = set(string.hexdigits.lower())

# Magic-byte sniff for the binary content types the UI renders inline (the
# composer image whitelist + PDF); anything else stays octet-stream. The noeta
# ContentStore has no metadata read interface, so the media type is recovered
# from the bytes themselves (same policy as the retired app's /content route).
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)


def _sniff_media_type(body: bytes) -> str:
    for magic, mt in _MAGIC:
        if body.startswith(magic):
            return mt
    if len(body) >= 12 and body[0:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


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
    user: CurrentUser = Depends(get_current_user),
) -> Response:
    """Raw ContentStore bytes by content hash.

    Two consumers: the admin Trace view (useContentBody) dereferencing
    ContentRefs, and the chat user bubble rendering composer image
    attachments back (``<img src="/api/v1/content/{hash}">``) — which is why
    the gate is any authenticated user, not require_admin: a regular member
    must be able to load the images of their own conversation. Content is
    addressed by unguessable SHA-256 hash (a capability: you can only ask for
    bytes you have already seen a ref to). Content-Type is sniffed from the
    magic bytes for inline-renderable types (the noeta ContentStore has no
    metadata read interface); everything else is octet-stream and the caller
    interprets it by the ContentRef.media_type it holds.
    """
    if len(content_hash) != 64 or not set(content_hash) <= _HEX_CHARS:
        raise HTTPException(status_code=404, detail="content not found")
    body = await request.app.state.agent_service.get_content_by_hash(content_hash)
    if body is None:
        raise HTTPException(status_code=404, detail="content not found")
    return Response(content=body, media_type=_sniff_media_type(body))
