"""``noeta.sdk`` usage scoring for the skill menu — the ledger is the counter.

A host folds a tenant's event streams into per-skill usage and scores it with
Claude Code's decay to build the ``menu_rank`` the ``skills`` pack keeps its
roster order by. Noeta records nothing new for this: only activations after a
task's loop started count, so host preloads never rank themselves first.
"""

from __future__ import annotations

import pytest

from noeta.protocols.events import EventEnvelope, TaskStatePatchedPayload
from noeta.sdk import (
    SkillUsage,
    decayed_usage_score,
    rank_skills_by_usage,
    skill_usage_from_events,
)


DAY = 86_400.0


def _event(task_id: str, type_: str, *, at: float, patch=None) -> EventEnvelope:
    payload = TaskStatePatchedPayload(patch=patch) if patch is not None else None
    return EventEnvelope.build(
        task_id=task_id, type=type_, payload=payload, occurred_at=at
    )


def _activate(task_id: str, names: list[str], *, at: float) -> EventEnvelope:
    return _event(
        task_id, "TaskStatePatched", at=at, patch={"activate_skills": names}
    )


def test_counts_only_activations_after_the_loop_started() -> None:
    events = [
        # Task A: a host preload before the first compose, then two model picks.
        _activate("A", ["preloaded"], at=10.0),
        _event("A", "ContextPlanComposed", at=11.0),
        _activate("A", ["coder"], at=12.0),
        _activate("A", ["coder", "reviewer"], at=13.0),
        # Task B: never started its loop — nothing counts.
        _activate("B", ["coder"], at=14.0),
    ]
    usage = skill_usage_from_events(events)
    assert usage == {
        "coder": SkillUsage(count=2, last_used_at=13.0),
        "reviewer": SkillUsage(count=1, last_used_at=13.0),
    }


def test_interleaved_task_streams_are_judged_per_task() -> None:
    events = [
        _event("A", "ContextPlanComposed", at=1.0),
        _activate("B", ["early"], at=2.0),  # B's loop has not started
        _event("B", "ContextPlanComposed", at=3.0),
        _activate("A", ["x"], at=4.0),
        _activate("B", ["x"], at=5.0),
    ]
    usage = skill_usage_from_events(events)
    assert usage == {"x": SkillUsage(count=2, last_used_at=5.0)}


def test_ignores_patches_without_activations_and_odd_payloads() -> None:
    events = [
        _event("A", "ContextPlanComposed", at=1.0),
        _event("A", "TaskStatePatched", at=2.0, patch={"set_phase": "work"}),
        EventEnvelope.build(
            task_id="A", type="TaskStatePatched", payload={"patch": {"activate_skills": ["dict-form"]}}, occurred_at=3.0
        ),
        EventEnvelope.build(task_id="A", type="TaskStatePatched", payload=None, occurred_at=4.0),
    ]
    assert skill_usage_from_events(events) == {
        "dict-form": SkillUsage(count=1, last_used_at=3.0)
    }


def test_decay_halves_per_week_and_floors_at_a_tenth() -> None:
    fresh = SkillUsage(count=4, last_used_at=100 * DAY)
    assert decayed_usage_score(fresh, now=100 * DAY) == pytest.approx(4.0)
    assert decayed_usage_score(fresh, now=107 * DAY) == pytest.approx(2.0)
    assert decayed_usage_score(fresh, now=114 * DAY) == pytest.approx(1.0)
    # Long-idle: never below count × 0.1.
    assert decayed_usage_score(fresh, now=1000 * DAY) == pytest.approx(0.4)
    # A clock behind the last use decays nothing.
    assert decayed_usage_score(fresh, now=0.0) == pytest.approx(4.0)


def test_rank_is_a_plain_score_map_ready_for_menu_rank() -> None:
    usage = {
        "old-favourite": SkillUsage(count=30, last_used_at=0.0),
        "recent": SkillUsage(count=2, last_used_at=99 * DAY),
    }
    rank = rank_skills_by_usage(usage, now=100 * DAY)
    assert set(rank) == {"old-favourite", "recent"}
    assert rank["old-favourite"] == pytest.approx(3.0)  # 30 × floor 0.1
    assert rank["recent"] == pytest.approx(2 * 0.5 ** (1 / 7))
    assert rank_skills_by_usage({}, now=0.0) == {}


def test_a_scalar_activate_skills_payload_counts_nothing() -> None:
    """A malformed bare-string ``activate_skills`` is not iterated character
    by character into phantom skill names."""
    events = [
        _event("A", "ContextPlanComposed", at=1.0),
        _event("A", "TaskStatePatched", at=2.0, patch={"activate_skills": "coder"}),
        _event("A", "TaskStatePatched", at=3.0, patch={"activate_skills": None}),
        _event("A", "TaskStatePatched", at=4.0, patch={"activate_skills": 7}),
    ]
    assert skill_usage_from_events(events) == {}
