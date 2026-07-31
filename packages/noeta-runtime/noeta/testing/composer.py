"""Composer fixtures: an empty-but-valid ``ThreeSegmentComposer`` for tests that
need an instance rather than prompt content, and a synthetic three-segment
``View`` for Policy tests that never build a Composer at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from noeta.protocols.content_store import ContentStore
from noeta.protocols.messages import Message, TextBlock
from noeta.protocols.values import ContentRef
from noeta.protocols.view import View, ViewSegment

if TYPE_CHECKING:
    from noeta.context.composer import ThreeSegmentComposer


__all__ = ["trivial_three_segment", "fake_view"]


def trivial_three_segment(content_store: ContentStore) -> "ThreeSegmentComposer":
    """Empty-prompt, empty-tools Composer — wiring infrastructure, not a
    subject: three-segment behaviour has its own tests."""
    # Lazy so that importing this module for :func:`fake_view` alone does not
    # drag in ``ThreeSegmentComposer``.
    from noeta.context.composer import ThreeSegmentComposer

    return ThreeSegmentComposer(
        system_prompt="",
        tools={},
        content_store=content_store,
    )


def fake_view(
    messages: Optional[list[Message]] = None,
    *,
    system_prompt: str = "",
    provider_tool_schemas: Optional[list[dict[str, Any]]] = None,
) -> View:
    """Synthetic three-segment View for Policy unit tests.

    No ContentStore and no hashing: ``segment_hash`` and ``plan_ref`` are
    filler, shape-valid but not byte-meaningful. ``rolling_history`` mirrors
    ``messages`` so that a compaction-aware Policy — which measures its
    summarise boundary against the raw history — sees the same non-empty
    history here as in production; with ``semi_stable`` empty and no prior
    summary the two coincide, and only a real Composer makes them diverge.
    """
    msgs = list(messages or [])
    return View(
        plan_ref=ContentRef(hash="0" * 64, size=0, media_type="application/json"),
        segments=(
            ViewSegment(
                name="stable_prefix",
                content=[
                    Message(role="system", content=[TextBlock(text=system_prompt)])
                ],
                segment_hash="0" * 64,
            ),
            ViewSegment(name="semi_stable", content=[], segment_hash="1" * 64),
            ViewSegment(
                name="dynamic_suffix",
                content=msgs,
                segment_hash="2" * 64,
            ),
        ),
        provider_tool_schemas=provider_tool_schemas or [],
        rolling_history=list(msgs),
    )
