"""A request's size on the wire, kept under the endpoint's limit.

Every adapter applies two rules on the way to the wire. First the images give
way: :func:`fit_request` replaces the oldest with a placeholder once the
images pass a byte or a count budget, Claude Code's rule. It is a forward
sweep over the conversation in which each removal is decided by the images up
to the one whose arrival made it, so appending messages never changes an
earlier decision and the prompt cache breaks once per cut. Then the body:
:func:`encode_body` serializes it as httpx serializes ``json=`` and refuses
one still over the limit with :class:`RequestTooLarge` before anything is
sent. A server that refuses an oversized request often drops the connection
instead of answering, so the size is checked here rather than learned from
the answer.
"""

from __future__ import annotations

import itertools
import json
from collections import deque
from collections.abc import Iterator, Sequence
from typing import Any

import msgspec

from noeta.core import models
from noeta.core.errors import RequestTooLarge
from noeta.core.images import base64_size
from noeta.core.types import (
    Block,
    Image,
    Message,
    ModelRequest,
    Text,
    ToolReference,
    ToolResult,
)

__all__ = [
    "DEFAULT_MAX_REQUEST_BYTES",
    "SEEN_PLACEHOLDER",
    "UNSEEN_PLACEHOLDER",
    "cap_media",
    "encode_body",
    "fit_request",
    "item_cap",
    "media_budget",
    "refused_note",
    "transport_note",
    "valid_limit",
]

_MIB = 1024 * 1024
#: Claude Code's request limit.
DEFAULT_MAX_REQUEST_BYTES = 32 * _MIB
#: The most a cut takes the images below their byte cap.
_CUT_DEPTH = 10 * _MIB
#: Claude Code's image counts: the cap, the cap on a model whose window is
#: over :data:`_LARGE_WINDOW` tokens, and how many fewer than the cap a cut
#: leaves.
_ITEM_CAP = 100
_LARGE_WINDOW_ITEM_CAP = 600
_LARGE_WINDOW = 200_000
_ITEM_CUT = 20
#: From this body size on, a transport failure says the size.
_NOTED_BODY_BYTES = _MIB

#: What stands in for an image removed after the model saw it (an assistant
#: message lies between it and the image whose arrival removed it), and for
#: one removed before; Claude Code's texts.
SEEN_PLACEHOLDER = (
    "[media removed: request limit. You have already seen this, and what you concluded "
    "from it still stands. It made room for newer images and documents. Re-reading pushes "
    "out others and re-sends everything, so don't re-read them all.]")
UNSEEN_PLACEHOLDER = (
    "[media removed: request limit. You have not seen this: it did not fit in the request. "
    "If you need it, get it again once, by itself. If it is dropped again, go on without it "
    "and say so. Re-reading pushes out others and re-sends everything, so don't re-read "
    "them all.]")

#: Where an image is: its message, its block, and its place inside a tool
#: result (``None``: the block is the image).
Position = tuple[int, int, int | None]


def valid_limit(max_request_bytes: int) -> int:
    """``max_request_bytes`` when it is a positive integer, else
    :class:`ValueError`."""
    if isinstance(max_request_bytes, bool) or not isinstance(max_request_bytes, int) \
            or max_request_bytes <= 0:
        raise ValueError(f"max_request_bytes must be a positive integer, got "
                         f"{max_request_bytes!r}")
    return max_request_bytes


def media_budget(limit_bytes: int) -> tuple[int, int]:
    """The images' byte cap under a request limit of ``limit_bytes``, and the
    total a cut goes down to (24 MiB and 14 MiB under 32 MiB)."""
    cap = limit_bytes * 3 // 4
    return cap, cap - min(_CUT_DEPTH, cap // 2)


def item_cap(model: str) -> int:
    """How many images a request to ``model`` may carry."""
    spec = models.find_spec(model)
    large = spec is not None and spec.context_window > _LARGE_WINDOW
    return _LARGE_WINDOW_ITEM_CAP if large else _ITEM_CAP


def _images(messages: Sequence[Message], in_tool_results: bool
            ) -> Iterator[tuple[Position, int]]:
    """Every image in conversation order, with its size in base64."""
    for mi, message in enumerate(messages):
        for bi, block in enumerate(message.content):
            if isinstance(block, Image):
                yield (mi, bi, None), base64_size(len(block.data))
            elif in_tool_results and isinstance(block, ToolResult):
                for ii, inner in enumerate(block.content):
                    if isinstance(inner, Image):
                        yield (mi, bi, ii), base64_size(len(inner.data))


def cap_media(messages: Sequence[Message], *, byte_cap: int, low_mark: int,
              item_cap: int, in_tool_results: bool = True) -> dict[Position, str]:
    """The images to remove from ``messages``, each with the text that takes
    its place. Each image joins the kept ones; past ``item_cap`` the oldest
    kept go until ``item_cap`` less 20 remain, then past ``byte_cap`` the
    oldest go until the kept total is at most ``low_mark``.
    ``in_tool_results=False``: the wire sends no image of a tool result, so
    those take no room and stay as they are."""
    #: Assistant messages before each index.
    replies = list(itertools.accumulate((m.role == "assistant" for m in messages), initial=0))
    kept: deque[tuple[Position, int]] = deque()
    total = 0
    removed: dict[Position, str] = {}

    def drop_oldest(arrived: int) -> None:
        nonlocal total
        position, size = kept.popleft()
        total -= size
        seen = replies[arrived] - replies[position[0] + 1] > 0
        removed[position] = SEEN_PLACEHOLDER if seen else UNSEEN_PLACEHOLDER

    for position, size in _images(messages, in_tool_results):
        kept.append((position, size))
        total += size
        if len(kept) > item_cap:
            while len(kept) > item_cap - _ITEM_CUT:
                drop_oldest(position[0])
        if total > byte_cap:
            while kept and total > low_mark:
                drop_oldest(position[0])
    return removed


def fit_request(request: ModelRequest, limit_bytes: int, *,
                in_tool_results: bool = True) -> ModelRequest:
    """``request`` with the images :func:`cap_media` removes replaced: inside
    a tool result by a text block in the image's place, at a message's top
    level dropped, a message left empty holding the placeholders instead.
    Messages are never added or removed. A ``cache_blocks`` mark on a
    dropped block moves to the nearest earlier block of its message, or goes.
    A request under both caps is returned as it is. ``in_tool_results``:
    whether the adapter sends a tool result's images to this model."""
    byte_cap, low_mark = media_budget(limit_bytes)
    removed = cap_media(request.messages, byte_cap=byte_cap, low_mark=low_mark,
                        item_cap=item_cap(request.model), in_tool_results=in_tool_results)
    if not removed:
        return request
    by_message: dict[int, dict[tuple[int, int | None], str]] = {}
    for (mi, bi, ii), text in removed.items():
        by_message.setdefault(mi, {})[(bi, ii)] = text
    messages = list(request.messages)
    places: dict[int, list[int | None]] = {}
    for mi, texts in by_message.items():
        messages[mi], places[mi] = _fit_message(messages[mi], texts)
    marks = [mark for mark in (_moved(mark, places) for mark in request.cache_blocks)
             if mark is not None]
    return msgspec.structs.replace(request, messages=messages, cache_blocks=marks)


def _fit_message(message: Message, removed: dict[tuple[int, int | None], str]
                 ) -> tuple[Message, list[int | None]]:
    """``message`` without its ``removed`` images, and where each of its
    blocks went (``None``: dropped)."""
    content: list[Block] = []
    places: list[int | None] = []
    dropped: list[str] = []
    results = {bi for bi, ii in removed if ii is not None}
    for bi, block in enumerate(message.content):
        if (bi, None) in removed:
            dropped.append(removed[(bi, None)])
            places.append(None)
            continue
        if isinstance(block, ToolResult) and bi in results:
            block = msgspec.structs.replace(block, content=_fit_result(
                block, {ii: text for (b, ii), text in removed.items()
                        if b == bi and ii is not None}))
        places.append(len(content))
        content.append(block)
    if not content:
        content = [Text(text=t) for t in dict.fromkeys(dropped)]
    return msgspec.structs.replace(message, content=content), places


def _fit_result(result: ToolResult, removed: dict[int, str]
                ) -> list[Text | Image | ToolReference]:
    """``result``'s content, a text block in the place of each ``removed``
    image. An adapter may send the result's text blocks joined
    (``ToolResult.text``), so a placeholder is set on a line of its own
    between the texts around it."""
    content: list[Text | Image | ToolReference] = []
    said = ""
    for ii, block in enumerate(result.content):
        if ii in removed:
            text = removed[ii]
            if said and not said.endswith("\n"):
                text = f"\n{text}"
            if any(isinstance(later, Text) for later in result.content[ii + 1:]):
                text += "\n"
            block = Text(text=text)
        if isinstance(block, Text):
            said += block.text
        content.append(block)
    return content


def _moved(mark: tuple[int, int], places: dict[int, list[int | None]]
           ) -> tuple[int, int] | None:
    mi, bi = mark
    where = places.get(mi)
    if where is None or not 0 <= bi < len(where):
        return mark
    earlier = [p for p in where[:bi + 1] if p is not None]
    return (mi, earlier[-1]) if earlier else None


def encode_body(body: dict[str, Any], limit_bytes: int) -> bytes:
    """``body`` as the bytes httpx sends for ``json=body``;
    :class:`RequestTooLarge`, saying how much of it is images, when they are
    over ``limit_bytes``."""
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")
    if len(data) > limit_bytes:
        media = _media_bytes(body)
        raise RequestTooLarge(
            f"Request too large: {_megabytes(len(data))}, over the {_megabytes(limit_bytes)} "
            f"request limit ({_megabytes(media)} of it images)",
            body_bytes=len(data), limit_bytes=limit_bytes, media_bytes=media)
    return data


def _media_bytes(value: Any) -> int:
    """The image data in a wire body, as each wire carries an image: an
    Anthropic base64 ``source``, a Chat Completions ``image_url``, a
    Responses ``input_image``."""
    if isinstance(value, list):
        return sum(_media_bytes(v) for v in value)
    if not isinstance(value, dict):
        return 0
    kind = value.get("type")
    source = value.get("source")
    if kind == "image" and isinstance(source, dict) and isinstance(source.get("data"), str):
        return len(source["data"])
    url = value.get("image_url")
    if kind == "image_url" and isinstance(url, dict):
        url = url.get("url")
    if kind in ("image_url", "input_image") and isinstance(url, str):
        return len(url)
    return sum(_media_bytes(v) for v in value.values())


def _megabytes(n: int) -> str:
    return f"{n / _MIB:.1f} MB"


def transport_note(body_bytes: int) -> str:
    """What a transport failure before the response adds about a large body."""
    if body_bytes < _NOTED_BODY_BYTES:
        return ""
    return (f" (the request body was {_megabytes(body_bytes)}; a server refusing an "
            "oversized request may drop the connection this way: if this endpoint's limit "
            "is lower, set max_request_bytes)")


def refused_note(body_bytes: int) -> str:
    """What a 413 answer adds: the size of the body it refused."""
    return f" (the request body was {_megabytes(body_bytes)})"
