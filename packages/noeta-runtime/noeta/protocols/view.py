"""View: the Composer's output, the Policy's input.

Issue 14 three-segment shape:

* ``segments`` is a 3-tuple of typed :class:`ViewSegment`
  (``stable_prefix`` / ``semi_stable`` / ``dynamic_suffix``).
* ``provider_tool_schemas`` is a separate field (not inside any Block) per the
  PRD §"Grill round 1 #1" decision — JSON Schema lists should not be
  stringified through ``TextBlock``.
* ``plan_ref`` points at the :class:`noeta.protocols.context_plan.ContextPlan`
  body in ContentStore.

:meth:`iter_messages` is the canonical accessor for "the message
history a Policy would feed to an LLM": it returns
``semi_stable.content + dynamic_suffix.content``. The stable_prefix
content flows separately into ``LLMRequest.system``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from noeta.protocols.canonical import register
from noeta.protocols.messages import Message
from noeta.protocols.values import ContentRef


SegmentName = Literal["stable_prefix", "semi_stable", "dynamic_suffix"]


@dataclass(frozen=True, slots=True)
class ViewSegment:
    """One named section of a composed View.

    ``content`` is a list of typed :class:`Message` so dynamic_suffix
    keeps role information intact and stable/semi-stable can sit next
    to it without a type-level shape change. ``segment_hash`` is the
    sha256 hex of the canonical bytes of (``content``, plus any
    segment-specific extras the Composer hashes together with the
    content — for stable_prefix the Composer folds ``provider_tool_schemas``
    in too, per PRD §"Grill round 1 #1").

    The earlier ``entry_sources`` per-entry attribution was retired along with
    the test infrastructure that consumed it (it was View metadata only — never
    in ``segment_hash``, the ``ContextPlan`` body, or the wire format). An old
    recording that still carries the key restores cleanly: the restorer reads
    only the live fields.
    """

    name: SegmentName
    content: list[Message]
    segment_hash: str

    __canonical_tag__ = "view_segment"


def _restore_segment(fields: dict[str, Any]) -> ViewSegment:
    return ViewSegment(
        name=fields["name"],
        content=list(fields.get("content", [])),
        segment_hash=fields["segment_hash"],
    )


register("view_segment", _restore_segment)


@dataclass(frozen=True, slots=True)
class View:
    """A composed projection of a Task ready for Policy consumption.

    Issue 14 fields:

    * ``segments`` — 3-tuple, fixed order (stable_prefix / semi_stable
      / dynamic_suffix).
    * ``provider_tool_schemas`` — JSON-Schema tool descriptions, passed
      verbatim into ``LLMRequest.tools``.
    * ``plan_ref`` — :class:`ContentRef` to the ContextPlan body.
      Stays Optional only for the Engine fallback / lazy-default path
      where no Composer has been wired yet; production Composers
      always populate it.
    """

    plan_ref: Optional[ContentRef] = None
    segments: tuple[ViewSegment, ...] = field(default_factory=tuple)
    provider_tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    #: ③ (D-3, finding 2) — the RAW rolling history (``task.runtime.messages``)
    #: the Composer summarised/pruned against, in its *own* coordinate space.
    #: A compaction-aware Policy computes its summarise boundary against THIS
    #: list (never against ``iter_messages()``, which is the post-summary /
    #: post-prune / skill-prefixed / tail-truncated projection): the boundary
    #: it records on ``CompactionRequestedDecision`` is therefore a raw
    #: ``task.runtime.messages`` index — exactly the coordinate the Composer's
    #: ``_apply_summary`` slices with (``ContextState.summary_boundary``), so
    #: policy-computed and composer-applied boundaries point at the same
    #: messages even when ``semi_stable`` is non-empty or a prior summary
    #: already collapsed a prefix. Defaulted empty (View is in-memory only,
    #: never serialised) → byte-safe for any View that does not set it.
    rolling_history: list[Message] = field(default_factory=list)
    #: ③ (D-3, finding 2) — the cumulative raw-history index already collapsed
    #: behind the current summary (``ContextState.summary_boundary``). The
    #: Policy treats a fresh boundary as cumulative-from-zero over
    #: ``rolling_history`` (the Composer always replaces ``[:boundary]`` with a
    #: single summary), so this is exposed for visibility / debugging and
    #: anti-spiral progress checks. ``0`` (default) when nothing is collapsed
    #: yet → byte-safe.
    summary_boundary: int = 0

    def iter_messages(self) -> list[Message]:
        """The message history a Policy hands to the LLM.

        Returns ``semi_stable.content + dynamic_suffix.content``;
        stable_prefix is consumed via ``LLMRequest.system`` at the
        Policy call site.
        """
        if not self.segments:
            return []
        return list(self.segments[1].content) + list(self.segments[2].content)
