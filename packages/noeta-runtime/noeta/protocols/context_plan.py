"""ContextPlan: metadata for one Composer-assembled View.

The body goes to ContentStore; the ref travels in the View and folds into
``task.context.plan_ref`` through the ``ContextPlanComposed`` event. Field
semantics and the assembly contract live with the Composer in ``noeta.context``.
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
    # One entry per tool output the prune step cleared outside the tail window:
    # the ContentStore ref of the FULL original body, so audit and trace can
    # deref it. Provenance belongs here rather than in the model-facing
    # ``[tool output cleared]`` marker, because the model has no ref-deref tool
    # and a hash in the prompt is dead weight.
    cleared_outputs: list[ContentRef] = field(default_factory=list)
    # One entry per body-referenced resource of an active skill:
    # ``reason="referenced"`` carries the ``content_ref`` of the raw resource
    # bytes inlined into ``semi_stable`` plus ``bytes`` / ``media_type``;
    # ``reason="skipped:*"`` has ``content_ref=None`` and never entered the
    # prompt.
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
