"""A ``tools/call`` result as the model sees it (0.6's shape).

The body is the JSON object 0.6 sent: ``{"text": ...}``, plus
``non_text_blocks`` when any block was not plain text and
``structured_content`` when the server returned it. Text blocks and embedded
text resources join the text in order; anything the model cannot see inline
(audio, a resource link, a binary resource, an unusable image) becomes a
one-line description; a displayable image travels as an image block. A text
over 1 MiB is cut to a marked head (the full text is saved under the spill
directory when there is one), and a large ``structuredContent`` is saved the
same way instead of inlined. ``isError: true`` is an error result.
"""

from __future__ import annotations

import base64
import binascii
import json
import posixpath
from typing import Any

from noeta.core.tool import ToolContext, ToolOutput
from noeta.core.tool_helpers import spill_path, truncate_bytes
from noeta.core.types import Image, Text

__all__ = ["IMAGE_MAX_BYTES", "INLINE_MAX_BYTES", "render_result"]

INLINE_MAX_BYTES = 1024 * 1024
#: The inline share of an over-cap result: half the ceiling.
_RESULT_INLINE_MAX_BYTES = INLINE_MAX_BYTES // 2
_INLINE_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
IMAGE_MAX_BYTES = 5 * 1024 * 1024


def _decode_b64(data: object) -> bytes | None:
    if not isinstance(data, str):
        return None
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None


def _describe_block(block: object) -> tuple[str, Image | None]:
    if not isinstance(block, dict):
        return "[unrecognised content block]", None
    kind = block.get("type")
    mime = block.get("mimeType")
    mime_s = mime if isinstance(mime, str) and mime else "unknown type"
    if kind == "image":
        raw = _decode_b64(block.get("data"))
        if raw is None:
            return f"[image ({mime_s}): data is not valid base64, not shown]", None
        if mime not in _INLINE_IMAGE_TYPES:
            return f"[image ({mime_s}, {len(raw)} bytes): type not shown inline]", None
        if len(raw) > IMAGE_MAX_BYTES:
            return (f"[image ({mime_s}, {len(raw)} bytes): over the "
                    f"{IMAGE_MAX_BYTES} byte inline limit, not shown]"), None
        return "", Image(media_type=str(mime), data=raw)
    if kind == "audio":
        raw = _decode_b64(block.get("data"))
        size = f", {len(raw)} bytes" if raw is not None else ""
        return f"[audio ({mime_s}{size}): not shown]", None
    if kind == "resource_link":
        uri, name, desc = block.get("uri"), block.get("name"), block.get("description")
        label = f"{name} " if isinstance(name, str) and name else ""
        tail = f" — {desc}" if isinstance(desc, str) and desc else ""
        return f"[resource link: {label}<{uri}> ({mime_s}){tail}]", None
    if kind == "resource":
        res = block.get("resource")
        if isinstance(res, dict):
            text = res.get("text")
            if isinstance(text, str):
                return text, None
            rmime = res.get("mimeType")
            rmime_s = rmime if isinstance(rmime, str) and rmime else "unknown type"
            return f"[embedded resource <{res.get('uri')}> ({rmime_s}): binary, not shown]", None
        return "[embedded resource: malformed, not shown]", None
    return f"[{kind!s} content block: not shown]", None


async def _save(ctx: ToolContext, suffix: str, data: bytes) -> str | None:
    if not ctx.spill_dir:
        return None
    base = spill_path(ctx.spill_dir, ctx.task_id, ctx.call_id)
    path = posixpath.splitext(base)[0] + suffix
    try:
        await ctx.env.write_bytes(path, data)
    except OSError:
        return None
    return path


async def render_result(result: dict[str, Any], ctx: ToolContext) -> ToolOutput:
    is_error = bool(result.get("isError"))
    content = result.get("content")
    parts: list[str] = []
    images: list[Image] = []
    non_text = 0
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
                continue
            non_text += 1
            line, image = _describe_block(block)
            if image is not None:
                images.append(image)
            if line:
                parts.append(line)
    text = "\n".join(parts)
    output: dict[str, Any] = {"text": text}
    if non_text:
        output["non_text_blocks"] = non_text
    structured = result.get("structuredContent")
    if structured is not None:
        encoded_s = json.dumps(structured, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(encoded_s) > _RESULT_INLINE_MAX_BYTES:
            output["structured_content_bytes"] = len(encoded_s)
            saved = await _save(ctx, ".structured.json", encoded_s)
            if saved:
                output["structured_content_saved_to"] = saved
        else:
            output["structured_content"] = structured
    encoded = text.encode("utf-8")
    if len(encoded) > INLINE_MAX_BYTES:
        # The marker leads: a later trim cuts from the end.
        head = truncate_bytes(text, _RESULT_INLINE_MAX_BYTES)
        output["text"] = (f"[truncated: the server returned {len(encoded)} bytes; what "
                          f"follows is the first {len(head.encode('utf-8'))}]\n{head}")
        saved = await _save(ctx, ".txt", encoded)
        if saved:
            output["text_saved_to"] = saved
    body = json.dumps(output, ensure_ascii=False)
    return ToolOutput(content=[Text(text=body), *images], is_error=is_error,
                      meta={"non_text_blocks": non_text, "images": len(images),
                            "structured": structured is not None})
