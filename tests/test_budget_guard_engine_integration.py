"""Engine-integration tests for ``BudgetGuard`` (issue 18).

Wires BudgetGuard onto a real Engine + InMemory storage and verifies
the deny path materialises as ``ToolCallDenied`` / ``SubtaskDenied``
/ ``TaskFailed`` events on the live EventLog.
"""

from __future__ import annotations

from noeta.core.engine import Engine
from noeta.core.hooks import HookManager
from noeta.guards.budget import Budget, BudgetGuard
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import (
    FinishDecision,
    SpawnSubtaskDecision,
    ToolCall,
    ToolCallsDecision,
)
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment
from noeta.tools.fake import FakeTool


def _build(*, policy, hooks=None, tools=None):
    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    disp = InMemoryDispatcher()
    log.bind_lease_registry(disp)
    tools = tools or {}
    runtime = ToolRuntime(event_log=log, content_store=cs)
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=policy,
        hooks=hooks or HookManager(),
        tools=tools,
        tool_runtime=runtime,
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w")
    assert lease is not None
    return engine, log, lease.lease_id, task


def test_budget_guard_blocks_third_tool_call_after_max_tool_calls_two() -> None:
    """``max_tool_calls=2`` should let the first two tool calls run
    through and deny the third."""
    tool = FakeTool(name="echo", script={("a",): "out-a", ("b",): "out-b", ("c",): "out-c"})

    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(tool_name="echo", arguments={"k": "a"}, call_id="c1"),
                    ToolCall(tool_name="echo", arguments={"k": "b"}, call_id="c2"),
                    ToolCall(tool_name="echo", arguments={"k": "c"}, call_id="c3"),
                ],
            ),
            FinishDecision(answer="done"),
        ]
    )

    hooks = HookManager()
    hooks.register(BudgetGuard(Budget(max_tool_calls=2)))

    engine, log, lease_id, task = _build(
        policy=policy, hooks=hooks, tools={"echo": tool}
    )
    finished = engine.run_one_step(task, lease_id=lease_id)
    assert finished.status == "terminal"

    types = [e.type for e in log.read(task.task_id)]
    # We expect: two successful tool calls + one denial in the same
    # batch, then the FinishDecision lands.
    assert types.count("ToolCallStarted") == 2
    assert types.count("ToolCallDenied") == 1
    denied = next(
        e for e in log.read(task.task_id) if e.type == "ToolCallDenied"
    )
    assert "max_tool_calls" in denied.payload.reason

    # Structural invariant: a denied tool call must STILL leave a matching
    # tool_result in the history, or the next provider request would carry a
    # dangling function call (the real-LLM symptom: a fatal 400). Every
    # proposed call (c1/c2 run, c3 denied) gets a result block; c3's is a
    # failed result carrying the deny reason.
    results = {
        b.call_id: b
        for m in finished.runtime.messages
        if m.role == "tool"
        for b in m.content
    }
    assert {"c1", "c2", "c3"} <= set(results)
    assert results["c3"].success is False
    assert "max_tool_calls" in (results["c3"].error or "")


def test_budget_guard_blocks_subtask_spawn_when_max_spawned_reached() -> None:
    policy = StubScriptedPolicy(
        [
            SpawnSubtaskDecision(agent_name="child", goal="g1", inputs={}),
        ]
    )
    hooks = HookManager()
    # max_spawned_subtasks=0 ensures even the first spawn is denied,
    # since g.spawned_subtasks=0 >= 0 is True (consumption cap >= semantics).
    hooks.register(BudgetGuard(Budget(max_spawned_subtasks=0)))

    engine, log, lease_id, task = _build(policy=policy, hooks=hooks)
    finished = engine.run_one_step(task, lease_id=lease_id)
    # Spawn was denied → SubtaskDenied + TaskFailed
    assert finished.status == "terminal"
    types = [e.type for e in log.read(task.task_id)]
    assert "SubtaskDenied" in types
    assert "TaskFailed" in types


def test_budget_guard_blocks_finish_when_iterations_exceeded() -> None:
    """``max_iterations`` uses strict ``>`` semantics: a single-step
    finish requires ``max_iterations >= 1`` to avoid tripping."""
    policy = StubScriptedPolicy([FinishDecision(answer="done")])
    hooks = HookManager()
    # max_iterations=0 means ``g.iterations=1 > 0`` → DENY at finish.
    # finish-denied routes through ``_fail`` and emits TaskFailed.
    hooks.register(BudgetGuard(Budget(max_iterations=0)))

    engine, log, lease_id, task = _build(policy=policy, hooks=hooks)
    finished = engine.run_one_step(task, lease_id=lease_id)
    assert finished.status == "terminal"
    types = [e.type for e in log.read(task.task_id)]
    assert "TaskFailed" in types
    assert "TaskCompleted" not in types
    # finish-denied DOES NOT add to the denied list (only ToolCallDenied
    # / SubtaskDenied / TaskCancelled do).
    assert finished.governance.denied == []
