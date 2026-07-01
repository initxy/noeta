"""Canonical round-trip tests for Phase 1 typed Message / Block protocol.

SSOT: ``to_canonical_bytes`` → ``from_canonical_bytes`` must
restore tagged typed values back into their dataclass instances. Phase 1
adds five new typed shapes (``Message`` + four ``Block`` subclasses);
this module is the regression barrier that they each register a
``__canonical_tag__`` and a restorer so the round-trip preserves
``isinstance`` identity.
"""

from __future__ import annotations

import pytest

from noeta.protocols.canonical import from_canonical_bytes, to_canonical_bytes
from noeta.protocols.messages import (
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.values import ContentRef


@pytest.mark.parametrize(
    "obj",
    [
        TextBlock(text="hello"),
        ThinkingBlock(text="let me think", signature="sig-abc"),
        ThinkingBlock(text="reasoning without sig"),
        ToolUseBlock(
            call_id="call-1",
            tool_name="echo",
            arguments={"x": 1, "y": "two"},
        ),
        ToolResultBlock(
            call_id="call-1",
            output="echoed",
            success=True,
        ),
        ToolResultBlock(
            call_id="call-2",
            output=None,
            success=False,
            error="boom",
        ),
        ImageBlock(
            source=ContentRef(
                hash="a" * 64,
                size=1234,
                media_type="image/png",
            ),
        ),
        Message(role="user", content=[TextBlock(text="hi")]),
        Message(
            role="user",
            content=[
                TextBlock(text="look at this"),
                ImageBlock(
                    source=ContentRef(
                        hash="b" * 64,
                        size=42,
                        media_type="image/jpeg",
                    ),
                ),
            ],
        ),
    ],
)
def test_typed_block_or_message_round_trips_to_same_typed_object(obj: object) -> None:
    """Every typed Block/Message round-trips into a typed instance equal to original."""
    canonical = to_canonical_bytes(obj)
    restored = from_canonical_bytes(canonical)
    assert isinstance(restored, type(obj))
    assert restored == obj


def test_mixed_content_message_round_trip_preserves_block_order_and_types() -> None:
    """A Message containing ThinkingBlock + TextBlock + ToolUseBlock + ImageBlock round-trips."""
    msg = Message(
        role="user",
        content=[
            ThinkingBlock(text="thinking aloud", signature="sig-xyz"),
            TextBlock(text="here's my plan"),
            ImageBlock(
                source=ContentRef(
                    hash="c" * 64,
                    size=99,
                    media_type="image/webp",
                ),
            ),
            ToolUseBlock(
                call_id="call-7",
                tool_name="lookup",
                arguments={"q": "weather"},
            ),
        ],
    )
    restored = from_canonical_bytes(to_canonical_bytes(msg))
    assert isinstance(restored, Message)
    assert restored == msg
    # explicit per-block isinstance to nail the contract
    assert isinstance(restored.content[0], ThinkingBlock)
    assert isinstance(restored.content[1], TextBlock)
    assert isinstance(restored.content[2], ImageBlock)
    assert isinstance(restored.content[3], ToolUseBlock)
    # the nested ContentRef must restore as a typed instance, not a bare dict
    assert isinstance(restored.content[2].source, ContentRef)


def test_image_block_round_trips_to_typed_instance() -> None:
    """``ImageBlock`` (carrying only a ContentRef handle) round-trips into a
    typed instance — never a bare dict — and the nested ContentRef stays typed."""
    block = ImageBlock(
        source=ContentRef(hash="d" * 64, size=2048, media_type="image/png"),
    )
    restored = from_canonical_bytes(to_canonical_bytes(block))
    assert isinstance(restored, ImageBlock)
    assert isinstance(restored.source, ContentRef)
    assert restored == block


def test_default_tool_result_images_omitted_canonical_bytes_pinned() -> None:
    """A ``ToolResultBlock`` with no ``images`` stays byte-identical to its
    pre-image shape — the ``images`` key is OMITTED, not serialized as ``null``.

    This is the headline replay invariant for the read-image feature: EVERY
    historical tool-result rides this canonical path (``MessagesAppended`` bodies
    + snapshot hashes), so a default ``images`` field showing up — even as
    ``null`` — would break byte-identical replay of all pre-image recordings. The
    round-trip equality tests above would NOT catch that (``None`` → ``null`` →
    ``None`` round-trips), so this pins the bytes directly (mirrors
    ``test_message_origin``'s default-origin byte pin)."""
    body = to_canonical_bytes(
        ToolResultBlock(call_id="call-1", output="echoed", success=True)
    )
    assert b"images" not in body
    assert body == (
        b'{"__canonical_tag__":"tool_result_block",'
        b'"call_id":"call-1","error":null,"output":"echoed","success":true}'
    )


def test_legacy_tool_result_without_images_restores_to_none() -> None:
    """A tool_result body from a pre-image recording (no ``images`` key) restores
    with ``images=None`` — the absent key folds to the default, no crash."""
    legacy = (
        b'{"__canonical_tag__":"tool_result_block",'
        b'"call_id":"x","error":null,"output":"y","success":true}'
    )
    restored = from_canonical_bytes(legacy)
    assert isinstance(restored, ToolResultBlock)
    assert restored.images is None


def test_tool_result_with_images_round_trips_typed() -> None:
    """When present, ``ToolResultBlock.images`` round-trips into a typed
    ``list[ImageBlock]`` (each carrying a typed ContentRef) — the carrier the
    adapters deref→inline at wire time."""
    block = ToolResultBlock(
        call_id="c2",
        output="saw a chart",
        success=True,
        images=[
            ImageBlock(
                source=ContentRef(hash="a" * 64, size=12, media_type="image/png")
            )
        ],
    )
    restored = from_canonical_bytes(to_canonical_bytes(block))
    assert isinstance(restored, ToolResultBlock)
    assert restored == block
    assert isinstance(restored.images[0], ImageBlock)
    assert isinstance(restored.images[0].source, ContentRef)
