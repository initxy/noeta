"""End-to-end smoke for the issue 18 built-in Guards backed by the
Phase 1 sqlite persistent stack (SqliteEventLog + SqliteDispatcher,
InMemoryContentStore for now since issue 16 doesn't auto-wire).

A single integration scenario: BudgetGuard with ``max_tool_calls=1``
denies the second tool call in a batch and the denial is durable on
the sqlite-backed EventLog. Verifies that the fold-side accumulators
read the same numbers as the live ``finished.governance``.
"""

from __future__ import annotations

from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.hooks import HookManager
from noeta.guards.budget import Budget, BudgetGuard
from noeta.guards.permission import PermissionGuard, PermissionPolicy
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import (
    FinishDecision,
    ToolCall,
    ToolCallsDecision,
)
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import InMemoryContentStore
from noeta.storage.sqlite.dispatcher import SqliteDispatcher
from noeta.storage.sqlite.eventlog import SqliteEventLog
from noeta.testing.composer import trivial_three_segment
from noeta.tools.fake import FakeTool


def test_budget_and_permission_guards_work_against_sqlite_stack(tmp_path) -> None:
    db = tmp_path / "noeta.db"
    log = SqliteEventLog(db)
    cs = InMemoryContentStore()
    disp = SqliteDispatcher(db)
    log.bind_lease_registry(disp)

    try:
        tool_alpha = FakeTool(name="alpha", script={("k",): "ok"})
        tool_beta = FakeTool(name="beta", script={("k",): "no"})

        hooks = HookManager()
        hooks.register(BudgetGuard(Budget(max_tool_calls=1)))
        hooks.register(
            PermissionGuard(
                PermissionPolicy(denied_tools=frozenset({"beta"})),
                tools={"alpha": tool_alpha, "beta": tool_beta},
            )
        )

        policy = StubScriptedPolicy(
            [
                ToolCallsDecision(
                    calls=[
                        ToolCall(tool_name="alpha", arguments={"k": "k"}, call_id="c1"),
                        ToolCall(tool_name="beta", arguments={"k": "k"}, call_id="c2"),
                        ToolCall(tool_name="alpha", arguments={"k": "k"}, call_id="c3"),
                    ]
                ),
                FinishDecision(answer="done"),
            ]
        )

        runtime = ToolRuntime(event_log=log, content_store=cs)
        engine = Engine(
            event_log=log,
            content_store=cs,
            composer=trivial_three_segment(cs),
            policy=policy,
            hooks=hooks,
            tools={"alpha": tool_alpha, "beta": tool_beta},
            tool_runtime=runtime,
        )

        task = engine.create_task(goal="g", policy_name="scripted")
        disp.enqueue(task.task_id)
        lease = disp.lease(worker_id="w")
        assert lease is not None
        finished = engine.run_one_step(task, lease_id=lease.lease_id)
        assert finished.status == "terminal"

        types = [e.type for e in log.read(task.task_id)]
        # alpha c1 → goes through (1st tool call, BudgetGuard allows).
        # beta c2  → PermissionGuard denies.
        # alpha c3 → BudgetGuard sees tool_calls=1, denies.
        assert types.count("ToolCallStarted") == 1
        assert types.count("ToolCallDenied") == 2

        # Fold-side governance must agree with the live finished task.
        rebuilt = fold(log, cs, task.task_id)
        assert rebuilt.governance.tool_calls == finished.governance.tool_calls == 1
        # Two denied records on the live task and on the rebuilt one.
        assert len(finished.governance.denied) == 2
        assert len(rebuilt.governance.denied) == 2
    finally:
        disp.close()
        log.close()
