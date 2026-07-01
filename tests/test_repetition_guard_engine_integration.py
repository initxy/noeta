"""Engine-integration tests for ``RepetitionGuard`` (work item ④).

Wires ``RepetitionGuard`` onto a real Engine + InMemory storage and verifies
the Engine folds the recorded ``ToolCallStarted`` history into
``GuardContext.recent_tool_calls`` so a run of identical ``(tool_name,
arguments)`` calls trips the configured action (deny → ``ToolCallDenied``;
require_approval → approval suspend), while a default-off session behaves
exactly as today.

Determinism / replay: the guard reads only the recorded EventLog prefix, so the
same recording with the same-parameter guard reproduces the same guard-origin
events. The last test pins identical re-folds yielding identical verdicts.
"""

from __future__ import annotations

from noeta.core.engine import Engine
from noeta.core.hooks import HookManager
from noeta.guards.repetition import RepetitionGuard, RepetitionPolicy
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import (
    FinishDecision,
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


def _identical_calls(n: int) -> list[ToolCall]:
    return [
        ToolCall(tool_name="echo", arguments={"k": "loop"}, call_id=f"c{i}")
        for i in range(n)
    ]


def test_repetition_guard_denies_third_identical_call() -> None:
    """``threshold=3`` lets the first two identical calls run and denies the
    third (proposed + 2 prior = 3 consecutive)."""
    tool = FakeTool(name="echo", script={("loop",): "out"})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(calls=_identical_calls(3)),
            FinishDecision(answer="done"),
        ]
    )
    hooks = HookManager()
    hooks.register(
        RepetitionGuard(RepetitionPolicy(threshold=3, action="deny"))
    )

    engine, log, lease_id, task = _build(
        policy=policy, hooks=hooks, tools={"echo": tool}
    )
    finished = engine.run_one_step(task, lease_id=lease_id)
    assert finished.status == "terminal"

    types = [e.type for e in log.read(task.task_id)]
    assert types.count("ToolCallStarted") == 2
    assert types.count("ToolCallDenied") == 1
    denied = next(
        e for e in log.read(task.task_id) if e.type == "ToolCallDenied"
    )
    assert "repetition" in denied.payload.reason.lower() or "echo" in (
        denied.payload.reason
    )


def test_repetition_guard_require_approval_suspends() -> None:
    """Default action ``require_approval`` routes the tripping call through the
    HITL suspend path (TaskSuspended + ToolCallApprovalRequested)."""
    tool = FakeTool(name="echo", script={("loop",): "out"})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(calls=_identical_calls(3)),
            FinishDecision(answer="done"),
        ]
    )
    hooks = HookManager()
    hooks.register(RepetitionGuard(RepetitionPolicy(threshold=3)))

    engine, log, lease_id, task = _build(
        policy=policy, hooks=hooks, tools={"echo": tool}
    )
    finished = engine.run_one_step(task, lease_id=lease_id)

    types = [e.type for e in log.read(task.task_id)]
    # The first two run; the third trips → approval suspend, not a deny.
    assert types.count("ToolCallStarted") == 2
    assert "ToolCallApprovalRequested" in types
    assert "TaskSuspended" in types
    assert finished.status == "suspended"


def test_repetition_guard_off_by_default_unaffected() -> None:
    """No RepetitionGuard registered → identical calls all run as today."""
    tool = FakeTool(name="echo", script={("loop",): "out"})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(calls=_identical_calls(3)),
            FinishDecision(answer="done"),
        ]
    )

    engine, log, lease_id, task = _build(
        policy=policy, tools={"echo": tool}
    )
    engine.run_one_step(task, lease_id=lease_id)

    types = [e.type for e in log.read(task.task_id)]
    assert types.count("ToolCallStarted") == 3
    assert "ToolCallDenied" not in types


def test_repetition_guard_distinct_args_never_trips() -> None:
    """Three calls with distinct arguments are not a repeat run."""
    tool = FakeTool(
        name="echo",
        script={("a",): "oa", ("b",): "ob", ("c",): "oc"},
    )
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(
                        tool_name="echo", arguments={"k": x}, call_id=f"c{x}"
                    )
                    for x in ("a", "b", "c")
                ]
            ),
            FinishDecision(answer="done"),
        ]
    )
    hooks = HookManager()
    hooks.register(
        RepetitionGuard(RepetitionPolicy(threshold=2, action="deny"))
    )

    engine, log, lease_id, task = _build(
        policy=policy, hooks=hooks, tools={"echo": tool}
    )
    engine.run_one_step(task, lease_id=lease_id)

    types = [e.type for e in log.read(task.task_id)]
    assert types.count("ToolCallStarted") == 3
    assert "ToolCallDenied" not in types


def test_engine_fills_recent_tool_calls_deterministically() -> None:
    """The Engine's ``_guard`` builds ``recent_tool_calls`` from the recorded
    ToolCallStarted prefix; folding the same recording twice yields the same
    history (replay determinism foundation)."""
    from noeta.core.engine import _recent_tool_calls
    from noeta.protocols.canonical import to_canonical_bytes

    tool = FakeTool(name="echo", script={("loop",): "out"})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(calls=_identical_calls(2)),
            FinishDecision(answer="done"),
        ]
    )
    engine, log, lease_id, task = _build(
        policy=policy, tools={"echo": tool}
    )
    engine.run_one_step(task, lease_id=lease_id)

    cs = InMemoryContentStore()  # not used by the recorded inline args path
    events = log.read(task.task_id)
    first = _recent_tool_calls(events, engine._content_store, window=8)
    second = _recent_tool_calls(events, engine._content_store, window=8)
    assert first == second
    # Two identical echo calls were recorded.
    expected_key = ("echo", to_canonical_bytes({"k": "loop"}))
    assert first.count(expected_key) == 2
