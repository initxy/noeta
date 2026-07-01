"""WakeCondition + SubtaskResult typed value objects.

Issue 03 introduces typed wake conditions so the Dispatcher can match
"wake event vs what the task is waiting on" by shape rather than by
arbitrary equality. Phase 0 only ships ``SubtaskCompleted`` (the other
three condition variants land with issue 05 / Phase 1).
"""

from __future__ import annotations

from noeta.protocols.wake import SubtaskCompleted, SubtaskResult


def test_subtask_completed_equality_by_subtask_id() -> None:
    a = SubtaskCompleted(subtask_id="t-child-1")
    b = SubtaskCompleted(subtask_id="t-child-1")
    c = SubtaskCompleted(subtask_id="t-child-2")

    assert a == b
    assert a != c


def test_subtask_result_completed_carries_output() -> None:
    r = SubtaskResult(status="completed", output={"answer": 42})

    assert r.status == "completed"
    assert r.output == {"answer": 42}
    assert r.error is None


def test_subtask_result_failed_carries_error() -> None:
    r = SubtaskResult(status="failed", error="upstream blew up")

    assert r.status == "failed"
    assert r.error == "upstream blew up"
    assert r.output is None
