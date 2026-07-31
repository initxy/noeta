"""fold projects the ``StepTransitionMarked`` tag onto ``RuntimeState``.

The event is the source of record; ``last_transition`` is the O(1) read the
anti-spiral guard depends on. Last write wins, and an unknown ``reason`` is
projected verbatim rather than rejected — a producer that emits a reason this
build does not know must not make the recording unfoldable.
"""

from __future__ import annotations

from noeta.core.fold import fold
from noeta.protocols.events import (
    StepTransitionMarkedPayload,
    TaskCreatedPayload,
)
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


def _make_runtime():
    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    return log, cs


def test_fold_projects_reason_onto_last_transition() -> None:
    log, cs = _make_runtime()
    log.emit(
        task_id="t1",
        type="StepTransitionMarked",
        payload=StepTransitionMarkedPayload(reason="approval_resume"),
    )
    assert fold(log, cs, "t1").runtime.last_transition == "approval_resume"


def test_fold_takes_last_of_multiple_marks() -> None:
    """Last write wins: the guard reads the most recent transition."""
    log, cs = _make_runtime()
    for reason in ("overflow_recovery", "approval_resume", "transient_retry"):
        log.emit(
            task_id="t1",
            type="StepTransitionMarked",
            payload=StepTransitionMarkedPayload(reason=reason),
        )
    assert fold(log, cs, "t1").runtime.last_transition == "transient_retry"


def test_fold_without_any_mark_keeps_none() -> None:
    """A recording with no ``StepTransitionMarked`` folds to ``None``: the
    tag is optional, so its absence is not an error."""
    log, cs = _make_runtime()
    assert fold(log, cs, "t1").runtime.last_transition is None


def test_fold_tolerates_unknown_reason() -> None:
    """An unknown reason must not crash fold (warning-not-fatal, mirroring
    fold's unknown-event-type policy). The raw value is still projected so a
    reader can see the drift."""
    log, cs = _make_runtime()
    log.emit(
        task_id="t1",
        type="StepTransitionMarked",
        payload=StepTransitionMarkedPayload(reason="something_new_in_v2"),
    )
    rebuilt = fold(log, cs, "t1")
    assert rebuilt.runtime.last_transition == "something_new_in_v2"


def test_snapshot_accelerated_fold_matches_from_scratch() -> None:
    """The snapshot-accelerated fold and the from-scratch fold must land
    the same ``last_transition`` (the snapshot body carries the field)."""
    log, cs = _make_runtime()
    log.emit(
        task_id="t1",
        type="StepTransitionMarked",
        payload=StepTransitionMarkedPayload(reason="compaction_retry"),
    )
    from_scratch = fold(log, cs, "t1", ignore_snapshots=True)
    accelerated = fold(log, cs, "t1")
    assert (
        from_scratch.runtime.last_transition
        == accelerated.runtime.last_transition
        == "compaction_retry"
    )
