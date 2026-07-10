"""Composer helpers for tests.

Issue 14 deleted ``MinimalComposer``: ``ThreeSegmentComposer`` is the
single in-tree Composer implementation. Test fixtures that don't care
about prompt content but need a Composer instance just want one
configured with empty defaults — :func:`trivial_three_segment` is
that fixture.

Policy unit tests that exercise ``Policy.decide`` with a synthetic
View (no real Composer involved) build one through :func:`fake_view`
— View now requires the three-segment shape (``View.messages``
legacy field was deleted in this slice).
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
    """Empty-prompt, empty-tools Composer for tests that just need a
    valid Composer instance (the full three-segment behaviour is
    exercised in dedicated tests; here it's wiring infrastructure).

    ``noeta.context`` ships in noeta-runtime alongside this module; the
    import stays lazy so importing ``noeta.testing.composer`` for
    :func:`fake_view` alone doesn't also pull in ``ThreeSegmentComposer``."""
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

    Skips real ContentStore / hashing: ``segment_hash`` and ``plan_ref``
    are filler values, valid in shape but not byte-meaningful. Use
    this when the test cares about Policy behaviour given a View, not
    about Composer semantics.

    ``rolling_history`` mirrors ``messages`` so a compaction-aware Policy
    (which computes its summarise boundary against the RAW history the real
    Composer exposes — finding 2) sees a non-empty raw history in unit tests
    just as it would in production. In ``fake_view`` ``semi_stable`` is empty
    and no prior summary exists, so ``iter_messages()`` and ``rolling_history``
    coincide here — the divergence the fix guards against only appears with a
    real Composer (non-empty ``semi_stable`` / a prior summary).
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
