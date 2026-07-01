"""Behavior smokes for the CLI default profile (issue 23 rev3 B2).

The :file:`test_cli_profile.py` suite already asserts wiring *shape*
(observer / guard types, ``Engine._hooks`` is bound). These tests
prove the wiring is **load-bearing** by driving Engine through real
scenarios:

* PermissionGuard deny-all → ``ToolCallDenied`` event lands in the
  EventLog.
* ChildLifecycleObserver wired by ``wire_default_observers`` →
  spawning a subtask emits ``SubtaskSpawned``, enqueues the child on
  the dispatcher, and wakes the parent when the child terminates.
"""

from __future__ import annotations

from typing import Any

import pytest

from noeta.testing.profile import (
    build_runtime,
    build_tools,
    default_budget,
    default_permission_policy,
)
from noeta.guards.permission import PermissionPolicy
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
    """Stand-in LLM provider so profile.build_runtime's RuntimeLLMClient
    does not need a real backend. Returns a trivial end_turn response
    when polled (rarely reached because tests swap Engine._policy)."""

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
# B2 — PermissionGuard deny-all smoke
# ---------------------------------------------------------------------------


def test_permission_guard_deny_all_emits_tool_call_denied() -> None:
    """PermissionGuard wired in profile must actually intercept the
    decision and write a ``ToolCallDenied`` envelope when the policy
    denies every tool."""
    deny_all = PermissionPolicy(
        allowed_tools=frozenset(),  # nothing allowed
        denied_tools=frozenset({"echo"}),
        max_risk_level=None,
        allowed_subtask_agents=None,
    )
    bundle = _build(permission_policy=deny_all)
    try:
        # Swap the wired ReActPolicy for a script that requests the
        # denied tool. Engine then runs Guard → records ToolCallDenied
        # because PermissionGuard returns deny.
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
# B2 — ChildLifecycleObserver smoke (spawn-subtask → enqueue + wake)
# ---------------------------------------------------------------------------


def test_wire_default_observers_full_parent_child_lifecycle() -> None:
    """``wire_default_observers`` registers ChildLifecycleObserver,
    which must implement the full parent ↔ child handoff (rev2 B1):

    * On ``TaskCreated(parent_task_id=...)`` enqueue the child on
      the dispatcher.
    * On child terminal (``TaskCompleted``) emit ``SubtaskCompleted``
      on the parent stream and **wake** the parent so it returns to
      the dispatcher's ready queue.
    """
    perm = PermissionPolicy(
        allowed_tools=frozenset({"echo"}),
        denied_tools=frozenset(),
        max_risk_level=None,
        allowed_subtask_agents=frozenset({"helper"}),
    )
    bundle = _build(permission_policy=perm)
    try:
        # Phase 1 — parent spawns subtask, suspends waiting_subtask
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

        # Phase 2 — child should now be in the dispatcher's ready queue.
        child_lease = bundle.dispatcher.lease(worker_id="t", lease_seconds=60.0)
        assert child_lease is not None, "ChildLifecycleObserver did not enqueue child"
        assert child_lease.task_id == child_task_id

        # Phase 3 — drive the child Engine to TaskCompleted. The Observer
        # subscribes to TaskCompleted events and on the child's terminal
        # emits SubtaskCompleted on the parent stream + wakes parent.
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

        # Phase 4 — verify the observer's post-terminal path ran:
        # 4a) Parent stream contains a SubtaskCompleted cross-stream event.
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

        # 4b) Parent is back on the ready queue (wake fired).
        wake_lease = bundle.dispatcher.lease(worker_id="t", lease_seconds=60.0)
        assert wake_lease is not None, "parent was not woken back to ready"
        assert wake_lease.task_id == parent_task.task_id
        bundle.dispatcher.release(wake_lease.lease_id, next_state="terminal")
    finally:
        bundle.shutdown()
