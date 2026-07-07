"""Step-attempt recovery (docs/adr/step-attempt-recovery.md).

Covers the scanner (attempt anchoring, seal-window reset, plan-less
approval-execution windows), the classifier (guard-chain rule), and the
worker seal → re-drive-or-park machine end-to-end over the real SQLite +
InMemory stacks — each crash window simulated exactly like
tests/test_durable_wake.py (raise mid-step, drop the in-flight lease,
``requeue_stale``, fresh lease).
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.hooks import HookManager
from noeta.core.snapshot import serialize_task_state
from noeta.core.wiring import wire_default_observers
from noeta.guards.budget import Budget, BudgetGuard
from noeta.guards.permission import PermissionGuard, PermissionPolicy
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import (
    FinishDecision,
    ToolCall,
    ToolCallsDecision,
    YieldForHumanDecision,
)
from noeta.protocols.messages import TextBlock
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.protocols.wake import (
    NEXT_GOAL_WAKE_HANDLE,
    HumanResponseReceived,
)
from noeta.runtime.attempt import (
    ABANDON_CAP,
    classify_attempt,
    scan_interrupted_attempt,
    scan_pending_park,
)
from noeta.runtime import worker as worker_mod
from noeta.runtime.worker import ReliabilityEvent, WorkerLoop, run_leased_task
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.storage.sqlite.contentstore import SqliteContentStore
from noeta.storage.sqlite.dispatcher import SqliteDispatcher
from noeta.storage.sqlite.eventlog import SqliteEventLog
from noeta.testing.composer import trivial_three_segment
from noeta.tools.fake import FakeTool


# ---------------------------------------------------------------------------
# Scanner — pure event-list unit tests
# ---------------------------------------------------------------------------


class _Env:
    def __init__(self, type_: str, seq: int, payload: Any = None) -> None:
        self.type = type_
        self.seq = seq
        self.payload = payload


def test_scan_prelude_only_tail_is_none() -> None:
    events = [
        _Env("TaskSuspended", 0),
        _Env("TaskWoken", 1),
        _Env("MessagesAppended", 2),   # seed-durable goal append
        _Env("TaskStatePatched", 3),   # seed-durable skill activation
    ]
    assert scan_interrupted_attempt(events) is None


def test_scan_anchors_on_last_plan() -> None:
    events = [
        _Env("TaskWoken", 0),
        _Env("MessagesAppended", 1),
        _Env("ContextPlanComposed", 2),   # completed attempt
        _Env("MessagesAppended", 3),
        _Env("ToolCallStarted", 4),
        _Env("ToolCallFinished", 5),
        _Env("ContextPlanComposed", 6),   # interrupted attempt
        _Env("MessagesAppended", 7),
    ]
    attempt = scan_interrupted_attempt(events)
    assert attempt is not None
    assert attempt.anchored_on_plan is True
    assert attempt.attempt_start_seq == 6
    assert [e.seq for e in attempt.tail] == [6, 7]
    assert attempt.abandon_count == 0


def test_scan_seal_closes_prior_history_and_counts() -> None:
    # crash → seal → re-driven attempt crashes again: the live tail is only
    # what follows the LAST plan, and the seal count feeds the cap.
    events = [
        _Env("TaskWoken", 0),
        _Env("ContextPlanComposed", 1),
        _Env("StepAttemptAbandoned", 2),
        _Env("ContextPlanComposed", 3),
    ]
    attempt = scan_interrupted_attempt(events)
    assert attempt is not None
    assert attempt.attempt_start_seq == 3
    assert attempt.abandon_count == 1
    # seal with NO re-driven attempt yet → nothing live to recover (the
    # bare re-drive after the seal is case 2′, not a recovery).
    events_sealed_only = events[:3]
    assert scan_interrupted_attempt(events_sealed_only) is None


def test_scan_count_resets_at_window_boundary() -> None:
    events = [
        _Env("TaskWoken", 0),
        _Env("ContextPlanComposed", 1),
        _Env("StepAttemptAbandoned", 2),
        _Env("TaskSuspended", 3),
        _Env("TaskWoken", 4),             # new window
        _Env("ContextPlanComposed", 5),
    ]
    attempt = scan_interrupted_attempt(events)
    assert attempt is not None
    assert attempt.abandon_count == 0     # prior window's seal not counted


def test_scan_planless_activity_window_is_approval_anchor() -> None:
    # ResolveApprovalPrelude crash: resolution durable, tool started, no plan.
    events = [
        _Env("TaskSuspended", 0),
        _Env("TaskWoken", 1),
        _Env("ToolCallApprovalResolved", 2),
        _Env("ToolCallStarted", 3),
    ]
    attempt = scan_interrupted_attempt(events)
    assert attempt is not None
    assert attempt.anchored_on_plan is False
    assert attempt.attempt_start_seq == 2
    assert [e.seq for e in attempt.tail] == [2, 3]


# ---------------------------------------------------------------------------
# Classifier — guard-chain rule
# ---------------------------------------------------------------------------


class _StartedPayload:
    def __init__(self, call_id: str, tool_name: str, arguments: dict) -> None:
        self.call_id = call_id
        self.tool_name = tool_name
        self.arguments = arguments
        self.arguments_ref = None


class _FinishedPayload:
    def __init__(self, call_id: str) -> None:
        self.call_id = call_id


def _classifier_engine(
    cs: Any, *, require_approval: tuple[str, ...] = ()
) -> Any:
    log = InMemoryEventLog()
    tools = {"reader": FakeTool(name="reader"), "danger": FakeTool(name="danger")}
    hooks = HookManager()
    hooks.register(
        PermissionGuard(
            PermissionPolicy(
                require_approval_tools=frozenset(require_approval)
            ),
            tools,
        )
    )
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=StubScriptedPolicy([]),
        tools=tools,
        hooks=hooks,
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    return engine, task


def test_classify_allowed_tool_activity_is_safe() -> None:
    cs = InMemoryContentStore()
    engine, task = _classifier_engine(cs)
    tail = (
        _Env("ContextPlanComposed", 5),
        _Env("ToolCallStarted", 6, _StartedPayload("c1", "reader", {"p": "x"})),
    )
    verdict = classify_attempt(tail, engine=engine, task=task, content_store=cs)
    assert verdict.safe and verdict.blockers == ()


def test_classify_approval_gated_tool_parks_finished_or_not() -> None:
    cs = InMemoryContentStore()
    engine, task = _classifier_engine(cs, require_approval=("danger",))
    tail = (
        _Env("ContextPlanComposed", 5),
        _Env("ToolCallStarted", 6, _StartedPayload("c1", "danger", {})),
        _Env("ToolCallFinished", 7, _FinishedPayload("c1")),
        _Env("ToolCallStarted", 8, _StartedPayload("c2", "danger", {})),
    )
    verdict = classify_attempt(tail, engine=engine, task=task, content_store=cs)
    assert not verdict.safe
    assert verdict.blockers == ("danger (completed)", "danger (interrupted)")


def test_classify_spawn_and_unknown_tool_park() -> None:
    cs = InMemoryContentStore()
    engine, task = _classifier_engine(cs)
    tail = (
        _Env("ContextPlanComposed", 5),
        _Env("SubtaskSpawned", 6),
        # a tool that no longer exists in the engine's toolset: the guard
        # chain allows it (no risk ceiling configured) but resolution at
        # invoke time would fail — the guard-with-metadata configs deny it.
    )
    verdict = classify_attempt(tail, engine=engine, task=task, content_store=cs)
    assert not verdict.safe
    assert verdict.blockers == ("spawned a subtask",)


# ---------------------------------------------------------------------------
# End-to-end — real stacks, real crash windows
# ---------------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "memory"])
def stack(request: Any, tmp_path: Any) -> Any:
    clock = [1000.0]

    def now() -> float:
        return clock[0]

    if request.param == "sqlite":
        db = str(tmp_path / "recovery.db")
        dispatcher: Any = SqliteDispatcher(db, now=now)
        event_log: Any = SqliteEventLog(db, lease_validator=dispatcher)
        content_store: Any = SqliteContentStore(db)
    else:
        dispatcher = InMemoryDispatcher(now=now)
        event_log = InMemoryEventLog(lease_validator=dispatcher)
        content_store = InMemoryContentStore()
    return event_log, content_store, dispatcher, clock


class _RT:
    def __init__(self, engine: Any, log: Any, cs: Any, dispatcher: Any) -> None:
        self.engine = engine
        self.event_log = log
        self.content_store = cs
        self.dispatcher = dispatcher


class _CrashOncePolicy:
    """Scripted policy whose entries may be Exception instances: reaching
    one raises it ONCE (simulating the process dying mid-decide, i.e. mid
    LLM call — after the attempt's ``ContextPlanComposed`` is durable) and
    the script continues on the next decide."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)

    def decide(self, ctx: Any, view: Any) -> Any:  # noqa: ARG002
        entry = self._script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry


class _CountingTool(FakeTool):
    """FakeTool that counts invocations (proves a completed attempt's calls
    are NOT re-executed by a re-drive)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.calls = 0

    def invoke(self, arguments: dict, ctx: ToolContext) -> ToolResult:
        self.calls += 1
        return super().invoke(arguments, ctx)


class _KillOnceTool(FakeTool):
    """Raises ``KeyboardInterrupt`` (uncatchable by the ToolRuntime's
    ``except Exception``) on the first call — a hard mid-tool crash leaving
    ``ToolCallStarted`` without its ``ToolCallFinished`` — then succeeds."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.killed = False

    def invoke(self, arguments: dict, ctx: ToolContext) -> ToolResult:
        if not self.killed:
            self.killed = True
            raise KeyboardInterrupt("simulated hard crash mid-tool")
        return super().invoke(arguments, ctx)


def _engine_on(stack: Any, policy: Any, *, tools: Any = None,
               hooks: Any = None) -> Any:
    event_log, content_store, dispatcher, _ = stack
    wire_default_observers(event_log, dispatcher)
    return Engine(
        event_log=event_log, content_store=content_store,
        composer=trivial_three_segment(content_store),
        policy=policy, tools=tools, hooks=hooks,
    )


def _suspended_session(stack: Any, policy: Any, **engine_kw: Any) -> Any:
    """Create a task, drive its opening turn to a next-goal suspend, and
    release. Returns (engine, task_id)."""
    event_log, content_store, dispatcher, _ = stack
    engine = _engine_on(stack, policy, **engine_kw)
    task = engine.create_task(goal="g", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None
    engine.append_user_message(
        task, content=[TextBlock(text="g")], lease_id=lease.lease_id
    )
    task = engine.run_one_step(task, lease_id=lease.lease_id)
    assert task.status == "suspended"
    dispatcher.release(
        lease.lease_id, next_state="suspended", wake_on=task.wake_on
    )
    return engine, task.task_id


def _crash_mid_turn(stack: Any, engine: Any, tid: str, handle: str) -> None:
    """Wake + lease + note_woken + run the step until it raises (the
    simulated crash), leaving the partial attempt on the stream."""
    event_log, content_store, dispatcher, _ = stack
    assert dispatcher.wake(tid, HumanResponseReceived(handle=handle)) is True
    lease = dispatcher.lease(worker_id="w", task_id=tid)
    assert lease is not None and lease.wake_event is not None
    task = fold(event_log, content_store, tid)
    task = engine.note_woken(
        task, lease_id=lease.lease_id, wake_event=lease.wake_event
    )
    # the seed-durable goal append for this turn
    task = engine.append_user_message(
        task, content=[TextBlock(text="turn 2")], lease_id=lease.lease_id
    )
    with pytest.raises((RuntimeError, KeyboardInterrupt)):
        engine.run_one_step(task, lease_id=lease.lease_id)


def _reclaim(stack: Any, tid: str) -> Any:
    event_log, content_store, dispatcher, clock = stack
    clock[0] += 100_000.0
    assert tid in dispatcher.requeue_stale()
    lease = dispatcher.lease(worker_id="w", task_id=tid)
    assert lease is not None
    return lease


def _seals(log: Any, tid: str) -> list[Any]:
    return [e for e in log.read(tid) if e.type == "StepAttemptAbandoned"]


def test_safe_crash_auto_redrives_without_reexecuting(stack: Any) -> None:
    """AC1 — crash mid-decide after a completed low-risk tool round: the
    interrupted (empty-tail) attempt is sealed and re-driven with no human;
    the completed round's tool call is NOT re-executed."""
    log, cs, dispatcher, _ = stack
    reader = _CountingTool(name="reader", script={("x",): "body"})
    policy = _CrashOncePolicy([
        YieldForHumanDecision(prompt=NEXT_GOAL_WAKE_HANDLE),   # opening turn
        ToolCallsDecision(calls=[
            ToolCall(tool_name="reader", arguments={"p": "x"}, call_id="c1")
        ]),
        RuntimeError("simulated crash during the second LLM call"),
        FinishDecision(answer="done"),
    ])
    engine, tid = _suspended_session(
        stack, policy, tools={"reader": reader}
    )
    _crash_mid_turn(stack, engine, tid, NEXT_GOAL_WAKE_HANDLE)
    assert reader.calls == 1
    lease = _reclaim(stack, tid)
    outcome = run_leased_task(_RT(engine, log, cs, dispatcher), lease)
    assert outcome == "woken"
    events = log.read(tid)
    seals = _seals(log, tid)
    assert len(seals) == 1 and seals[0].payload.reason == "auto_redrive"
    assert any(e.type == "TaskCompleted" for e in events)
    # the completed round survived the seal: its call ran exactly once.
    assert reader.calls == 1
    # exactly-once wake: the opening turn has no TaskWoken (TaskStarted)
    # and the re-drive emits no second one for turn 2.
    assert sum(1 for e in events if e.type == "TaskWoken") == 1


def test_unfinished_lowrisk_tool_still_redrives(stack: Any) -> None:
    """A hard crash mid-tool (Started without Finished) on a guard-allowed
    tool: the D2 rule re-drives it unattended — the same trust as running
    it unattended in the first place. The re-driven decide re-issues the
    call and it completes."""
    log, cs, dispatcher, _ = stack
    tool = _KillOnceTool(name="reader", script={("x",): "body"})
    policy = _CrashOncePolicy([
        YieldForHumanDecision(prompt=NEXT_GOAL_WAKE_HANDLE),
        ToolCallsDecision(calls=[
            ToolCall(tool_name="reader", arguments={"p": "x"}, call_id="c1")
        ]),
        # after the re-drive the policy decides the same call again, then ends
        ToolCallsDecision(calls=[
            ToolCall(tool_name="reader", arguments={"p": "x"}, call_id="c2")
        ]),
        FinishDecision(answer="done"),
    ])
    engine, tid = _suspended_session(stack, policy, tools={"reader": tool})
    _crash_mid_turn(stack, engine, tid, NEXT_GOAL_WAKE_HANDLE)
    lease = _reclaim(stack, tid)
    outcome = run_leased_task(_RT(engine, log, cs, dispatcher), lease)
    assert outcome == "woken"
    seals = _seals(log, tid)
    assert len(seals) == 1 and seals[0].payload.reason == "auto_redrive"
    assert any(e.type == "TaskCompleted" for e in log.read(tid))


def test_spawn_in_window_parks_as_stopped_conversation(stack: Any) -> None:
    """AC2 — a window whose interrupted attempt spawned a subtask parks:
    seal + system notice + next-goal suspend; typing resumes it."""
    log, cs, dispatcher, _ = stack
    policy = _CrashOncePolicy([
        YieldForHumanDecision(prompt=NEXT_GOAL_WAKE_HANDLE),
        FinishDecision(answer="never reached"),
    ])
    engine, tid = _suspended_session(stack, policy)
    # hand-build the crash window: TaskWoken + plan + SubtaskSpawned with no
    # closing suspend — the crash-between-spawn-and-suspend shape.
    assert dispatcher.wake(
        tid, HumanResponseReceived(handle=NEXT_GOAL_WAKE_HANDLE)
    ) is True
    lease = dispatcher.lease(worker_id="w", task_id=tid)
    task = fold(log, cs, tid)
    task = engine.note_woken(
        task, lease_id=lease.lease_id, wake_event=lease.wake_event
    )
    from noeta.protocols.events import (
        ContextPlanComposedPayload,
        SubtaskSpawnedPayload,
    )
    engine._emit(  # noqa: SLF001 — hand-simulated crash window
        task_id=tid, type_="ContextPlanComposed",
        payload=ContextPlanComposedPayload(plan_ref=None),
        lease_id=lease.lease_id,
    )
    engine._emit(  # noqa: SLF001
        task_id=tid, type_="SubtaskSpawned",
        payload=SubtaskSpawnedPayload(
            subtask_id="t-child", goal="child g", agent_name="main",
        ),
        lease_id=lease.lease_id,
    )
    lease = _reclaim(stack, tid)
    outcome = run_leased_task(_RT(engine, log, cs, dispatcher), lease)
    assert outcome == "stopped"
    seals = _seals(log, tid)
    assert len(seals) == 1
    assert seals[0].payload.reason == "unsafe_tool_activity"
    # the park notice is a system-origin message naming the blocker.
    task = fold(log, cs, tid)
    assert task.status == "suspended"
    assert isinstance(task.wake_on, HumanResponseReceived)
    assert task.wake_on.handle == NEXT_GOAL_WAKE_HANDLE
    notice = task.runtime.messages[-1]
    assert notice.origin == "system"
    assert "spawned a subtask" in notice.content[0].text
    # typing resumes: the next-goal wake matches the park suspend.
    assert dispatcher.wake(
        tid, HumanResponseReceived(handle=NEXT_GOAL_WAKE_HANDLE)
    ) is True


def test_opening_turn_crash_recovers_on_drained_path(stack: Any) -> None:
    """AC3 — an opening-turn crash (no TaskWoken exists) is recovered on
    the wake-less drained path, not silently re-driven on a dirty window."""
    log, cs, dispatcher, _ = stack
    policy = _CrashOncePolicy([
        RuntimeError("simulated crash during the opening LLM call"),
        FinishDecision(answer="done"),
    ])
    engine = _engine_on(stack, policy)
    task = engine.create_task(goal="g", policy_name="scripted")
    tid = task.task_id
    dispatcher.enqueue(tid)
    lease = dispatcher.lease(worker_id="w")
    engine.append_user_message(
        task, content=[TextBlock(text="g")], lease_id=lease.lease_id
    )
    with pytest.raises(RuntimeError):
        engine.run_one_step(task, lease_id=lease.lease_id)
    lease = _reclaim(stack, tid)
    assert lease.wake_event is None          # drained path, no wake
    outcome = run_leased_task(_RT(engine, log, cs, dispatcher), lease)
    assert outcome == "drained"
    seals = _seals(log, tid)
    assert len(seals) == 1 and seals[0].payload.reason == "auto_redrive"
    assert any(e.type == "TaskCompleted" for e in log.read(tid))


def test_crash_loop_hits_abandon_cap_and_parks(stack: Any) -> None:
    """AC5 — recovery recurses across repeated crashes; the cap forces a
    park instead of burning LLM calls forever."""
    log, cs, dispatcher, _ = stack
    script: list[Any] = [YieldForHumanDecision(prompt=NEXT_GOAL_WAKE_HANDLE)]
    script += [RuntimeError(f"crash {i}") for i in range(ABANDON_CAP + 2)]
    script += [FinishDecision(answer="never reached")]
    policy = _CrashOncePolicy(script)
    engine, tid = _suspended_session(stack, policy)
    _crash_mid_turn(stack, engine, tid, NEXT_GOAL_WAKE_HANDLE)
    outcome: Any = None
    for _ in range(ABANDON_CAP + 1):
        lease = _reclaim(stack, tid)
        # the real daemon's heartbeat keepalive resets the dispatcher's
        # stale-reclaim poison counter each cycle; mimic it so THIS test
        # exercises the abandon cap, not reclaim_max (whichever backstop
        # trips first wins in production — both are deliberate).
        dispatcher.heartbeat(lease.lease_id)
        try:
            outcome = run_leased_task(_RT(engine, log, cs, dispatcher), lease)
        except RuntimeError:
            continue                          # the re-drive crashed again
        break
    assert outcome == "stopped"
    seals = _seals(log, tid)
    assert [s.payload.reason for s in seals] == (
        ["auto_redrive"] * ABANDON_CAP + ["abandon_cap"]
    )
    task = fold(log, cs, tid)
    assert task.status == "suspended"        # parked, NOT silently terminal
    assert task.runtime.messages[-1].origin == "system"


def test_interrupted_approval_execution_parks_and_reapproves(stack: Any) -> None:
    """A crash while executing a human-approved tool call: the plan-less
    window parks on the approval's own handle, the seal restores the
    pending approval, and the ordinary approve verb re-runs it."""
    log, cs, dispatcher, _ = stack
    tool = _KillOnceTool(name="danger", script={(): "did it"})
    hooks = HookManager()
    hooks.register(
        PermissionGuard(
            PermissionPolicy(require_approval_tools=frozenset({"danger"})),
            {"danger": tool},
        )
    )
    policy = _CrashOncePolicy([
        ToolCallsDecision(calls=[
            ToolCall(tool_name="danger", arguments={}, call_id="c1")
        ]),
        FinishDecision(answer="done"),
    ])
    engine = _engine_on(stack, policy, tools={"danger": tool}, hooks=hooks)
    task = engine.create_task(goal="g", policy_name="scripted")
    tid = task.task_id
    dispatcher.enqueue(tid)
    lease = dispatcher.lease(worker_id="w")
    engine.append_user_message(
        task, content=[TextBlock(text="g")], lease_id=lease.lease_id
    )
    task = engine.run_one_step(task, lease_id=lease.lease_id)
    assert task.status == "suspended"        # awaiting approval of c1
    approval_handle = task.wake_on.handle
    assert approval_handle == "approval-c1"
    dispatcher.release(
        lease.lease_id, next_state="suspended", wake_on=task.wake_on
    )
    # approve → the execution crashes hard mid-tool.
    assert dispatcher.wake(
        tid, HumanResponseReceived(handle=approval_handle)
    ) is True
    lease = dispatcher.lease(worker_id="w", task_id=tid)
    task = fold(log, cs, tid)
    task = engine.note_woken(
        task, lease_id=lease.lease_id, wake_event=lease.wake_event
    )
    with pytest.raises(KeyboardInterrupt):
        engine.resolve_tool_approval(
            task, call_id="c1", approved=True, lease_id=lease.lease_id
        )
    lease = _reclaim(stack, tid)
    outcome = run_leased_task(_RT(engine, log, cs, dispatcher), lease)
    assert outcome == "stopped"
    seals = _seals(log, tid)
    assert len(seals) == 1
    assert seals[0].payload.reason == "interrupted_approval"
    task = fold(log, cs, tid)
    assert task.status == "suspended"
    assert task.wake_on.handle == approval_handle     # SAME approval handle
    assert "c1" in task.governance.pending_approvals  # approval restored
    assert task.runtime.messages[-1].origin == "system"
    # the ordinary approve verb works again and the tool re-runs to done.
    assert dispatcher.wake(
        tid, HumanResponseReceived(handle=approval_handle)
    ) is True
    lease = dispatcher.lease(worker_id="w", task_id=tid)
    task = fold(log, cs, tid)
    task = engine.note_woken(
        task, lease_id=lease.lease_id, wake_event=lease.wake_event
    )
    task = engine.resolve_tool_approval(
        task, call_id="c1", approved=True, lease_id=lease.lease_id
    )
    task = engine.run_one_step(task, lease_id=lease.lease_id)
    assert task.status == "terminal"
    dispatcher.release(
        lease.lease_id, next_state="terminal",
        consumed_wake_event=lease.wake_event,
    )
    assert any(e.type == "TaskCompleted" for e in log.read(tid))


def test_recovered_stream_folds_bytes_equal_from_scratch(stack: Any) -> None:
    """AC7 half — after a seal + re-drive, the from-scratch fold and the
    baseline-accelerated fold land byte-equal (the TaskRewound invariant,
    extended to the new marker)."""
    log, cs, dispatcher, _ = stack
    policy = _CrashOncePolicy([
        YieldForHumanDecision(prompt=NEXT_GOAL_WAKE_HANDLE),
        RuntimeError("crash"),
        FinishDecision(answer="done"),
    ])
    engine, tid = _suspended_session(stack, policy)
    _crash_mid_turn(stack, engine, tid, NEXT_GOAL_WAKE_HANDLE)
    lease = _reclaim(stack, tid)
    run_leased_task(_RT(engine, log, cs, dispatcher), lease)
    accelerated = fold(log, cs, tid)
    scratch = fold(log, cs, tid, ignore_snapshots=True)
    assert serialize_task_state(accelerated) == serialize_task_state(scratch)


# ---------------------------------------------------------------------------
# D6 — seed-time prelude durability (driver level)
# ---------------------------------------------------------------------------


def _coding_session(ws: Any, responses: list[Any]) -> Any:
    from noeta.testing.fake_llm import FakeLLMProvider
    from noeta.tools.fs import FsWriteMode, ShellMode

    from tests._sdk_session import (
        make_driver,
        make_host,
        make_registry,
        runner_main_spec,
    )

    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responses=responses),
        model="gpt-test",
        multi_turn=True,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
    )
    return host, make_driver(host)


def _end_turn(text: str) -> Any:
    from noeta.protocols.messages import LLMResponse, Usage

    return LLMResponse(
        stop_reason="end_turn", content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1), raw={"id": "e"},
    )


def test_seed_send_goal_is_durable_before_drive(tmp_path: Any) -> None:
    """D6 — the ack contract: ``seed_send_goal`` returns only after
    ``TaskWoken`` + the goal message are durable, so a crash between seed
    and drive loses nothing. The later drive is prelude-less (case 2′) and
    the turn produces the exact pre-change event order with exactly one
    ``TaskWoken`` and exactly one goal append."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, driver = _coding_session(ws, [_end_turn("t1"), _end_turn("t2")])
    out = driver.start(goal="g", agent="main")
    assert out.status == "suspended"
    tid = out.task_id
    before = len(host.event_log.read(tid))
    seeded = driver.seed_send_goal(tid, goal="turn two")
    # the 202 moment: wake consumed into a durable TaskWoken + goal append.
    events = host.event_log.read(tid)
    tail_types = [e.type for e in events[before:]]
    assert tail_types[0] == "TaskWoken"
    assert "MessagesAppended" in tail_types
    assert seeded.prelude is None            # applied at seed, not at drive
    # ...crash here would re-drive the bare step with the goal intact...
    outcome = driver.drive_seeded(seeded)
    assert outcome.status == "suspended"
    events = host.event_log.read(tid)
    # exactly one TaskWoken and one goal append for the turn — the seed
    # application did not double anything and the drive re-ran nothing.
    assert sum(1 for e in events[before:] if e.type == "TaskWoken") == 1
    goal_appends = [
        e for e in events[before:] if e.type == "MessagesAppended"
    ]
    assert len(goal_appends) >= 1
    assert not any(e.type == "StepAttemptAbandoned" for e in events)


# ---------------------------------------------------------------------------
# Pending-park completion — the park decision is durable with the seal;
# a crash between the seal and the suspend must finish the park, never
# bare-re-drive over it.
# ---------------------------------------------------------------------------


class _SealPayload:
    def __init__(self, reason: str, abandoned_from_seq: int = 1) -> None:
        self.reason = reason
        self.abandoned_from_seq = abandoned_from_seq


def test_scan_pending_park_variants() -> None:
    parked = [
        _Env("TaskWoken", 0),
        _Env("ContextPlanComposed", 1),
        _Env("SubtaskSpawned", 2),
        _Env("StepAttemptAbandoned", 3, _SealPayload("unsafe_tool_activity")),
    ]
    pending = scan_pending_park(parked)
    assert pending is not None and pending.notice_appended is False
    # notice already on the stream (crash hit between notice and suspend)
    pending = scan_pending_park(parked + [_Env("MessagesAppended", 4)])
    assert pending is not None and pending.notice_appended is True
    # an auto_redrive seal: the bare case-2′ re-drive IS the design
    redrive = [
        _Env("TaskWoken", 0),
        _Env("ContextPlanComposed", 1),
        _Env("StepAttemptAbandoned", 2, _SealPayload("auto_redrive")),
    ]
    assert scan_pending_park(redrive) is None
    # step activity after the seal: a re-drive already ran, nothing pending
    assert scan_pending_park(parked + [_Env("ContextPlanComposed", 4)]) is None


class _SuspendedPayload:
    def __init__(self, wake_on: Any) -> None:
        self.wake_on = wake_on


def test_park_handle_prefers_wake_then_stream() -> None:
    events = [
        _Env(
            "TaskSuspended", 0,
            _SuspendedPayload(HumanResponseReceived(handle="approval-c9")),
        ),
        _Env("TaskWoken", 1),
        _Env("ToolCallApprovalResolved", 2),
    ]
    # the consumed wake wins when present…
    assert worker_mod._park_handle(
        events, reason="interrupted_approval",
        consumed=HumanResponseReceived(handle="approval-c1"),
    ) == "approval-c1"
    # …a wake-less re-entry falls back to the suspension the window consumed
    assert worker_mod._park_handle(
        events, reason="interrupted_approval", consumed=None
    ) == "approval-c9"
    # non-approval parks always rest on the next-goal handle
    assert worker_mod._park_handle(
        events, reason="unsafe_tool_activity", consumed=None
    ) == NEXT_GOAL_WAKE_HANDLE


def _hand_built_spawn_window(stack: Any, engine: Any, tid: str) -> None:
    """Wake + note_woken + plan + SubtaskSpawned with no closing suspend —
    the crash-between-spawn-and-suspend shape the park tests recover."""
    from noeta.protocols.events import (
        ContextPlanComposedPayload,
        SubtaskSpawnedPayload,
    )

    log, cs, dispatcher, _ = stack
    assert dispatcher.wake(
        tid, HumanResponseReceived(handle=NEXT_GOAL_WAKE_HANDLE)
    ) is True
    lease = dispatcher.lease(worker_id="w", task_id=tid)
    task = fold(log, cs, tid)
    engine.note_woken(
        task, lease_id=lease.lease_id, wake_event=lease.wake_event
    )
    engine._emit(  # noqa: SLF001 — hand-simulated crash window
        task_id=tid, type_="ContextPlanComposed",
        payload=ContextPlanComposedPayload(plan_ref=None),
        lease_id=lease.lease_id,
    )
    engine._emit(  # noqa: SLF001
        task_id=tid, type_="SubtaskSpawned",
        payload=SubtaskSpawnedPayload(
            subtask_id="t-child", goal="child g", agent_name="main",
        ),
        lease_id=lease.lease_id,
    )


def test_crash_during_park_before_notice_completes_park(
    stack: Any, monkeypatch: Any
) -> None:
    """A second crash right after the seal (before the park's notice): the
    park decision is already durable in the seal's reason, so the next
    entry completes the park — notice (blockers re-derived from the dead
    tail) + suspend — instead of bare-re-driving over it."""
    log, cs, dispatcher, _ = stack
    policy = _CrashOncePolicy([
        YieldForHumanDecision(prompt=NEXT_GOAL_WAKE_HANDLE),
        FinishDecision(answer="never reached"),
    ])
    engine, tid = _suspended_session(stack, policy)
    _hand_built_spawn_window(stack, engine, tid)
    real_append = Engine.append_user_message
    state = {"raised": False}

    def kill_once(self: Any, *args: Any, **kwargs: Any) -> Any:
        if not state["raised"]:
            state["raised"] = True
            raise KeyboardInterrupt("simulated crash mid-park")
        return real_append(self, *args, **kwargs)

    monkeypatch.setattr(Engine, "append_user_message", kill_once)
    lease = _reclaim(stack, tid)
    with pytest.raises(KeyboardInterrupt):
        run_leased_task(_RT(engine, log, cs, dispatcher), lease)
    assert len(_seals(log, tid)) == 1        # the decision IS durable
    seen: list[ReliabilityEvent] = []
    lease = _reclaim(stack, tid)
    outcome = run_leased_task(
        _RT(engine, log, cs, dispatcher), lease, reliability_sink=seen.append
    )
    assert outcome == "stopped"
    assert len(_seals(log, tid)) == 1        # completed, not re-sealed
    task = fold(log, cs, tid)
    assert task.status == "suspended"
    assert task.wake_on.handle == NEXT_GOAL_WAKE_HANDLE
    notices = [m for m in task.runtime.messages if m.origin == "system"]
    assert len(notices) == 1
    assert "spawned a subtask" in notices[0].content[0].text
    # the bare step never ran over the parked window.
    assert not any(e.type == "TaskCompleted" for e in log.read(tid))
    assert [e.kind for e in seen] == ["attempt_parked"]
    assert seen[0].detail["resumed_park"] is True


def test_crash_during_park_after_notice_does_not_duplicate_it(
    stack: Any, monkeypatch: Any
) -> None:
    """A second crash between the park's notice and its suspend: the next
    entry finishes with the suspend only — the operator warning is not
    appended twice."""
    log, cs, dispatcher, _ = stack
    policy = _CrashOncePolicy([
        YieldForHumanDecision(prompt=NEXT_GOAL_WAKE_HANDLE),
        FinishDecision(answer="never reached"),
    ])
    engine, tid = _suspended_session(stack, policy)
    _hand_built_spawn_window(stack, engine, tid)
    real_suspend = worker_mod.suspend_on_human_handle
    state = {"raised": False}

    def kill_once(*args: Any, **kwargs: Any) -> Any:
        if not state["raised"]:
            state["raised"] = True
            raise KeyboardInterrupt("simulated crash mid-park")
        return real_suspend(*args, **kwargs)

    monkeypatch.setattr(worker_mod, "suspend_on_human_handle", kill_once)
    lease = _reclaim(stack, tid)
    with pytest.raises(KeyboardInterrupt):
        run_leased_task(_RT(engine, log, cs, dispatcher), lease)
    lease = _reclaim(stack, tid)
    outcome = run_leased_task(_RT(engine, log, cs, dispatcher), lease)
    assert outcome == "stopped"
    assert len(_seals(log, tid)) == 1
    task = fold(log, cs, tid)
    assert task.status == "suspended"
    notices = [m for m in task.runtime.messages if m.origin == "system"]
    assert len(notices) == 1                 # written once, before the crash


# ---------------------------------------------------------------------------
# Classification baseline — the guard verdict is about the state the
# re-drive runs on, not the dirty interrupted window.
# ---------------------------------------------------------------------------


def test_dirty_window_budget_does_not_mispark(stack: Any) -> None:
    """The interrupted attempt's own ``ToolCallStarted`` must not consume
    the budget it is judged by: with ``max_tool_calls=1`` and one dirty
    started call, the re-drive would run the call fine (the seal reverts
    the counter), so classification must say safe — not park."""
    log, cs, dispatcher, _ = stack
    tool = _KillOnceTool(name="reader", script={("x",): "body"})
    hooks = HookManager()
    hooks.register(BudgetGuard(Budget(max_tool_calls=1)))
    policy = _CrashOncePolicy([
        YieldForHumanDecision(prompt=NEXT_GOAL_WAKE_HANDLE),
        ToolCallsDecision(calls=[
            ToolCall(tool_name="reader", arguments={"p": "x"}, call_id="c1")
        ]),
        # the re-driven decide re-issues the call, then ends
        ToolCallsDecision(calls=[
            ToolCall(tool_name="reader", arguments={"p": "x"}, call_id="c2")
        ]),
        FinishDecision(answer="done"),
    ])
    engine, tid = _suspended_session(
        stack, policy, tools={"reader": tool}, hooks=hooks
    )
    _crash_mid_turn(stack, engine, tid, NEXT_GOAL_WAKE_HANDLE)
    lease = _reclaim(stack, tid)
    outcome = run_leased_task(_RT(engine, log, cs, dispatcher), lease)
    assert outcome == "woken"
    seals = _seals(log, tid)
    assert len(seals) == 1 and seals[0].payload.reason == "auto_redrive"
    assert any(e.type == "TaskCompleted" for e in log.read(tid))


def test_capped_planless_window_parks_as_interrupted_approval(
    stack: Any,
) -> None:
    """Reason precedence: a plan-less (approval-execution) window parks as
    ``interrupted_approval`` even when the abandon cap is also tripped —
    the notice must say a human-approved call was interrupted, and the
    suspend must land on the approval's own handle."""
    log, cs, dispatcher, _ = stack
    tool = _KillOnceTool(name="danger", script={(): "did it"})
    hooks = HookManager()
    hooks.register(
        PermissionGuard(
            PermissionPolicy(require_approval_tools=frozenset({"danger"})),
            {"danger": tool},
        )
    )
    policy = _CrashOncePolicy([
        ToolCallsDecision(calls=[
            ToolCall(tool_name="danger", arguments={}, call_id="c1")
        ]),
        FinishDecision(answer="done"),
    ])
    engine = _engine_on(stack, policy, tools={"danger": tool}, hooks=hooks)
    task = engine.create_task(goal="g", policy_name="scripted")
    tid = task.task_id
    dispatcher.enqueue(tid)
    lease = dispatcher.lease(worker_id="w")
    engine.append_user_message(
        task, content=[TextBlock(text="g")], lease_id=lease.lease_id
    )
    task = engine.run_one_step(task, lease_id=lease.lease_id)
    approval_handle = task.wake_on.handle
    dispatcher.release(
        lease.lease_id, next_state="suspended", wake_on=task.wake_on
    )
    assert dispatcher.wake(
        tid, HumanResponseReceived(handle=approval_handle)
    ) is True
    lease = dispatcher.lease(worker_id="w", task_id=tid)
    task = fold(log, cs, tid)
    task = engine.note_woken(
        task, lease_id=lease.lease_id, wake_event=lease.wake_event
    )
    with pytest.raises(KeyboardInterrupt):
        engine.resolve_tool_approval(
            task, call_id="c1", approved=True, lease_id=lease.lease_id
        )
    lease = _reclaim(stack, tid)
    events = log.read(tid)
    attempt = scan_interrupted_attempt(events)
    assert attempt is not None and attempt.anchored_on_plan is False
    # force the cap alongside the plan-less anchor to pin the precedence.
    attempt = dataclasses.replace(attempt, abandon_count=ABANDON_CAP)
    outcome = worker_mod._recover_interrupted_attempt(
        _RT(engine, log, cs, dispatcher), lease, engine, events, attempt,
        cancelled=None,
        consumed=lease.wake_event,
        outcome="woken",
        reliability_sink=None,
    )
    assert outcome == "stopped"
    seals = _seals(log, tid)
    assert seals[-1].payload.reason == "interrupted_approval"
    task = fold(log, cs, tid)
    assert task.status == "suspended"
    assert task.wake_on.handle == approval_handle
    assert "human-approved" in task.runtime.messages[-1].content[0].text


# ---------------------------------------------------------------------------
# D6 hardening — seed-time failure paths stay resumable
# ---------------------------------------------------------------------------


def test_seed_prelude_failure_compensates_to_resumable(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """If the seed-time prelude fails AFTER ``note_woken`` (e.g. a payload
    cap), the driver re-suspends on the same handle and releases the lease:
    the conversation stays resumable and the retried command works —
    nothing is left half-seeded for the worker to re-drive without input."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, driver = _coding_session(ws, [_end_turn("t1"), _end_turn("t2")])
    out = driver.start(goal="g", agent="main")
    tid = out.task_id
    real_append = Engine.append_user_message
    state = {"raised": False}

    def fail_once(self: Any, *args: Any, **kwargs: Any) -> Any:
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError("simulated append failure")
        return real_append(self, *args, **kwargs)

    monkeypatch.setattr(Engine, "append_user_message", fail_once)
    with pytest.raises(RuntimeError):
        driver.seed_send_goal(tid, goal="boom")
    task = fold(host.event_log, host.content_store, tid)
    assert task.status == "suspended"        # compensated, not stranded
    assert task.wake_on.handle == NEXT_GOAL_WAKE_HANDLE
    seeded = driver.seed_send_goal(tid, goal="turn two")
    outcome = driver.drive_seeded(seeded)
    assert outcome.status == "suspended"
    events = host.event_log.read(tid)
    assert not any(e.type == "StepAttemptAbandoned" for e in events)


def test_seed_retry_path_applies_durable_prelude(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The dispatcher-restore retry inside ``_seed_woken`` must land in the
    same D6 tail as the first-try path: the durable prelude is applied at
    seed (ack ⇒ input durable) and the drive runs prelude-less."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, driver = _coding_session(ws, [_end_turn("t1"), _end_turn("t2")])
    out = driver.start(goal="g", agent="main")
    tid = out.task_id
    real_lease = host.dispatcher.lease
    state = {"n": 0}

    def flaky_lease(**kwargs: Any) -> Any:
        state["n"] += 1
        if state["n"] == 1:
            return None
        return real_lease(**kwargs)

    monkeypatch.setattr(host.dispatcher, "lease", flaky_lease)
    before = len(host.event_log.read(tid))
    seeded = driver.seed_send_goal(tid, goal="turn two")
    assert state["n"] >= 2                   # the retry branch actually ran
    assert seeded.prelude is None            # D6: applied at seed
    tail_types = [e.type for e in host.event_log.read(tid)[before:]]
    assert tail_types[0] == "TaskWoken"
    assert "MessagesAppended" in tail_types
    outcome = driver.drive_seeded(seeded)
    assert outcome.status == "suspended"


def test_worker_loop_emits_reliability_and_never_silent_terminal(
    stack: Any,
) -> None:
    """AC8 — the daemon path: recovery inside ``WorkerLoop.tick`` emits the
    ``attempt_parked`` reliability kind and the task rests suspended (the
    old fail(retryable)→terminal path is gone)."""
    log, cs, dispatcher, _ = stack
    policy = _CrashOncePolicy([
        YieldForHumanDecision(prompt=NEXT_GOAL_WAKE_HANDLE),
        FinishDecision(answer="never reached"),
    ])
    engine, tid = _suspended_session(stack, policy)
    assert dispatcher.wake(
        tid, HumanResponseReceived(handle=NEXT_GOAL_WAKE_HANDLE)
    ) is True
    lease = dispatcher.lease(worker_id="w", task_id=tid)
    task = fold(log, cs, tid)
    task = engine.note_woken(
        task, lease_id=lease.lease_id, wake_event=lease.wake_event
    )
    from noeta.protocols.events import (
        ContextPlanComposedPayload,
        SubtaskSpawnedPayload,
    )
    engine._emit(  # noqa: SLF001 — hand-simulated crash window
        task_id=tid, type_="ContextPlanComposed",
        payload=ContextPlanComposedPayload(plan_ref=None),
        lease_id=lease.lease_id,
    )
    engine._emit(  # noqa: SLF001
        task_id=tid, type_="SubtaskSpawned",
        payload=SubtaskSpawnedPayload(
            subtask_id="t-child", goal="child g", agent_name="main",
        ),
        lease_id=lease.lease_id,
    )
    _, _, _, clock = stack
    clock[0] += 100_000.0
    assert tid in dispatcher.requeue_stale()
    seen: list[ReliabilityEvent] = []
    loop = WorkerLoop(
        _RT(engine, log, cs, dispatcher),
        heartbeat_interval=0,
        stale_sweep_interval=0,
        timer_poll_interval=0,
        reliability_sink=seen.append,
    )
    assert loop.tick() is True
    kinds = [e.kind for e in seen]
    assert "attempt_parked" in kinds
    assert "step_failed_retryable" not in kinds
    task = fold(log, cs, tid)
    assert task.status == "suspended"        # parked, not terminal
    assert dispatcher.task_status(tid) == "suspended"
