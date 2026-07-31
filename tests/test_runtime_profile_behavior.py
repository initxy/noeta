"""The default profile's guards and observers are load-bearing, not just present.

Shape assertions — which guard and observer types got registered — still pass
when the wiring is inert, so these drive a real Engine through the two paths
that would go quiet: a denied tool call must reach the EventLog as
``ToolCallDenied``, and a spawned subtask must be enqueued on the dispatcher and
wake its parent when it terminates.
"""

from __future__ import annotations

from typing import Any


from noeta.testing.profile import (
    build_runtime,
    build_tools,
    default_budget,
    default_permission_policy,
)
from noeta.runtime.governance import PermissionPolicy
from noeta.core.fold import fold
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import (
    FinishDecision,
    SpawnSubtaskDecision,
    ToolCall,
    ToolCallsDecision,
)
from noeta.protocols.messages import LLMRequest, LLMResponse, TextBlock, Usage


class _StubProvider:
    """Stand-in so ``build_runtime`` has a provider to wire; rarely polled,
    because each test swaps ``Engine._policy`` for a scripted one."""

    def complete(self, request: LLMRequest) -> LLMResponse:  # noqa: ARG002
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="ok")],
            usage=Usage(),
        )


def _build(
    *, permission_policy: PermissionPolicy | None = None
) -> Any:
    return build_runtime(
        provider=_StubProvider(),
        model="test-model",
        system_prompt="You are a helpful assistant.",
        tools=build_tools(),
        sqlite_path=":memory:",
        sse_broadcaster=None,
        max_steps=5,
        permission_policy=permission_policy or default_permission_policy(),
        budget=default_budget(),
    )


# ---------------------------------------------------------------------------
# PermissionGuard deny-all
# ---------------------------------------------------------------------------


def test_permission_guard_deny_all_emits_tool_call_denied() -> None:
    """The guard must intercept the decision and write a durable
    ``ToolCallDenied`` envelope, not merely refuse in memory."""
    deny_all = PermissionPolicy(
        allowed_tools=frozenset(),  # nothing allowed
        denied_tools=frozenset({"echo"}),
        max_risk_level=None,
        allowed_subtask_agents=None,
    )
    bundle = _build(permission_policy=deny_all)
    try:
        # A script that requests the denied tool, so the Engine reaches the
        # guard with something the policy has to refuse.
        bundle.engine._policy = StubScriptedPolicy(  # type: ignore[attr-defined]
            [
                ToolCallsDecision(
                    calls=[ToolCall(tool_name="echo", arguments={"text": "hi"}, call_id="c-1")]
                ),
                FinishDecision(answer="done"),
            ]
        )

        task = bundle.engine.create_task(goal="try denied tool", policy_name="stub")
        bundle.dispatcher.enqueue(task.task_id)
        lease = bundle.dispatcher.lease(worker_id="t", lease_seconds=60.0)
        assert lease is not None
        bundle.engine.run_one_step(task, lease_id=lease.lease_id)
        bundle.dispatcher.release(lease.lease_id, next_state=task.status)

        events = bundle.event_log.read(task.task_id)
        types = [e.type for e in events]
        assert "ToolCallDenied" in types, (
            f"expected ToolCallDenied; got {types}"
        )
    finally:
        bundle.shutdown()


# ---------------------------------------------------------------------------
# ChildLifecycleObserver — spawn-subtask → enqueue child → wake parent
# ---------------------------------------------------------------------------


def test_wire_default_observers_full_parent_child_lifecycle() -> None:
    """The observer owns the whole parent-child handoff, and half of it is
    invisible from either side alone: it enqueues the child on
    ``TaskCreated(parent_task_id=...)``, and on the child's terminal it emits
    ``SubtaskCompleted`` on the *parent* stream and wakes the parent back onto
    the ready queue. A parent that is never woken hangs forever.
    """
    perm = PermissionPolicy(
        allowed_tools=frozenset({"echo"}),
        denied_tools=frozenset(),
        max_risk_level=None,
        allowed_subtask_agents=frozenset({"helper"}),
    )
    bundle = _build(permission_policy=perm)
    try:
        # Parent spawns the subtask and suspends on waiting_subtask.
        bundle.engine._policy = StubScriptedPolicy(  # type: ignore[attr-defined]
            [SpawnSubtaskDecision(agent_name="helper", goal="sub-job")]
        )
        parent_task = bundle.engine.create_task(goal="parent", policy_name="stub")
        bundle.dispatcher.enqueue(parent_task.task_id)
        parent_lease = bundle.dispatcher.lease(worker_id="t", lease_seconds=60.0)
        assert parent_lease is not None
        parent_after = bundle.engine.run_one_step(
            parent_task, lease_id=parent_lease.lease_id
        )
        bundle.dispatcher.release(
            parent_lease.lease_id,
            next_state=parent_after.status,
            wake_on=parent_after.wake_on,
        )
        assert parent_after.status == "suspended"

        parent_events = bundle.event_log.read(parent_task.task_id)
        types = [e.type for e in parent_events]
        assert "SubtaskSpawned" in types
        spawned = next(e for e in parent_events if e.type == "SubtaskSpawned")
        child_task_id = spawned.payload.subtask_id

        # The child must be on the dispatcher's ready queue by now.
        child_lease = bundle.dispatcher.lease(worker_id="t", lease_seconds=60.0)
        assert child_lease is not None, "ChildLifecycleObserver did not enqueue child"
        assert child_lease.task_id == child_task_id

        # Drive the child to TaskCompleted — the event the observer reacts to.
        child_task = fold(bundle.event_log, bundle.content_store, child_task_id)
        bundle.engine._policy = StubScriptedPolicy(  # type: ignore[attr-defined]
            [FinishDecision(answer="child done")]
        )
        child_after = bundle.engine.run_one_step(
            child_task, lease_id=child_lease.lease_id
        )
        bundle.dispatcher.release(
            child_lease.lease_id,
            next_state=child_after.status,
        )
        assert child_after.status == "terminal"

        # The observer's post-terminal path: a cross-stream event on the
        # parent, then the parent wake.
        parent_events_after = bundle.event_log.read(parent_task.task_id)
        parent_types_after = [e.type for e in parent_events_after]
        assert "SubtaskCompleted" in parent_types_after, (
            f"ChildLifecycleObserver did not emit SubtaskCompleted; "
            f"parent stream types: {parent_types_after}"
        )
        subtask_completed = next(
            e for e in parent_events_after if e.type == "SubtaskCompleted"
        )
        assert subtask_completed.payload.subtask_id == child_task_id

        # Parent is back on the ready queue.
        wake_lease = bundle.dispatcher.lease(worker_id="t", lease_seconds=60.0)
        assert wake_lease is not None, "parent was not woken back to ready"
        assert wake_lease.task_id == parent_task.task_id
        bundle.dispatcher.release(wake_lease.lease_id, next_state="terminal")
    finally:
        bundle.shutdown()
