"""A ``tools/call`` result as the model sees it (Claude Code's shape).

The server's ``content`` blocks pass through as the result's blocks, in
order, with no wrapper: a text block stays a text block, an embedded text
resource becomes its text, a displayable image travels as an image block, and
anything the model cannot see inline (audio, a resource link, a binary
resource, an unusable image) becomes a one-line description. A result with
only ``structuredContent`` is that JSON as text.

A result is held to Claude Code's budget (25,000 tokens, or
``NOETA_MAX_MCP_OUTPUT_TOKENS``, its ``MAX_MCP_OUTPUT_TOKENS``), sized as
Claude Code sizes it: estimated first (text by
``noeta.toolkit.tokens.estimate_tokens``, an image 1,600); under half the
budget it fits and text alone past four times it does not; in between the
provider counts the text (``ToolContext.count_tokens``), the estimate standing
in where it cannot. A text-only result over it is saved
under the spill directory and the model gets only where, its size and format,
and how to read it; with images, or nowhere to save, images that do not fit
are left out and the text is cut to its head, marked at the end.
``isError: true`` is an error result.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import posixpath
from typing import Any

from noeta.core.images import ImageTooLarge, fit_image
from noeta.core.tool import ToolContext, ToolOutput
from noeta.core.types import Image, Text
from noeta.toolkit.tokens import estimate_tokens, prefix_within
from noeta.toolkit.tools import save_spilled, spill_path

__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS", "IMAGE_TOKENS", "describe_block", "max_output_tokens",
    "render_result",
]

#: Claude Code's budget for one MCP result, in tokens.
DEFAULT_MAX_OUTPUT_TOKENS = 25_000
#: What one image counts against it (Claude Code's estimate).
IMAGE_TOKENS = 1_600
#: Claude Code's band around the budget where an estimate is not trusted: a
#: result estimated at most half the budget fits, text alone estimated past
#: four times it does not, and the provider counts the rest.
_FITS_UNDER = 0.5
_OVER_PAST = 4
_INLINE_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def max_output_tokens() -> int:
    """``NOETA_MAX_MCP_OUTPUT_TOKENS`` when it is a positive number, else the
    default."""
    try:
        value = int(os.environ.get("NOETA_MAX_MCP_OUTPUT_TOKENS", ""))
    except ValueError:
        return DEFAULT_MAX_OUTPUT_TOKENS
    return value if value > 0 else DEFAULT_MAX_OUTPUT_TOKENS


def _decode_b64(data: object) -> bytes | None:
    if not isinstance(data, str):
        return None
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None


def describe_block(block: object) -> tuple[str, Image | None]:
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
        try:
            fitted = fit_image(raw, str(mime))
        except ImageTooLarge as exc:
            return f"[image ({mime_s}, {len(raw)} bytes): not shown: {exc}]", None
        return fitted.note() or "", Image(media_type=fitted.media_type, data=fitted.data)
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
    return path if await save_spilled(ctx, path, data) else None


def _saved_note(path: str, full: str, limit: int, *, as_json: bool) -> str:
    """The whole result when it was too large and saved instead of shown."""
    lines = full.count("\n") + 1
    size = f"{len(full):,} characters" + (f" in {lines:,} lines" if lines > 1 else "")
    how = ("Query it with jq, or slice it with python if a value is too long for one line."
           if as_json else
           "Read it in parts with Read's offset and limit, or search it with grep (jq if "
           "it is JSON); lines too long to page by line can be cut by character position.")
    return (f"The result ({size}) is over the {limit}-token limit for an MCP result "
            f"(NOETA_MAX_MCP_OUTPUT_TOKENS), so it was saved to {path} instead of shown. "
            f"Format: {'JSON' if as_json else 'plain text'}.\n{how}\n"
            "To summarize or analyze it, read all of it first, in order, and say which part "
            "you read if you could not read it all.")


async def _text_tokens(blocks: list[Text | Image], estimated: int, images: int, limit: int,
                       ctx: ToolContext) -> int:
    """The result's text in tokens: ``estimated`` unless the whole result
    falls in the band where Claude Code asks the provider for a count."""
    if (estimated + images * IMAGE_TOKENS <= limit * _FITS_UNDER
            or estimated > limit * _OVER_PAST or ctx.count_tokens is None or not estimated):
        return estimated
    counted = await ctx.count_tokens("\n".join(b.text for b in blocks if isinstance(b, Text)))
    return counted if counted else estimated


async def render_result(result: dict[str, Any], ctx: ToolContext) -> ToolOutput:
    is_error = bool(result.get("isError"))
    content = result.get("content")
    blocks: list[Text | Image] = []
    non_text = 0
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    blocks.append(Text(text=t))
                continue
            non_text += 1
            # Decoding a server's image is CPU work: off the event loop.
            line, image = await asyncio.to_thread(describe_block, block)
            if image is not None:
                blocks.append(image)
            elif line:
                blocks.append(Text(text=line))
            if image is not None and line:
                blocks.append(Text(text=line))
    structured = result.get("structuredContent")
    from_structured = structured is not None and not blocks
    if from_structured:
        # Only a structured result: its JSON is the text.
        blocks.append(Text(text=json.dumps(structured, ensure_ascii=False, sort_keys=True)))
    limit = max_output_tokens()
    budget = limit
    images = [b for b in blocks if isinstance(b, Image)]
    fitting = min(len(images), budget // IMAGE_TOKENS)
    if fitting < len(images):
        drop = {id(b) for b in images[fitting:]}
        blocks = [b for b in blocks if id(b) not in drop]
        # First, so cutting the text cannot drop it.
        blocks.insert(0, Text(text=f"[{len(images) - fitting} image(s) left out: over the "
                                   "tool output limit]"))
    budget -= fitting * IMAGE_TOKENS
    text_len = sum(len(b.text) for b in blocks if isinstance(b, Text))
    estimated = sum(estimate_tokens(b.text) for b in blocks if isinstance(b, Text))
    tokens = await _text_tokens(blocks, estimated, len(images), limit, ctx)
    # The estimate's units per counted token, to cut by the estimate.
    scale = estimated / tokens if tokens and estimated else 1.0
    if tokens > budget:
        full = "\n".join(b.text for b in blocks if isinstance(b, Text))
        saved = None if images else await _save(ctx, ".txt", full.encode("utf-8"))
        if saved is not None:
            blocks = [Text(text=_saved_note(saved, full, limit, as_json=from_structured))]
        else:
            cut: list[Text | Image] = []
            left, shown = int(budget * scale), 0
            for b in blocks:
                if isinstance(b, Image):
                    cut.append(b)
                elif left > 0:
                    head = b.text[:prefix_within(b.text, left)]
                    cut.append(Text(text=head))
                    shown += len(head)
                    left = left - estimate_tokens(head) if head == b.text else 0
            cut.append(Text(text=(
                f"[output truncated: over the {limit} token limit for an MCP result "
                f"(NOETA_MAX_MCP_OUTPUT_TOKENS); the server returned {text_len} characters and "
                f"only the first {shown} are shown above. Use the server's pagination or "
                "filtering to get the rest; without them, tell the user the result is "
                "incomplete]")))
            blocks = cut
    return ToolOutput(content=list(blocks), is_error=is_error, block_list=True,
                      meta={"non_text_blocks": non_text,
                            "images": sum(isinstance(b, Image) for b in blocks),
                            "structured": structured is not None})
