"""Engine + Guard integration: the three action points run real Guards
and respond to the three Verdict outcomes.

These tests cover the issue 05 acceptance criteria as observable
behaviour on EventLog + Task status — never on HookManager call counts
or any internal dispatch path.
"""

from __future__ import annotations

from typing import Any

from noeta.testing.composer import trivial_three_segment
from noeta.core.engine import Engine
from noeta.core.hooks import HookManager
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import (
    FinishDecision,
    SpawnSubtaskDecision,
    ToolCall,
    ToolCallsDecision,
)
from noeta.protocols.hooks import (
    GuardContext,
    ProposedAction,
    ProposedFinish,
    ProposedSpawnSubtask,
    ProposedToolCall,
    VerdictResult,
)
from noeta.protocols.wake import HumanResponseReceived
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.tools.fake import FakeTool


# ---------------------------------------------------------------------------
# Guard fixtures
# ---------------------------------------------------------------------------


class _DenyOnFooGuard:
    """Denies any tool_call whose tool_name is exactly 'foo'."""

    name = "deny-on-foo"
    priority = 10

    def check(
        self, action: ProposedAction, ctx: GuardContext  # noqa: ARG002
    ) -> VerdictResult:
        if isinstance(action, ProposedToolCall) and action.call.tool_name == "foo":
            return VerdictResult.deny("foo is forbidden")
        return VerdictResult.allow()


class _RequireApprovalOnBarGuard:
    """Demands human approval before any tool named 'bar' runs."""

    name = "approve-bar"
    priority = 10

    def check(
        self, action: ProposedAction, ctx: GuardContext  # noqa: ARG002
    ) -> VerdictResult:
        if isinstance(action, ProposedToolCall) and action.call.tool_name == "bar":
            return VerdictResult.require_approval("bar needs human approval")
        return VerdictResult.allow()


class _DenyAllSubtasks:
    name = "deny-subtask"
    priority = 10

    def check(
        self, action: ProposedAction, ctx: GuardContext  # noqa: ARG002
    ) -> VerdictResult:
        if isinstance(action, ProposedSpawnSubtask):
            return VerdictResult.deny("subtasks are forbidden")
        return VerdictResult.allow()


class _RequireApprovalOnSubtask:
    name = "approve-subtask"
    priority = 10

    def check(
        self, action: ProposedAction, ctx: GuardContext  # noqa: ARG002
    ) -> VerdictResult:
        if isinstance(action, ProposedSpawnSubtask):
            return VerdictResult.require_approval("spawning needs approval")
        return VerdictResult.allow()


class _DenyFinishGuard:
    name = "deny-finish"
    priority = 10

    def check(
        self, action: ProposedAction, ctx: GuardContext  # noqa: ARG002
    ) -> VerdictResult:
        if isinstance(action, ProposedFinish):
            return VerdictResult.deny("not done yet")
        return VerdictResult.allow()


class _RequireApprovalOnFinish:
    name = "approve-finish"
    priority = 10

    def check(
        self, action: ProposedAction, ctx: GuardContext  # noqa: ARG002
    ) -> VerdictResult:
        if isinstance(action, ProposedFinish):
            return VerdictResult.require_approval("review before finish")
        return VerdictResult.allow()


# ---------------------------------------------------------------------------
# Engine wiring helpers
# ---------------------------------------------------------------------------


def _build_engine(
    *,
    policy: object,
    tools: dict[str, Any] | None = None,
    hooks: HookManager | None = None,
) -> tuple[Engine, InMemoryEventLog, InMemoryContentStore, str, Any]:
    content_store = InMemoryContentStore()
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    wire_default_observers(event_log, dispatcher)
    composer = trivial_three_segment(content_store)
    tool_runtime = ToolRuntime(
        event_log=event_log, content_store=content_store
    )

    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=policy,
        tools=tools or {},
        tool_runtime=tool_runtime,
        hooks=hooks,
    )

    task = engine.create_task(goal="guard-test", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w-test")
    assert lease is not None
    return engine, event_log, content_store, lease.lease_id, task


# ---------------------------------------------------------------------------
# before_tool_call: deny
# ---------------------------------------------------------------------------


def test_tool_call_deny_appends_tool_call_denied_and_skips_call() -> None:
    foo = FakeTool(name="foo", script={("x",): "should-not-run"})
    safe = FakeTool(name="safe", script={("y",): "out-safe"})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(tool_name="foo", arguments={"k": "x"}, call_id="c-foo"),
                    ToolCall(tool_name="safe", arguments={"k": "y"}, call_id="c-safe"),
                ],
            ),
            FinishDecision(answer="done"),
        ]
    )
    hooks = HookManager()
    hooks.register(_DenyOnFooGuard())

    engine, log, _cs, lease_id, task = _build_engine(
        policy=policy, tools={"foo": foo, "safe": safe}, hooks=hooks
    )
    result = engine.run_one_step(task, lease_id=lease_id)

    assert result.status == "terminal"
    types = [e.type for e in log.read(task.task_id)]
    assert "ToolCallDenied" in types
    # No tool envelope for foo (no Started/Result/Finished for it).
    starts = [
        e for e in log.read(task.task_id) if e.type == "ToolCallStarted"
    ]
    assert {s.payload.call_id for s in starts} == {"c-safe"}


def test_tool_call_denied_payload_carries_call_id_and_reason() -> None:
    foo = FakeTool(name="foo", script={("x",): "x"})
    safe = FakeTool(name="safe", script={("y",): "y"})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(tool_name="foo", arguments={"k": "x"}, call_id="c-foo"),
                    ToolCall(tool_name="safe", arguments={"k": "y"}, call_id="c-safe"),
                ],
            ),
            FinishDecision(answer="done"),
        ]
    )
    hooks = HookManager()
    hooks.register(_DenyOnFooGuard())

    engine, log, _cs, lease_id, task = _build_engine(
        policy=policy, tools={"foo": foo, "safe": safe}, hooks=hooks
    )
    engine.run_one_step(task, lease_id=lease_id)

    denied = [
        e for e in log.read(task.task_id) if e.type == "ToolCallDenied"
    ]
    assert len(denied) == 1
    assert denied[0].payload.call_id == "c-foo"
    assert denied[0].payload.tool_name == "foo"
    assert "forbidden" in denied[0].payload.reason


def test_tool_call_deny_still_lets_other_calls_in_batch_run() -> None:
    foo = FakeTool(name="foo", script={("x",): "x-out"})
    safe = FakeTool(name="safe", script={("y",): "safe-out"})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(tool_name="foo", arguments={"k": "x"}, call_id="c-foo"),
                    ToolCall(tool_name="safe", arguments={"k": "y"}, call_id="c-safe"),
                ],
            ),
            FinishDecision(answer="done"),
        ]
    )
    hooks = HookManager()
    hooks.register(_DenyOnFooGuard())

    engine, log, cs, lease_id, task = _build_engine(
        policy=policy, tools={"foo": foo, "safe": safe}, hooks=hooks
    )
    finished = engine.run_one_step(task, lease_id=lease_id)
    # The whole batch travels on one role="tool" message.
    tool_messages = [
        m for m in finished.runtime.messages if m.role == "tool"
    ]
    assert len(tool_messages) == 1
    # BOTH calls leave a result block so the history stays balanced — the
    # safe call's real output, and the denied call's FAILED result. Without
    # the latter the assistant's c-foo tool_use would be dangling and the
    # next provider request would 400.
    blocks = tool_messages[0].content
    by_id = {b.call_id: b for b in blocks}
    assert set(by_id) == {"c-foo", "c-safe"}
    assert by_id["c-safe"].success is True
    assert by_id["c-foo"].success is False
    assert "forbidden" in (by_id["c-foo"].error or "")
    # Only the actually-invoked tool records a ToolResultRecorded in the
    # ContentStore; the denied call's block is synthetic feedback, not an
    # invocation.
    recorded = [
        e for e in log.read(task.task_id) if e.type == "ToolResultRecorded"
    ]
    assert len(recorded) == 1
    body = cs.get(recorded[0].payload.output_ref)
    assert b"safe-out" in body


# ---------------------------------------------------------------------------
# before_tool_call: require_approval
# ---------------------------------------------------------------------------


def test_tool_call_require_approval_suspends_with_human_wake_on() -> None:
    bar = FakeTool(name="bar", script={("x",): "out"})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(
                        tool_name="bar", arguments={"k": "x"}, call_id="c-bar"
                    ),
                ],
            ),
        ]
    )
    hooks = HookManager()
    hooks.register(_RequireApprovalOnBarGuard())

    engine, log, _cs, lease_id, task = _build_engine(
        policy=policy, tools={"bar": bar}, hooks=hooks
    )
    result = engine.run_one_step(task, lease_id=lease_id)

    assert result.status == "suspended"
    types = [e.type for e in log.read(task.task_id)]
    # Snapshot precedes Suspended on the same stream.
    assert types.index("TaskSnapshot") < types.index("TaskSuspended")

    suspended = [
        e for e in log.read(task.task_id) if e.type == "TaskSuspended"
    ][0]
    assert suspended.payload.reason == "waiting_human"
    assert isinstance(suspended.payload.wake_on, HumanResponseReceived)
    assert suspended.payload.wake_on.handle  # non-empty handle


def test_tool_call_require_approval_does_not_run_the_tool() -> None:
    bar = FakeTool(name="bar", script={("x",): "out"})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(
                        tool_name="bar", arguments={"k": "x"}, call_id="c-bar"
                    ),
                ],
            ),
        ]
    )
    hooks = HookManager()
    hooks.register(_RequireApprovalOnBarGuard())

    engine, log, _cs, lease_id, task = _build_engine(
        policy=policy, tools={"bar": bar}, hooks=hooks
    )
    engine.run_one_step(task, lease_id=lease_id)

    types = [e.type for e in log.read(task.task_id)]
    # The pending tool never produced a ToolCallStarted/Result/Finished.
    assert "ToolCallStarted" not in types
    assert "ToolResultRecorded" not in types


# ---------------------------------------------------------------------------
# before_spawn_subtask: deny / require_approval
# ---------------------------------------------------------------------------


def test_spawn_subtask_deny_fails_parent_and_appends_subtask_denied() -> None:
    policy = StubScriptedPolicy(
        [
            SpawnSubtaskDecision(
                agent_name="child", goal="do thing", inputs={"k": "v"}
            ),
        ]
    )
    hooks = HookManager()
    hooks.register(_DenyAllSubtasks())

    engine, log, _cs, lease_id, task = _build_engine(
        policy=policy, hooks=hooks
    )
    result = engine.run_one_step(task, lease_id=lease_id)

    assert result.status == "terminal"
    types = [e.type for e in log.read(task.task_id)]
    assert "SubtaskDenied" in types
    assert "TaskFailed" in types
    # SubtaskSpawned MUST NOT appear — the child was never created.
    assert "SubtaskSpawned" not in types


def test_spawn_subtask_denied_payload_carries_decision_and_reason() -> None:
    policy = StubScriptedPolicy(
        [
            SpawnSubtaskDecision(
                agent_name="child", goal="do thing", inputs={"k": "v"}
            ),
        ]
    )
    hooks = HookManager()
    hooks.register(_DenyAllSubtasks())

    engine, log, _cs, lease_id, task = _build_engine(
        policy=policy, hooks=hooks
    )
    engine.run_one_step(task, lease_id=lease_id)

    denied = [
        e for e in log.read(task.task_id) if e.type == "SubtaskDenied"
    ][0]
    assert denied.payload.agent_name == "child"
    assert denied.payload.goal == "do thing"
    assert "forbidden" in denied.payload.reason

    failed = [
        e for e in log.read(task.task_id) if e.type == "TaskFailed"
    ][0]
    assert "subtask denied" in failed.payload.reason


def test_spawn_subtask_require_approval_suspends_for_human() -> None:
    policy = StubScriptedPolicy(
        [
            SpawnSubtaskDecision(
                agent_name="child", goal="do thing", inputs={"k": "v"}
            ),
        ]
    )
    hooks = HookManager()
    hooks.register(_RequireApprovalOnSubtask())

    engine, log, _cs, lease_id, task = _build_engine(
        policy=policy, hooks=hooks
    )
    result = engine.run_one_step(task, lease_id=lease_id)

    assert result.status == "suspended"
    suspended = [
        e for e in log.read(task.task_id) if e.type == "TaskSuspended"
    ][0]
    assert suspended.payload.reason == "waiting_human"
    assert isinstance(suspended.payload.wake_on, HumanResponseReceived)
    # Child was NOT bootstrapped.
    types = [e.type for e in log.read(task.task_id)]
    assert "SubtaskSpawned" not in types


# ---------------------------------------------------------------------------
# before_finish: deny / require_approval
# ---------------------------------------------------------------------------


def test_finish_deny_fails_the_task() -> None:
    policy = StubScriptedPolicy([FinishDecision(answer="done")])
    hooks = HookManager()
    hooks.register(_DenyFinishGuard())

    engine, log, _cs, lease_id, task = _build_engine(
        policy=policy, hooks=hooks
    )
    result = engine.run_one_step(task, lease_id=lease_id)

    assert result.status == "terminal"
    types = [e.type for e in log.read(task.task_id)]
    assert "TaskCompleted" not in types
    assert "TaskFailed" in types
    failed = [
        e for e in log.read(task.task_id) if e.type == "TaskFailed"
    ][0]
    assert "not done yet" in failed.payload.reason


def test_finish_require_approval_suspends_not_terminal() -> None:
    """Per issue 05: require_approval on finish goes to suspended,
    NOT terminal — the human still has to approve."""
    policy = StubScriptedPolicy([FinishDecision(answer="answer-pending")])
    hooks = HookManager()
    hooks.register(_RequireApprovalOnFinish())

    engine, log, _cs, lease_id, task = _build_engine(
        policy=policy, hooks=hooks
    )
    result = engine.run_one_step(task, lease_id=lease_id)

    assert result.status == "suspended"
    types = [e.type for e in log.read(task.task_id)]
    # No terminal events; one TaskSuspended.
    assert "TaskCompleted" not in types
    assert "TaskFailed" not in types
    assert types.count("TaskSuspended") == 1

    suspended = [
        e for e in log.read(task.task_id) if e.type == "TaskSuspended"
    ][0]
    assert suspended.payload.reason == "waiting_human"
    assert isinstance(suspended.payload.wake_on, HumanResponseReceived)


def test_finish_allow_path_still_terminates_normally() -> None:
    """Regression guard: the allow case must not have changed."""
    policy = StubScriptedPolicy([FinishDecision(answer="done")])
    # Empty HookManager + no guards = pure ALLOW.
    hooks = HookManager()

    engine, log, _cs, lease_id, task = _build_engine(
        policy=policy, hooks=hooks
    )
    result = engine.run_one_step(task, lease_id=lease_id)
    assert result.status == "terminal"
    types = [e.type for e in log.read(task.task_id)]
    assert types.count("TaskCompleted") == 1
    assert "TaskFailed" not in types


# ---------------------------------------------------------------------------
# fold / snapshot round-trip for the new HumanResponseReceived wake_on
# ---------------------------------------------------------------------------


def test_fold_after_require_approval_restores_human_wake_on() -> None:
    """A suspended-on-approval Task must survive a fold so that a worker
    re-leasing it sees the same ``HumanResponseReceived`` wake_on.

    The serialize/rehydrate path for ``HumanResponseReceived`` is the
    only new wake-condition shape Phase 0 ships; without round-trip
    coverage, a serialization bug would only surface in Phase 2 HITL.
    """
    from noeta.core.fold import fold

    policy = StubScriptedPolicy([FinishDecision(answer="answer-pending")])
    hooks = HookManager()
    hooks.register(_RequireApprovalOnFinish())

    engine, log, cs, lease_id, task = _build_engine(
        policy=policy, hooks=hooks
    )
    suspended = engine.run_one_step(task, lease_id=lease_id)
    assert suspended.status == "suspended"

    rebuilt = fold(log, cs, task.task_id)
    assert rebuilt.status == "suspended"
    assert isinstance(rebuilt.wake_on, HumanResponseReceived)
    assert rebuilt.wake_on.handle == suspended.wake_on.handle


def test_fold_after_require_approval_byte_equal_without_snapshot() -> None:
    """The from-scratch fold path (ignoring the snapshot) must rebuild
    the same suspended Task. Snapshots are an optimisation only."""
    from noeta.core.fold import fold

    policy = StubScriptedPolicy([FinishDecision(answer="answer-pending")])
    hooks = HookManager()
    hooks.register(_RequireApprovalOnFinish())

    engine, log, cs, lease_id, task = _build_engine(
        policy=policy, hooks=hooks
    )
    suspended = engine.run_one_step(task, lease_id=lease_id)

    rebuilt = fold(log, cs, task.task_id, ignore_snapshots=True)
    assert rebuilt == suspended


# ---------------------------------------------------------------------------
# Direct yield_for_human Decision (allow-path; no Guard involvement)
# ---------------------------------------------------------------------------


def test_yield_for_human_decision_suspends_on_human_response() -> None:
    """A Policy that directly returns ``yield_for_human`` shares the
    same exit path as approval-required Verdicts — both end on the
    ``HumanResponseReceived`` wake condition (no separate
    approval-event type)."""
    from noeta.protocols.decisions import YieldForHumanDecision

    policy = StubScriptedPolicy(
        [YieldForHumanDecision(prompt="need-clarification")]
    )
    hooks = HookManager()

    engine, log, _cs, lease_id, task = _build_engine(
        policy=policy, hooks=hooks
    )
    result = engine.run_one_step(task, lease_id=lease_id)

    assert result.status == "suspended"
    suspended = [
        e for e in log.read(task.task_id) if e.type == "TaskSuspended"
    ][0]
    assert suspended.payload.reason == "waiting_human"
    assert isinstance(suspended.payload.wake_on, HumanResponseReceived)
    # The decision's prompt is reused as the wake handle so a downstream
    # HITL channel can correlate the question with the response.
    assert suspended.payload.wake_on.handle == "need-clarification"
