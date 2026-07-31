"""``SubtaskCompleted`` and ``SubtaskResult`` as typed value objects.

A subtask wake matches by identity projection, so ``subtask_id`` alone
carries the identity of the condition a parent stored: the result a child
hands back is informational payload and must never decide whether the
parent recognises its own wake. ``SubtaskResult`` holds both terminal
outcomes in one shape, populating only the field its status implies.
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
