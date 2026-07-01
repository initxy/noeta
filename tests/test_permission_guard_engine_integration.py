"""Engine-integration tests for ``PermissionGuard`` (issue 18)."""

from __future__ import annotations

from noeta.core.engine import Engine
from noeta.core.hooks import HookManager
from noeta.guards.permission import PermissionGuard, PermissionPolicy
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


def _build(*, policy, hooks, tools=None):
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
        hooks=hooks,
        tools=tools,
        tool_runtime=runtime,
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w")
    assert lease is not None
    return engine, log, lease.lease_id, task


def test_permission_guard_blocks_disallowed_tool_emits_tool_call_denied() -> None:
    tools = {"echo": FakeTool(name="echo", script={("k",): "ok"})}
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(tool_name="echo", arguments={"k": "k"}, call_id="c1")
                ]
            ),
            FinishDecision(answer="all-denied"),
        ]
    )
    hooks = HookManager()
    hooks.register(
        PermissionGuard(
            PermissionPolicy(denied_tools=frozenset({"echo"})),
            tools=tools,
        )
    )

    engine, log, lease_id, task = _build(policy=policy, hooks=hooks, tools=tools)
    engine.run_one_step(task, lease_id=lease_id)

    types = [e.type for e in log.read(task.task_id)]
    assert "ToolCallDenied" in types
    assert "ToolCallStarted" not in types  # never actually invoked


def test_permission_guard_fail_closed_unknown_tool_does_not_crash_engine() -> None:
    """B4 Engine-level regression: an unknown tool name combined with
    a PermissionGuard that has ``max_risk_level`` configured must
    surface as ``ToolCallDenied`` rather than letting the Engine
    fall through to ``_resolve_tool`` and raise ``KeyError``."""
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(
                        tool_name="mystery", arguments={}, call_id="cx"
                    )
                ]
            ),
            FinishDecision(answer="cleanup"),
        ]
    )
    hooks = HookManager()
    # tools dict is empty + max_risk_level set → fail-closed DENY path
    # must fire before Engine ever asks ToolRuntime for "mystery".
    hooks.register(
        PermissionGuard(PermissionPolicy(max_risk_level="low"), tools={}),
    )

    engine, log, lease_id, task = _build(policy=policy, hooks=hooks, tools={})
    # The key assertion is "does not raise".
    finished = engine.run_one_step(task, lease_id=lease_id)
    assert finished.status == "terminal"

    types = [e.type for e in log.read(task.task_id)]
    assert "ToolCallDenied" in types
    assert "ToolCallStarted" not in types
    denied = next(
        e for e in log.read(task.task_id) if e.type == "ToolCallDenied"
    )
    assert "fail-closed" in (denied.payload.reason or "") or "no metadata" in (
        denied.payload.reason or ""
    )


def test_permission_guard_blocks_disallowed_subtask_agent() -> None:
    policy = StubScriptedPolicy(
        [SpawnSubtaskDecision(agent_name="hacker", goal="g", inputs={})]
    )
    hooks = HookManager()
    hooks.register(
        PermissionGuard(
            PermissionPolicy(allowed_subtask_agents=frozenset({"writer"})),
            tools={},
        )
    )
    engine, log, lease_id, task = _build(policy=policy, hooks=hooks)
    finished = engine.run_one_step(task, lease_id=lease_id)
    assert finished.status == "terminal"
    types = [e.type for e in log.read(task.task_id)]
    assert "SubtaskDenied" in types
    assert "TaskFailed" in types
