"""image_input — decode composer image attachments into ``ImageBlock``s.

The message endpoint's image-ingestion side: a ``POST /sessions/{id}/messages``
body may carry ``images: [{media_type, data_base64}]``. This module collapses
validate → decode → content-store write into one pass:

* whitelist the MIME type (PNG / JPEG / GIF / WebP — matching the frontend
  ``apps/web/src/lib/imageAttach.ts`` ``ALLOWED_IMAGE_TYPES``);
* ``base64.b64decode(..., validate=True)`` — reject illegal input with a clear
  error, never a silent swallow;
* cap a single image at 5MB (matching the frontend ``MAX_IMAGE_BYTES``) so
  base64 bloat can't blow up the request body;
* store the decoded bytes via ``AgentService.put_content`` (a noeta.sdk
  passthrough to the content-addressed store) and wrap the returned
  ``ContentRef`` in an ``ImageBlock``.

Per the import-linter ``app-uses-only-sdk`` contract this module imports
``ImageBlock`` from ``noeta.sdk`` only — never ``noeta.protocols``. A
validation failure raises :class:`ImageInputError`; the sessions endpoint maps
it to an HTTP 400 (the backend never trusts the client).
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from noeta.sdk import ImageBlock

# Allowed image MIME types (compared lowercase), matching the frontend
# ``imageAttach.ts`` ``ALLOWED_IMAGE_TYPES``.
ALLOWED_IMAGE_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)

# Single-image size cap (bytes), matching the frontend ``MAX_IMAGE_BYTES`` (5MB) —
# stop base64 bloat from blowing up the request body.
MAX_IMAGE_BYTES = 5 * 1024 * 1024


class ImageInputError(ValueError):
    """A composer image attachment failed validation / decoding.

    Raised distinctly (not a bare ``ValueError``) so the sessions endpoint can
    map it to an HTTP 400 and return early — the turn is never seeded when the
    client sends a bad attachment.
    """


def build_image_blocks(service: Any, raw_images: Any) -> list[ImageBlock]:
    """Decode validated ``{media_type, data_base64}`` entries → ``list[ImageBlock]``.

    ``raw_images`` is the request body's ``images`` field. ``None`` / empty →
    ``[]`` (byte-identical to the text-only path). Each entry must be a dict
    with a whitelisted ``media_type`` (normalized lowercase / stripped) and
    legal base64 ``data_base64`` decoding to at most :data:`MAX_IMAGE_BYTES`;
    the bytes are stored via ``service.put_content`` and wrapped in an
    ``ImageBlock(source=ref)``. Any violation raises :class:`ImageInputError`.
    """
    if not raw_images:
        return []
    if not isinstance(raw_images, list):
        raise ImageInputError("'images' must be a list when present")
    blocks: list[ImageBlock] = []
    for item in raw_images:
        if not isinstance(item, dict):
            raise ImageInputError("each 'images' entry must be an object")
        media_type = item.get("media_type")
        data_base64 = item.get("data_base64")
        if not isinstance(media_type, str) or not media_type:
            raise ImageInputError(
                "image 'media_type' is required and must be a non-empty string"
            )
        normalized = media_type.strip().lower()
        if normalized not in ALLOWED_IMAGE_TYPES:
            raise ImageInputError(
                f"image 'media_type' {media_type!r} is not a supported image type; "
                f"allowed: {sorted(ALLOWED_IMAGE_TYPES)}"
            )
        if not isinstance(data_base64, str) or not data_base64:
            raise ImageInputError(
                "image 'data_base64' is required and must be a non-empty string"
            )
        try:
            # validate=True: reject non-base64 chars with a clear error.
            body = base64.b64decode(data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageInputError("image 'data_base64' is not valid base64") from exc
        if len(body) > MAX_IMAGE_BYTES:
            raise ImageInputError(
                f"image is {len(body)} bytes, over the "
                f"{MAX_IMAGE_BYTES}-byte (5MB) limit; please compress it"
            )
        ref = service.put_content(body, media_type=normalized)
        blocks.append(ImageBlock(source=ref))
    return blocks
