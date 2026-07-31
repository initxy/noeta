"""fold rejects a snapshot body that lacks the governance sentinel.

``spawned_subtasks`` is the schema sentinel for a ``governance`` dict whose
counters were accumulated; a body without that key carries only default
zeros. Accelerating from such a body would hand BudgetGuard undercounted
totals and let a task spend past its budget, so fold detects the missing key
and refolds the whole event log instead.
"""

from __future__ import annotations

from noeta.core.fold import fold
from noeta.core.snapshot import snapshot_media_type
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.events import (
    ContextPlanComposedPayload,
    LLMRequestFinishedPayload,
    SubtaskSpawnedPayload,
    TaskCreatedPayload,
    TaskSnapshotPayload,
    ToolCallStartedPayload,
)
from noeta.protocols.values import ContentRef
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


def _seed_prefix(log, cs):
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    plan_ref = ContentRef(hash="p" * 64, size=4, media_type="application/json")
    cs.put(b"plan-body", media_type="application/json")
    for _ in range(2):
        log.emit(
            task_id="t1",
            type="ContextPlanComposed",
            payload=ContextPlanComposedPayload(plan_ref=plan_ref),
        )
        log.emit(
            task_id="t1",
            type="ToolCallStarted",
            payload=ToolCallStartedPayload(
                call_id="c", tool_name="echo", arguments={}
            ),
        )
    log.emit(
        task_id="t1",
        type="SubtaskSpawned",
        payload=SubtaskSpawnedPayload(
            subtask_id="c0", agent_name="child", goal="g"
        ),
    )
    log.emit(
        task_id="t1",
        type="LLMRequestFinished",
        payload=LLMRequestFinishedPayload(
            call_id="L1", success=True, cost_usd=0.25
        ),
    )


def _write_pre18_snapshot(log, cs):
    """A snapshot body missing the sentinel: the ``governance`` dict holds
    zeros and no ``spawned_subtasks`` key, regardless of what the event
    prefix actually contains."""
    legacy_state = {
        "task_id": "t1",
        "status": "running",
        "parent_task_id": None,
        "runtime": {"messages": [], "usage": {}},
        "state": {
            "goal": "g",
            "phase": None,
            "todos": [],
            "decisions": [],
            "next_action": None,
            "active_skills": [],
        },
        "context": {"plan_ref": None},
        # No ``spawned_subtasks`` key — the sentinel this guard keys on.
        "governance": {
            "cost_usd": 0.0,
            "tool_calls": 0,
            "iterations": 0,
            "denied": [],
            "subtask_results": [],
        },
        "wake_on": None,
    }
    body = to_canonical_bytes(legacy_state)
    ref = cs.put(body, media_type=snapshot_media_type())
    log.emit(
        task_id="t1",
        type="TaskSnapshot",
        payload=TaskSnapshotPayload(state_ref=ref),
    )


def test_legacy_pre18_snapshot_is_ignored_by_fold() -> None:
    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    _seed_prefix(log, cs)
    _write_pre18_snapshot(log, cs)
    # Events after the snapshot, so the accelerated path has a tail to scan
    # and only the rejected prefix distinguishes the two folds.
    plan_ref = ContentRef(hash="q" * 64, size=4, media_type="application/json")
    log.emit(
        task_id="t1",
        type="ContextPlanComposed",
        payload=ContextPlanComposedPayload(plan_ref=plan_ref),
    )
    log.emit(
        task_id="t1",
        type="ToolCallStarted",
        payload=ToolCallStartedPayload(
            call_id="c-post", tool_name="echo", arguments={}
        ),
    )

    accelerated = fold(log, cs, "t1")
    from_scratch = fold(log, cs, "t1", ignore_snapshots=True)

    # The accelerated path can only recover the full prefix counts if it
    # rejected the snapshot; equality on these four fields proves it did.
    for field in ("iterations", "tool_calls", "cost_usd", "spawned_subtasks"):
        assert getattr(accelerated.governance, field) == getattr(
            from_scratch.governance, field
        ), field
    # The seeded prefix plus the tail: 3 plans, 3 tool starts, 1 spawn and
    # the $0.25 LLM cost.
    assert from_scratch.governance.iterations == 3
    assert from_scratch.governance.tool_calls == 3
    assert from_scratch.governance.spawned_subtasks == 1
    assert abs(from_scratch.governance.cost_usd - 0.25) < 1e-9
    assert abs(accelerated.governance.cost_usd - 0.25) < 1e-9
