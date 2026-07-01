"""B7: fold must ignore pre-issue-18 snapshots and refold from scratch.

A snapshot body that pre-dates issue 18 has no ``spawned_subtasks``
key in its ``governance`` dict, and its other counter fields are only
the default zeros (the pre-18 fold didn't accumulate them). If fold
trusts that snapshot to accelerate, BudgetGuard reads stale counters
and B1's fold-from-EventLog mitigation is defeated. The B7 guard
detects the missing sentinel and falls back to from-scratch fold.
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
    """Write a snapshot body styled the way pre-issue-18 code would
    have produced it: ``governance`` dict carries only the old four
    fields (no ``spawned_subtasks``) and they're all zero, regardless
    of what events the prefix contains."""
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
        # Crucially, no ``spawned_subtasks`` key — the B7 sentinel.
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
    # More events after the legacy snapshot.
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

    # The accelerated path must recover the full prefix counts because
    # the legacy snapshot is rejected. Equality on the four key fields
    # is enough to prove the legacy snapshot was ignored.
    for field in ("iterations", "tool_calls", "cost_usd", "spawned_subtasks"):
        assert getattr(accelerated.governance, field) == getattr(
            from_scratch.governance, field
        ), field
    # The from-scratch fold should reflect 3 plans, 3 tool starts, 1
    # spawn, and the $0.25 LLM cost from before the legacy snapshot.
    assert from_scratch.governance.iterations == 3
    assert from_scratch.governance.tool_calls == 3
    assert from_scratch.governance.spawned_subtasks == 1
    assert abs(from_scratch.governance.cost_usd - 0.25) < 1e-9
    assert abs(accelerated.governance.cost_usd - 0.25) < 1e-9
