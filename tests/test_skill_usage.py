"""``noeta.sdk`` usage scoring for the skill menu — the ledger is the counter.

A host folds a tenant's event streams into per-skill usage and scores it with
Claude Code's decay to build the ``menu_rank`` the ``skills`` pack keeps its
roster order by. Noeta records nothing new for this: only activations after a
task's loop started count, so host preloads never rank themselves first.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import pytest

from noeta.client.skill_usage import SkillUsageRanker
from noeta.protocols.event_log import TaskStreamSummary
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


# ---------------------------------------------------------------------------
# SkillUsageRanker — the host's store-wide default
# ---------------------------------------------------------------------------


class _Store:
    """Task streams listed most-recent first, counting every read."""

    def __init__(self, streams: dict[str, list[EventEnvelope]]) -> None:
        self.streams = streams
        self.reads: list[str] = []
        self.fail = False

    def list_task_streams(self) -> list[TaskStreamSummary]:
        if self.fail:
            raise OSError("store down")
        return [
            TaskStreamSummary(task_id=tid, last_seq=len(events), last_event_time=events[-1].occurred_at)
            for tid, events in self.streams.items()
        ]

    def read(self, task_id: str, *, after_seq=None) -> list[EventEnvelope]:
        self.reads.append(task_id)
        return self.streams[task_id]

    def find_latest_snapshot(self, task_id: str):
        return None


def _inline(fold: Callable[[], None]) -> None:
    fold()


def _used(task_id: str, name: str, *, at: float) -> list[EventEnvelope]:
    return [
        _event(task_id, "ContextPlanComposed", at=at),
        _activate(task_id, [name], at=at + 1),
    ]


def test_ranker_folds_only_the_most_recent_streams_once_per_ttl() -> None:
    now = {"t": 30 * DAY}
    store = _Store(
        {
            "newest": _used("newest", "coder", at=29 * DAY),
            "newer": _used("newer", "reviewer", at=28 * DAY),
            "oldest": _used("oldest", "archivist", at=1 * DAY),
        }
    )
    ranker = SkillUsageRanker(
        store, clock=lambda: now["t"], max_streams=2, ttl_s=600, spawn=_inline
    )

    rank = ranker.rank()
    assert set(rank) == {"coder", "reviewer"}
    assert rank["coder"] > rank["reviewer"]
    assert store.reads == ["newest", "newer"]

    # Within the TTL every ask answers from the snapshot: no read at all.
    for _ in range(5):
        now["t"] += 60
        assert ranker.rank() == rank
    assert store.reads == ["newest", "newer"]

    # Past it, the next ask folds again and sees new use.
    store.streams = {"latest": _used("latest", "archivist", at=now["t"]), **store.streams}
    now["t"] += 600
    assert "archivist" in ranker.rank()
    assert store.reads[2:] == ["latest", "newest"]


def test_ranker_keeps_the_previous_snapshot_when_the_store_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = {"t": 10 * DAY}
    store = _Store({"a": _used("a", "coder", at=9 * DAY)})
    ranker = SkillUsageRanker(store, clock=lambda: now["t"], ttl_s=600, spawn=_inline)
    rank = ranker.rank()
    assert set(rank) == {"coder"}

    store.fail = True
    now["t"] += 601
    assert ranker.rank() == rank
    assert "skill usage fold failed" in caplog.text
    # The failed fold still counts as the fold of this TTL.
    now["t"] += 60
    assert ranker.rank() == rank
    assert caplog.text.count("skill usage fold failed") == 1



def test_ranker_never_folds_on_the_asking_thread() -> None:
    """An ask answers from the snapshot at once; a due fold runs elsewhere and
    the asks after it see its result."""
    now = {"t": 10 * DAY}
    store = _Store({"a": _used("a", "coder", at=9 * DAY)})
    started: list[Callable[[], None]] = []
    ranker = SkillUsageRanker(
        store, clock=lambda: now["t"], ttl_s=600, spawn=started.append
    )

    assert ranker.rank() == {}
    assert store.reads == []
    # A fold already running is not started twice, even past the TTL.
    now["t"] += 601
    assert ranker.rank() == {}
    assert len(started) == 1

    started[0]()
    assert ranker.wait_idle(0)
    # The fold landed; this ask is past the TTL too, so it queues the next.
    assert set(ranker.rank()) == {"coder"}
    assert len(started) == 2


def test_ranker_default_spawn_folds_in_the_background() -> None:
    store = _Store({"a": _used("a", "coder", at=time.time())})
    ranker = SkillUsageRanker(store)
    ranker.rank()
    assert ranker.wait_idle(5)
    assert set(ranker.rank()) == {"coder"}


def test_ranker_reads_only_the_fold_event_types_when_the_log_can_narrow() -> None:
    events = [
        _event("a", "TaskCreated", at=9 * DAY),
        *_used("a", "coder", at=9 * DAY),
    ]

    class _Narrowing(_Store):
        asked: list[tuple[str, ...]] = []

        def read_event_types(self, task_id, event_types):
            self.asked.append(tuple(event_types))
            return [e for e in self.streams[task_id] if e.type in event_types]

    store = _Narrowing({"a": events})
    ranker = SkillUsageRanker(store, clock=lambda: 10 * DAY, spawn=_inline)
    assert set(ranker.rank()) == {"coder"}
    assert store.reads == []
    assert store.asked == [("ContextPlanComposed", "TaskStatePatched")]


def test_ranker_folds_on_a_reader_of_its_own_and_closes_it(tmp_path) -> None:
    """Over a sqlite file the fold reads through a separate read-only
    connection, never the adapter's shared one, and closes it after."""
    from noeta.builtins.storage.impl.sqlite.eventlog import SqliteEventLog

    log = SqliteEventLog(tmp_path / "store.sqlite")
    from noeta.protocols.events import ContextPlanComposedPayload

    log.emit(
        task_id="a", type="ContextPlanComposed", payload=ContextPlanComposedPayload()
    )
    log.emit(
        task_id="a",
        type="TaskStatePatched",
        payload=TaskStatePatchedPayload(patch={"activate_skills": ["coder"]}),
    )
    opened: list[Any] = []
    real_open = log.open_reader

    def open_reader() -> Any:
        reader = real_open()
        opened.append(reader)
        return reader

    log.open_reader = open_reader  # type: ignore[method-assign]
    log.read = log.read_event_types = None  # type: ignore[assignment,method-assign]
    ranker = SkillUsageRanker(log, spawn=_inline)
    assert set(ranker.rank()) == {"coder"}
    assert len(opened) == 1
    with pytest.raises(Exception):
        opened[0].read("a")  # closed after the fold


def test_ranker_reads_through_the_log_when_its_reader_fails_to_open() -> None:
    class _NoReader(_Store):
        def open_reader(self):
            raise OSError("cannot open")

    store = _NoReader({"a": _used("a", "coder", at=9 * DAY)})
    ranker = SkillUsageRanker(store, clock=lambda: 10 * DAY, spawn=_inline)
    assert set(ranker.rank()) == {"coder"}
    assert store.reads == ["a"]
