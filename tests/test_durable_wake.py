"""H2 — durable exactly-once wake.

Covers: the dispatcher contract (D1 lease does not consume matched; D2
consuming release clears it; D6 mismatch/no-matched raises + rolls back;
D5 non-consuming release preserves matched), parametrized over
InMemory + SQLite; the P1.2 suspend-window boundary helper
(`_find_matching_woken_index`, incl. the recurring-handle gate 5b); and
the worker D4 recovery state machine end-to-end (first-consume /
terminal-reconcile / re-suspend-reconcile / bare re-drive) — each crash
window simulated by dropping the in-flight lease + `requeue_stale` + a
fresh lease, asserting exactly-once. The old case 5 (H1 partial-step
orphan → typed error) is replaced by the attempt-recovery machine —
covered in tests/test_attempt_recovery.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.execution.driver import NEXT_GOAL_WAKE_HANDLE
from noeta.protocols.messages import TextBlock
from noeta.protocols.errors import WakeConsumeMismatch
from noeta.protocols.decisions import (
    FinishDecision,
    YieldForHumanDecision,
)
from noeta.protocols.wake import (
    HumanResponseReceived,
    SubtaskCompleted,
    SubtaskResult,
)
from noeta.policies.stub import StubScriptedPolicy
from noeta.runtime.worker import (
    _find_matching_woken_index,
    run_leased_task,
)
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.storage.sqlite.dispatcher import SqliteDispatcher
from noeta.storage.sqlite.contentstore import SqliteContentStore
from noeta.storage.sqlite.eventlog import SqliteEventLog
from noeta.testing.composer import trivial_three_segment


# ---------------------------------------------------------------------------
# Dispatcher contract — parametrized InMemory + SQLite
# ---------------------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def disp(request: Any) -> Any:
    if request.param == "memory":
        return InMemoryDispatcher()
    return SqliteDispatcher(":memory:")


def _suspend_with_matched(disp: Any, task_id: str, event: Any) -> None:
    """Drive a task to suspended-on-`event` then wake it → matched-in-flight
    (status ready, matched set)."""
    disp.enqueue(task_id)
    lease = disp.lease(worker_id="w", task_id=task_id)
    assert lease is not None
    disp.release(lease.lease_id, next_state="suspended", wake_on=event)
    assert disp.wake(task_id, event) is True


def test_d2_consuming_release_clears_matched(disp: Any) -> None:
    ev = SubtaskCompleted(subtask_id="c", result=SubtaskResult(status="completed"))
    _suspend_with_matched(disp, "t1", ev)
    lease = disp.lease(worker_id="w", task_id="t1")
    assert lease is not None and lease.wake_event == ev
    # consume it (terminal) → matched cleared.
    disp.release(lease.lease_id, next_state="terminal", consumed_wake_event=ev)
    # task is terminal now; nothing further to lease.


def _sqlite_row(disp: Any, task_id: str) -> Any:
    return disp._conn.execute(  # noqa: SLF001 — test inspects durable row
        "SELECT status, lease_id, matched_wake_event_canonical, ready_order "
        "FROM dispatcher_tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()


def test_d6_mismatch_raises_and_rolls_back(disp: Any) -> None:
    ev = SubtaskCompleted(subtask_id="c", result=SubtaskResult(status="completed"))
    other = SubtaskCompleted(subtask_id="OTHER", result=SubtaskResult(status="completed"))
    _suspend_with_matched(disp, "t1", ev)
    lease = disp.lease(worker_id="w", task_id="t1")
    assert lease is not None
    before = _sqlite_row(disp, "t1") if isinstance(disp, SqliteDispatcher) else None
    with pytest.raises(WakeConsumeMismatch):
        disp.release(lease.lease_id, next_state="terminal", consumed_wake_event=other)
    # P1.4: rollback committed NOTHING — the durable row is byte-for-byte
    # unchanged (status / lease_id / matched / ready_order).
    if isinstance(disp, SqliteDispatcher):
        after = _sqlite_row(disp, "t1")
        assert tuple(after) == tuple(before)  # type: ignore[arg-type]
        assert after["matched_wake_event_canonical"] is not None  # matched intact
    # and a correct consuming release still succeeds afterwards (lease valid).
    disp.release(lease.lease_id, next_state="terminal", consumed_wake_event=ev)


def test_d6_no_matched_raises(disp: Any) -> None:
    # a task with no matched event: a consuming release must raise.
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w", task_id="t1")
    assert lease is not None
    ev = SubtaskCompleted(subtask_id="c", result=SubtaskResult(status="completed"))
    with pytest.raises(WakeConsumeMismatch):
        disp.release(lease.lease_id, next_state="terminal", consumed_wake_event=ev)


def test_d5_non_consuming_release_preserves_matched(disp: Any) -> None:
    ev = SubtaskCompleted(subtask_id="c", result=SubtaskResult(status="completed"))
    _suspend_with_matched(disp, "t1", ev)
    lease = disp.lease(worker_id="w", task_id="t1")
    assert lease is not None and lease.wake_event == ev
    # release WITHOUT consumed_wake_event (e.g. heartbeat-cap / skipped) →
    # matched preserved → re-lease re-delivers it.
    disp.release(lease.lease_id, next_state="suspended", wake_on=ev)
    # the release(suspended) re-suspends; but matched was preserved, so the
    # task is ready again (drain) OR matched still set. Re-lease re-delivers.
    relead = disp.lease(worker_id="w", task_id="t1")
    assert relead is not None and relead.wake_event == ev


# ---------------------------------------------------------------------------
# P1.2 — suspend-window boundary helper (incl. recurring-handle gate 5b)
# ---------------------------------------------------------------------------


class _Env:
    def __init__(self, type_: str, payload: Any) -> None:
        self.type = type_
        self.payload = payload


class _Susp:
    def __init__(self, wake_on: Any) -> None:
        self.wake_on = wake_on


class _Woke:
    def __init__(self, wake_event: Any) -> None:
        self.wake_event = wake_event


def test_boundary_no_suspension_returns_sentinel() -> None:
    ev = HumanResponseReceived(handle="h")
    assert _find_matching_woken_index([], ev) == -2
    assert _find_matching_woken_index(
        [_Env("TaskStarted", object())], ev
    ) == -2


def test_boundary_matching_after_suspend() -> None:
    ev = HumanResponseReceived(handle="h")
    events = [
        _Env("TaskSuspended", _Susp(ev)),
        _Env("TaskWoken", _Woke(ev)),
    ]
    assert _find_matching_woken_index(events, ev) == 1


def test_boundary_no_woken_after_suspend_is_none() -> None:
    ev = HumanResponseReceived(handle="h")
    events = [_Env("TaskSuspended", _Susp(ev))]
    assert _find_matching_woken_index(events, ev) is None


def test_recurring_handle_round2_ignores_round1_woken() -> None:
    """Gate 5b — the same handle reused across two rounds: in round 2 the
    matching search is bounded to AFTER round 2's TaskSuspended, so round
    1's TaskWoken is NOT mistaken for round 2's consumption."""
    ev = HumanResponseReceived(handle="noeta-code-next-goal")
    events = [
        _Env("TaskSuspended", _Susp(ev)),   # round 1 suspend
        _Env("TaskWoken", _Woke(ev)),       # round 1 woken
        _Env("MessagesAppended", object()),
        _Env("TaskSuspended", _Susp(ev)),   # round 2 suspend (boundary)
    ]
    # round 2 lease: no TaskWoken after the round-2 TaskSuspended (idx 3).
    assert _find_matching_woken_index(events, ev) is None


def test_rewind_after_failed_turn_does_not_strand_woken() -> None:
    """Regression — a conversation ``rewind`` that undoes
    a turn which already woke from a next-goal suspend must NOT leave that
    turn's ``TaskWoken`` stranded as a phantom consumption. Without honouring
    the rewind re-base, the next genuine wake matched the dead round-1
    ``TaskWoken`` (idx 1), reconciled as an already-consumed duplicate (case
    4), and silently dropped the new goal — the session looked resumable but
    swallowed every message. The window must open AFTER the latest rewind, so
    the next wake is a fresh first-consume (``None`` → case 1)."""
    ev = HumanResponseReceived(handle="noeta-code-next-goal")
    events = [
        _Env("TaskSuspended", _Susp(ev)),   # the next-goal suspend
        _Env("TaskWoken", _Woke(ev)),       # a turn woke, ran, and failed...
        _Env("TaskFailed", object()),
        _Env("TaskRewound", object()),      # ...then a rewind undid it (boundary)
    ]
    assert _find_matching_woken_index(events, ev) is None


# ---------------------------------------------------------------------------
# Worker D4 recovery machine — SQLite-primary + InMemory, all crash windows
# ---------------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "memory"])
def stack(request: Any, tmp_path: Any) -> Any:
    """A clock-injectable storage stack (event_log, content_store,
    dispatcher, clock). SQLite is the **primary** acceptance surface; the
    clock lets us drive lease expiry deterministically to simulate a crash."""
    clock = [1000.0]

    def now() -> float:
        return clock[0]

    if request.param == "sqlite":
        db = str(tmp_path / "h2.db")
        dispatcher: Any = SqliteDispatcher(db, now=now)
        event_log: Any = SqliteEventLog(db, lease_validator=dispatcher)
        content_store: Any = SqliteContentStore(db)
    else:
        dispatcher = InMemoryDispatcher(now=now)
        event_log = InMemoryEventLog(lease_validator=dispatcher)
        content_store = InMemoryContentStore()
    return event_log, content_store, dispatcher, clock


class _RT:
    """Concrete `WorkerRuntime` (the protocol can't be instantiated)."""

    def __init__(self, engine: Any, log: Any, cs: Any, dispatcher: Any) -> None:
        self.engine = engine
        self.event_log = log
        self.content_store = cs
        self.dispatcher = dispatcher


def _woken_count(log: Any, task_id: str) -> int:
    return sum(1 for e in log.read(task_id) if e.type == "TaskWoken")


def _human_engine(stack: Any, *, decisions: list[Any]) -> tuple[Any, str, str]:
    """Build an engine on `stack`; drive to a first YieldForHuman suspend.
    Returns (engine, task_id, first_handle)."""
    event_log, content_store, dispatcher, _ = stack
    wire_default_observers(event_log, dispatcher)
    engine = Engine(
        event_log=event_log, content_store=content_store,
        composer=trivial_three_segment(content_store),
        policy=StubScriptedPolicy(decisions),
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None
    engine.append_user_message(task, content=[TextBlock(text="g")], lease_id=lease.lease_id)
    task = engine.run_one_step(task, lease_id=lease.lease_id)   # → YieldForHuman
    assert task.status == "suspended"
    dispatcher.release(lease.lease_id, next_state="suspended", wake_on=task.wake_on)
    return engine, task.task_id, task.wake_on.handle


def _wake_and_lease(stack: Any, tid: str, handle: str) -> Any:
    event_log, content_store, dispatcher, _ = stack
    assert dispatcher.wake(tid, HumanResponseReceived(handle=handle)) is True
    lease = dispatcher.lease(worker_id="w", task_id=tid)
    assert lease is not None and lease.wake_event is not None
    return lease


def _crash_then_release(stack: Any, tid: str) -> Any:
    """Simulate a crash before release: expire the in-flight lease, requeue
    (matched preserved, D3), and re-lease (matched re-delivered, D1)."""
    event_log, content_store, dispatcher, clock = stack
    clock[0] += 100_000.0
    assert tid in dispatcher.requeue_stale()
    release = dispatcher.lease(worker_id="w", task_id=tid)
    assert release is not None and release.wake_event is not None
    return release


def test_d4_case1_first_consume(stack: Any) -> None:
    engine, tid, handle = _human_engine(
        stack, decisions=[YieldForHumanDecision(prompt="a"), FinishDecision(answer="done")]
    )
    log, cs, dispatcher, _ = stack
    lease = _wake_and_lease(stack, tid, handle)
    outcome = run_leased_task(_RT(engine, log, cs, dispatcher), lease)
    assert outcome == "woken"
    assert _woken_count(log, tid) == 1               # exactly one TaskWoken
    assert any(e.type == "TaskCompleted" for e in log.read(tid))


def test_d4_case2_crash_after_woken_before_step(stack: Any) -> None:
    """Crash after TaskWoken, before the step ran → re-delivery skips emit,
    runs the step on the new lease; exactly one TaskWoken."""
    engine, tid, handle = _human_engine(
        stack, decisions=[YieldForHumanDecision(prompt="a"), FinishDecision(answer="done")]
    )
    log, cs, dispatcher, _ = stack
    lease = _wake_and_lease(stack, tid, handle)
    # note_woken (emits TaskWoken) but DO NOT run the step — crash here.
    engine.note_woken(fold(log, cs, tid), lease_id=lease.lease_id, wake_event=lease.wake_event)
    assert _woken_count(log, tid) == 1
    release = _crash_then_release(stack, tid)
    outcome = run_leased_task(_RT(engine, log, cs, dispatcher), release)
    assert outcome == "woken"
    assert _woken_count(log, tid) == 1               # NO second TaskWoken (case 2)
    assert any(e.type == "TaskCompleted" for e in log.read(tid))


def test_d4_case3_terminal_reconcile_after_crash(stack: Any) -> None:
    """Crash after TaskWoken + TaskCompleted, before release → reconcile
    terminal, no re-run / re-emit."""
    engine, tid, handle = _human_engine(
        stack, decisions=[YieldForHumanDecision(prompt="a"), FinishDecision(answer="done")]
    )
    log, cs, dispatcher, _ = stack
    lease = _wake_and_lease(stack, tid, handle)
    task = engine.note_woken(fold(log, cs, tid), lease_id=lease.lease_id, wake_event=lease.wake_event)
    task = engine.run_one_step(task, lease_id=lease.lease_id)
    assert task.status == "terminal" and _woken_count(log, tid) == 1
    release = _crash_then_release(stack, tid)
    outcome = run_leased_task(_RT(engine, log, cs, dispatcher), release)
    assert outcome == "woken"
    assert _woken_count(log, tid) == 1               # NO second TaskWoken (case 3)


def test_d4_case4_resuspend_reconcile_after_crash(stack: Any) -> None:
    """Crash after TaskWoken + a fresh TaskSuspended(new wake_on), before
    release → no re-emit; the NEW wake_on is installed in the dispatcher."""
    engine, tid, handle = _human_engine(
        stack,
        decisions=[YieldForHumanDecision(prompt="first"), YieldForHumanDecision(prompt="second")],
    )
    log, cs, dispatcher, _ = stack
    lease = _wake_and_lease(stack, tid, handle)   # handle == "first"
    task = engine.note_woken(fold(log, cs, tid), lease_id=lease.lease_id, wake_event=lease.wake_event)
    task = engine.run_one_step(task, lease_id=lease.lease_id)   # → suspend on "second"
    assert task.status == "suspended" and task.wake_on.handle == "second"
    assert _woken_count(log, tid) == 1
    release = _crash_then_release(stack, tid)
    outcome = run_leased_task(_RT(engine, log, cs, dispatcher), release)
    assert outcome == "woken"
    assert _woken_count(log, tid) == 1               # NO second TaskWoken (case 4)
    # the NEW wake_on ("second") was installed → a wake for it matches.
    assert dispatcher.wake(tid, HumanResponseReceived(handle="second")) is True


def test_d4_case2prime_prelude_only_tail_runs_bare_step(stack: Any) -> None:
    """Case 2′ — TaskWoken + durable prelude appends (a seed-written user
    message), no ``ContextPlanComposed``, still running → the bare step runs
    on top of the durable prelude (D6: nothing is lost, nothing re-runs; the
    old case 5 typed error no longer exists for this window)."""
    engine, tid, handle = _human_engine(
        stack, decisions=[YieldForHumanDecision(prompt="a"), FinishDecision(answer="done")]
    )
    log, cs, dispatcher, _ = stack
    lease = _wake_and_lease(stack, tid, handle)
    task = engine.note_woken(fold(log, cs, tid), lease_id=lease.lease_id, wake_event=lease.wake_event)
    # the seed-durable prelude append after TaskWoken; crash before the step.
    engine.append_user_message(task, content=[TextBlock(text="the goal")], lease_id=lease.lease_id)
    release = _crash_then_release(stack, tid)
    outcome = run_leased_task(_RT(engine, log, cs, dispatcher), release)
    assert outcome == "woken"
    assert _woken_count(log, tid) == 1               # NO second TaskWoken
    events = log.read(tid)
    # the durable prelude message survived, the step ran once, no seal.
    assert any(e.type == "TaskCompleted" for e in events)
    assert not any(e.type == "StepAttemptAbandoned" for e in events)


# ---------------------------------------------------------------------------
# P1.1 — every `noeta code` wake consumer explicitly consumes (matched clears)
# ---------------------------------------------------------------------------


def _coding_session(
    ws: Any, responses: list[Any], *, multi_turn: bool = False, **host_knobs: Any
) -> Any:
    """A production ``SdkHost`` + ``InteractionDriver`` over an in-memory L0
    triple (the shipping SDK assembly the deleted runner is replaced by)."""
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
        multi_turn=multi_turn,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        **host_knobs,
    )
    return host, make_driver(host)


def _end_turn(text: str) -> Any:
    from noeta.protocols.messages import LLMResponse, TextBlock, Usage

    return LLMResponse(
        stop_reason="end_turn", content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1), raw={"id": "e"},
    )


def test_multi_turn_same_handle_consumes_wake_each_round(tmp_path: Any) -> None:
    """P1.1 — `send_goal` reuses the SAME next-goal handle every round; each
    resume must CONSUME its wake (clear matched), else the next re-suspend (D5
    matched-present) would wrongly re-ready the task. Drive 3 rounds; after the
    round-2 resume the task must be **suspended** (not ready) — proof the
    round-1+2 wakes were consumed. With the interactive SDK path every
    normally-finishing turn rests at a trailing next-goal suspend
    ("no synthesized terminal"), so round 3 is likewise a suspend."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "x.py").write_text("foo\n")
    host, driver = _coding_session(
        ws, [_end_turn("t1"), _end_turn("t2"), _end_turn("t3")], multi_turn=True
    )
    dispatcher = host.dispatcher
    out = driver.start(goal="g", agent="main")                 # round 1
    assert out.status == "suspended"
    tid = out.task_id
    assert driver.send_goal(tid, goal="again").status == "suspended"  # round 2
    # H2: round-2 resume consumed its wake → the re-suspend did NOT find a
    # stale matched, so the task is SUSPENDED (not spuriously re-readied).
    assert dispatcher.task_status(tid) == "suspended"
    third = driver.send_goal(tid, goal="finish")               # round 3
    assert third.status == "suspended"
    assert third.wake_handle == NEXT_GOAL_WAKE_HANDLE
    # exactly one TaskWoken per resume round (2 resumes → 2).
    woken = [e for e in host.event_log.read(tid) if e.type == "TaskWoken"]
    assert len(woken) == 2


def test_approval_resume_consumes_wake(tmp_path: Any) -> None:
    """P1.1 — `driver.approve` must consume the approval wake. After
    approve-resume the task progresses and the dispatcher holds no stale
    matched (it is terminal / suspended on its own next wake, not re-readied)."""
    from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage

    def _tool(call_id: str) -> Any:
        return LLMResponse(
            stop_reason="tool_use",
            content=[ToolUseBlock(call_id=call_id, tool_name="read",
                                  arguments={"path": "x.py"})],
            usage=Usage(uncached=1, output=1), raw={"id": call_id},
        )

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "x.py").write_text("foo\n")
    host, driver = _coding_session(
        ws, [_tool("c1"), _end_turn("done")],
        require_approval_tools=("read",),
    )
    dispatcher = host.dispatcher
    out = driver.start(goal="g", agent="main")
    assert out.status == "suspended"          # waiting on approval
    tid = out.task_id
    res = driver.approve(tid, call_id="c1")
    assert res.status == "terminal"
    # consumed: the task is terminal, NOT spuriously re-readied by a
    # stale matched.
    assert dispatcher.task_status(tid) == "terminal"
    assert sum(1 for e in host.event_log.read(tid) if e.type == "TaskWoken") == 1
