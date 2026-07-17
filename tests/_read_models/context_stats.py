"""Read-only context-size statistics for code sessions (CW18e).

This module intentionally works over the CW17 projection objects instead of
EventLog/runtime internals. It reports only recorded refs/counts; it never reads
prompt bodies, estimates tokens, runs the composer, or constructs a provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


__all__ = [
    "ContextAggregateStats",
    "ContextPlanStats",
    "SelectionStats",
    "aggregate_context_stats",
    "compact_context_stats",
    "plan_stats",
    "selection_stats",
]


@dataclass(frozen=True, slots=True)
class ContextPlanStats:
    plan_ref: dict[str, Any]
    selected_message_count: int
    selected_message_bytes: int
    dropped_message_count: int
    dropped_message_bytes: int
    retrieved_resource_count: int
    retrieved_resource_bytes: int
    active_skill_count: int
    segment_hash_count: int
    decode_error: Optional[str] = None


@dataclass(frozen=True, slots=True)
class SelectionStats:
    request_bytes: Optional[int]
    input_tokens: int
    input_tokens_available: bool
    candidates: int
    selected: int
    dropped: int
    limit: int
    dropped_ratio: Optional[float]


@dataclass(frozen=True, slots=True)
class ContextAggregateStats:
    plan_count: int
    selection_count: int
    selected_message_bytes: int
    dropped_message_bytes: int
    retrieved_resource_bytes: int
    request_bytes: int
    input_tokens: int
    input_tokens_available_count: int
    max_request_bytes: Optional[int]
    max_input_tokens: Optional[int]
    max_dropped: Optional[int]
    decode_error_count: int


def _bytes(value: Any) -> int:
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _sum_ref_bytes(refs: Any) -> int:
    total = 0
    for ref in refs or ():
        if isinstance(ref, dict):
            total += _bytes(ref.get("bytes"))
    return total


def plan_stats(plan: Any) -> ContextPlanStats:
    selected = getattr(plan, "selected_messages", ())
    dropped = getattr(plan, "dropped_messages", ())
    resources = getattr(plan, "retrieved_resources", ())
    decode_error = getattr(plan, "decode_error", None)
    if decode_error is not None:
        return ContextPlanStats(
            plan_ref=dict(getattr(plan, "plan_ref", {})),
            selected_message_count=0,
            selected_message_bytes=0,
            dropped_message_count=0,
            dropped_message_bytes=0,
            retrieved_resource_count=0,
            retrieved_resource_bytes=0,
            active_skill_count=0,
            segment_hash_count=0,
            decode_error=str(decode_error),
        )
    return ContextPlanStats(
        plan_ref=dict(getattr(plan, "plan_ref", {})),
        selected_message_count=len(selected),
        selected_message_bytes=_sum_ref_bytes(selected),
        dropped_message_count=len(dropped),
        dropped_message_bytes=_sum_ref_bytes(dropped),
        retrieved_resource_count=len(resources),
        retrieved_resource_bytes=_sum_ref_bytes(resources),
        active_skill_count=len(getattr(plan, "selected_skills", ())),
        segment_hash_count=len(getattr(plan, "segment_hashes", {})),
        decode_error=None,
    )


def selection_stats(selection: Any) -> SelectionStats:
    request_ref = getattr(selection, "request_ref", {})
    request_bytes = request_ref.get("bytes") if isinstance(request_ref, dict) else None
    request_bytes = request_bytes if isinstance(request_bytes, int) else None
    input_tokens = int(getattr(selection, "input_tokens", 0) or 0)
    candidates = int(getattr(selection, "candidates", 0) or 0)
    dropped = int(getattr(selection, "dropped", 0) or 0)
    return SelectionStats(
        request_bytes=request_bytes,
        input_tokens=input_tokens,
        input_tokens_available=input_tokens > 0,
        candidates=candidates,
        selected=int(getattr(selection, "selected", 0) or 0),
        dropped=dropped,
        limit=int(getattr(selection, "limit", 0) or 0),
        dropped_ratio=(dropped / candidates) if candidates > 0 else None,
    )


def aggregate_context_stats(
    plans: list[Any],
    selections: list[Any],
) -> ContextAggregateStats:
    pstats = [plan_stats(plan) for plan in plans]
    sstats = [selection_stats(selection) for selection in selections]
    request_values = [
        stat.request_bytes for stat in sstats if stat.request_bytes is not None
    ]
    input_values = [
        stat.input_tokens for stat in sstats if stat.input_tokens_available
    ]
    dropped_values = [stat.dropped for stat in sstats]
    return ContextAggregateStats(
        plan_count=len(plans),
        selection_count=len(selections),
        selected_message_bytes=sum(s.selected_message_bytes for s in pstats),
        dropped_message_bytes=sum(s.dropped_message_bytes for s in pstats),
        retrieved_resource_bytes=sum(s.retrieved_resource_bytes for s in pstats),
        request_bytes=sum(request_values),
        input_tokens=sum(input_values),
        input_tokens_available_count=len(input_values),
        max_request_bytes=max(request_values) if request_values else None,
        max_input_tokens=max(input_values) if input_values else None,
        max_dropped=max(dropped_values) if dropped_values else None,
        decode_error_count=sum(1 for s in pstats if s.decode_error is not None),
    )


def compact_context_stats(
    plans: list[Any],
    selections: list[Any],
) -> dict[str, Any]:
    aggregate = aggregate_context_stats(plans[-1:], selections[-1:])
    latest_plan = plans[-1] if plans else None
    latest_selection = selections[-1] if selections else None
    latest_plan_stats = plan_stats(latest_plan) if latest_plan is not None else None
    latest_selection_stats = (
        selection_stats(latest_selection) if latest_selection is not None else None
    )
    return {
        "plan_seq": getattr(latest_plan, "seq", None),
        "selection_seq": getattr(latest_selection, "seq", None),
        "selected_message_count": (
            latest_plan_stats.selected_message_count
            if latest_plan_stats is not None else 0
        ),
        "selected_message_bytes": aggregate.selected_message_bytes,
        "dropped_message_count": (
            latest_plan_stats.dropped_message_count
            if latest_plan_stats is not None else 0
        ),
        "dropped_message_bytes": aggregate.dropped_message_bytes,
        "retrieved_resource_bytes": aggregate.retrieved_resource_bytes,
        "request_bytes": (
            latest_selection_stats.request_bytes
            if latest_selection_stats is not None else None
        ),
        "input_tokens": (
            latest_selection_stats.input_tokens
            if latest_selection_stats is not None else 0
        ),
        "input_tokens_available": (
            latest_selection_stats.input_tokens_available
            if latest_selection_stats is not None else False
        ),
        "decode_error_count": aggregate.decode_error_count,
    }
