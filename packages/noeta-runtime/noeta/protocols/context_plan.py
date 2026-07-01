"""ContextPlan: metadata for one Composer-assembled View (L0).

The body goes to ContentStore; the ref goes into the View and folds into
``task.context.plan_ref`` via the ``ContextPlanComposed`` event.

Field semantics and the assembly contract live with the Composer in
``noeta.context``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from noeta.protocols.canonical import register
from noeta.protocols.values import ContentRef


__all__ = ["ContextPlan"]


@dataclass(frozen=True, slots=True)
class ContextPlan:
    composer_version: str
    segment_hashes: dict[str, str]
    selected_skills: list[str] = field(default_factory=list)
    selected_messages: list[ContentRef] = field(default_factory=list)
    dropped_messages: list[ContentRef] = field(default_factory=list)
    # One entry per tool output the prune
    # step cleared outside the tail window: the ContentStore ref of the FULL
    # original body. Lives here (internal provenance) instead of inside the
    # model-facing ``[tool output cleared]`` marker, which carries no hash — the
    # model has no ref-deref tool, so a hash in the prompt is dead weight. Audit
    # / trace deref the original body through these refs. Defaulted empty;
    # ``_restore`` reads it via ``.get`` so pre-v5 plan bodies still deserialize
    # (the new field changes ``plan_ref`` bytes — hence ``composer_version``
    # ``three_segment.v5``).
    cleared_outputs: list[ContentRef] = field(default_factory=list)
    # Phase 4.5 Issue D — skill referenced-file retrieval provenance.
    # One entry per **body-referenced** resource of an active skill:
    # ``reason="referenced"`` carries the ``content_ref`` of the raw
    # resource bytes inlined into ``semi_stable`` + ``bytes`` /
    # ``media_type``; ``reason="skipped:*"`` has ``content_ref=None`` and
    # never entered the prompt. Defaulted empty; ``_restore`` reads it via
    # ``.get`` so old plan bodies / snapshots still deserialize. (The new
    # field does change ``plan_ref`` bytes — hence the ``composer_version``
    # bump to ``three_segment.v2``; a pre-D recording re-derived under the new
    # generation produces different plan bytes, which the version bump marks as
    # expected.)
    retrieved_resources: list[dict[str, Any]] = field(default_factory=list)

    __canonical_tag__ = "context_plan"


def _restore(fields: dict[str, object]) -> ContextPlan:
    return ContextPlan(
        composer_version=fields["composer_version"],  # type: ignore[arg-type]
        segment_hashes=dict(fields["segment_hashes"]),  # type: ignore[call-overload]
        selected_skills=list(fields.get("selected_skills", [])),  # type: ignore[call-overload]
        selected_messages=list(fields.get("selected_messages", [])),  # type: ignore[call-overload]
        dropped_messages=list(fields.get("dropped_messages", [])),  # type: ignore[call-overload]
        cleared_outputs=list(fields.get("cleared_outputs", [])),  # type: ignore[call-overload]
        retrieved_resources=list(fields.get("retrieved_resources", [])),  # type: ignore[call-overload]
    )


register("context_plan", _restore)
