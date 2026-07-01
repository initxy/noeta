"""Canonical round-trip tests for issue 14 typed shapes.

SSOT: every new typed value that travels through ContentStore
must declare ``__canonical_tag__`` and register a restorer so
``to_canonical_bytes → from_canonical_bytes`` rebuilds the typed
object. Issue 14 introduces ``ContextPlan`` and the ``ViewSegment``
helper carried inside ``View.segments``.
"""

from __future__ import annotations

import pytest

from noeta.protocols.canonical import from_canonical_bytes, to_canonical_bytes
from noeta.protocols.context_plan import ContextPlan
from noeta.protocols.messages import TextBlock, ToolUseBlock
from noeta.protocols.values import ContentRef
from noeta.protocols.view import ViewSegment


@pytest.mark.parametrize(
    "obj",
    [
        ContextPlan(
            composer_version="three_segment.v1",
            segment_hashes={
                "stable_prefix": "a" * 64,
                "semi_stable": "b" * 64,
                "dynamic_suffix": "c" * 64,
            },
            selected_skills=["s1", "s2"],
            selected_messages=[
                ContentRef(hash="h1", size=10, media_type="application/json"),
            ],
            dropped_messages=[],
        ),
        ViewSegment(
            name="stable_prefix",
            content=[TextBlock(text="system prompt")],
            segment_hash="d" * 64,
        ),
        ViewSegment(
            name="dynamic_suffix",
            content=[
                TextBlock(text="hi"),
                ToolUseBlock(call_id="c1", tool_name="echo", arguments={"x": 1}),
            ],
            segment_hash="e" * 64,
        ),
    ],
)
def test_issue14_typed_objects_round_trip_to_same_typed_instance(obj: object) -> None:
    restored = from_canonical_bytes(to_canonical_bytes(obj))
    assert isinstance(restored, type(obj))
    assert restored == obj
