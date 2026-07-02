"""Phase-0 coverage backfill (issue 09).

PRD requires these modules at >= 90% coverage:

* ``noeta.core.engine``
* ``noeta.core.fold``
* ``noeta.core.snapshot``
* ``noeta.storage.memory``

Earlier issues already cover the happy paths; these tests target the
small set of branches that integration tests never trip — the lazy
default wiring, defensive ``raise`` statements, the snapshot
serialization helpers for every WakeCondition shape, and the fold
reducers for state-changing events. They are pure white-box behaviour
checks: a Task with the right event stream must produce the right
Task-state after fold; a Snapshot body must round-trip every typed
value the Engine can emit.
"""

from __future__ import annotations

import pytest

from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.core.snapshot import (
    deserialize_task_state,
    rehydrate_task,
    serialize_task_state,
)
from noeta.policies.stub import StubFinishPolicy, StubScriptedPolicy
from noeta.protocols.decisions import (
    FailDecision,
    FinishDecision,
    SpawnSubtaskDecision,
    ToolCall,
    ToolCallsDecision,
    WaitTimerDecision,
    YieldForHumanDecision,
)
from noeta.protocols.events import (
    EventEnvelope,
    MessagesAppendedPayload,
    SubtaskCompletedPayload,
    TaskCompletedPayload,
    TaskCreatedPayload,
    TaskFailedPayload,
    TaskSnapshotPayload,
    TaskStatePatchedPayload,
    TaskSuspendedPayload,
    TaskWokenPayload,
)
from noeta.protocols.messages import Message, TextBlock
from noeta.protocols.task import (
    ContextState,
    GovernanceState,
    RuntimeState,
    Task,
    TaskState,
)
from noeta.protocols.values import ContentRef
from noeta.protocols.wake import (
    HumanResponseReceived,
    SubtaskCompleted,
    SubtaskResult,
)
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment
from noeta.tools.fake import FakeTool


# ---------------------------------------------------------------------------
# Engine defensive branches
# ---------------------------------------------------------------------------


def _wire(
    *, policy=None, tools=None, dispatcher_factory=None, hooks=None, clock=None
):
    store = InMemoryContentStore()
    dispatcher = dispatcher_factory() if dispatcher_factory else None
    log = InMemoryEventLog(
        lease_validator=dispatcher if dispatcher else None
    )
    if dispatcher is not None:
        wire_default_observers(log, dispatcher)
    engine = Engine(
        event_log=log,
        content_store=store,
        composer=trivial_three_segment(store),
        policy=policy,
        tools=tools,
        hooks=hooks,
        clock=clock,
    )
    return engine, log, store, dispatcher


def _lease_for(dispatcher, task_id):
    dispatcher.enqueue(task_id)
    lease = dispatcher.lease(worker_id="w-test")
    assert lease is not None
    return lease.lease_id


def test_engine_without_policy_raises_runtime_error() -> None:
    engine, _log, _store, dispatcher = _wire(
        policy=None, dispatcher_factory=InMemoryDispatcher
    )
    task = engine.create_task(goal="g", policy_name="none")
    lease_id = _lease_for(dispatcher, task.task_id)
    with pytest.raises(RuntimeError, match="without a Policy"):
        engine.run_one_step(task, lease_id=lease_id)


def test_engine_requires_composer_but_lazily_wires_tool_runtime() -> None:
    """``composer`` is a required injection (no implicit
    default), but ``tool_runtime`` is still lazily wired when ``tools``
    is passed without an explicit runtime.

    The retired behaviour — Engine auto-creating a ``ThreeSegmentComposer``
    when ``composer`` was omitted — is gone: omitting ``composer`` now
    raises ``TypeError``. The zero-opinion fallback is
    ``noeta.core.composer.PassthroughComposer``, which composes every Task
    to the empty ``View()`` (no segments, ``plan_ref`` is None; the Engine
    still emits its per-step ``ContextPlanComposed`` with a ``None``
    ``plan_ref`` — core #2).
    """
    from noeta.core.composer import PassthroughComposer
    from noeta.protocols.task import Task
    from noeta.protocols.view import View

    tool = FakeTool(name="echo", script={(): "ok"})

    # composer is required: omitting it raises TypeError.
    with pytest.raises(TypeError):
        Engine(
            event_log=InMemoryEventLog(),
            content_store=InMemoryContentStore(),
            policy=StubFinishPolicy("ok"),
            tools={"echo": tool},
        )

    # PassthroughComposer is the documented zero-opinion fallback: it
    # composes every Task to the empty View (no segments, plan_ref None).
    view = PassthroughComposer().compose(Task(task_id="t-pass", status="running"))
    assert view == View()
    assert view.segments == ()
    assert view.plan_ref is None

    # tool_runtime is still lazily wired when tools is passed without one.
    store = InMemoryContentStore()
    engine = Engine(
        event_log=InMemoryEventLog(),
        content_store=store,
        composer=trivial_three_segment(store),
        policy=StubFinishPolicy("ok"),
        tools={"echo": tool},
    )
    assert engine._tool_runtime is not None


def test_engine_state_patch_is_written_and_applied() -> None:
    """A Decision carrying a state_patch produces TaskStatePatched + apply."""
    from noeta.protocols.decisions import TaskStatePatch

    policy = StubScriptedPolicy(
        [
            FinishDecision(
                answer="ok", state_patch=TaskStatePatch(set_goal="patched")
            )
        ]
    )
    engine, log, _store, dispatcher = _wire(
        policy=policy, dispatcher_factory=InMemoryDispatcher
    )
    task = engine.create_task(goal="initial", policy_name="scripted")
    lease_id = _lease_for(dispatcher, task.task_id)
    result = engine.run_one_step(task, lease_id=lease_id)
    assert result.state.goal == "patched"
    types = [e.type for e in log.read(task.task_id)]
    assert "TaskStatePatched" in types


def test_task_state_patch_from_dict_unknown_field_raises() -> None:
    """Replay payloads with unknown keys are rejected by from_dict.

    Replaces the pre-typed ``_apply_state_patch`` test: now that the
    patch shape is closed (PRD §"protocol shape"), the only path that needs
    runtime validation is rehydration from a possibly-stale event
    payload.
    """
    from noeta.protocols.decisions import TaskStatePatch

    with pytest.raises(KeyError, match="unknown TaskStatePatch field"):
        TaskStatePatch.from_dict({"nonexistent_field": 1})


def test_engine_yield_for_human_decision_path() -> None:
    policy = StubScriptedPolicy(
        [YieldForHumanDecision(prompt="confirm please?")]
    )
    engine, _log, _store, dispatcher = _wire(
        policy=policy, dispatcher_factory=InMemoryDispatcher
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    lease_id = _lease_for(dispatcher, task.task_id)
    result = engine.run_one_step(task, lease_id=lease_id)
    assert result.status == "suspended"
    assert isinstance(result.wake_on, HumanResponseReceived)


def test_engine_wait_timer_decision_suspends_with_timer_fired_wake() -> None:
    """``wait_timer`` writes snapshot + TaskSuspended with a TimerFired wake."""
    from noeta.protocols.wake import TimerFired

    policy = StubScriptedPolicy([WaitTimerDecision(seconds=30)])
    engine, log, _store, dispatcher = _wire(
        policy=policy,
        dispatcher_factory=InMemoryDispatcher,
        clock=lambda: 1_000.0,
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    lease_id = _lease_for(dispatcher, task.task_id)
    result = engine.run_one_step(task, lease_id=lease_id)
    assert result.status == "suspended"
    assert isinstance(result.wake_on, TimerFired)
    assert result.wake_on.fire_at == 1_030.0
    # The PRD pseudocode requires a snapshot directly before the
    # TaskSuspended event so a suspended task can resume from here.
    types = [e.type for e in log.read(task.task_id)]
    suspend_idx = types.index("TaskSuspended")
    assert types[suspend_idx - 1] == "TaskSnapshot"
    suspend_payload = log.read(task.task_id)[suspend_idx].payload
    assert suspend_payload.reason == "waiting_timer"


def test_engine_tool_calls_without_tool_runtime_raises() -> None:
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[ToolCall(call_id="c1", tool_name="echo", arguments={})]
            )
        ]
    )
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    engine = Engine(
        event_log=log,
        content_store=store,
        composer=trivial_three_segment(store),
        policy=policy,
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    with pytest.raises(RuntimeError, match="no ToolRuntime"):
        engine.run_one_step(task, lease_id="lease-test")


def test_engine_resolve_tool_unknown_raises() -> None:
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(call_id="c1", tool_name="missing", arguments={})
                ]
            ),
            FinishDecision(answer="never"),
        ]
    )
    engine, _log, _store, dispatcher = _wire(
        policy=policy,
        tools={"echo": FakeTool(name="echo", script={(): "ok"})},
        dispatcher_factory=InMemoryDispatcher,
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    lease_id = _lease_for(dispatcher, task.task_id)
    with pytest.raises(KeyError, match="unknown tool"):
        engine.run_one_step(task, lease_id=lease_id)


# NOTE: Phase 0 used to ship two tests asserting that Engine itself
# raised RuntimeError when a spawn_subtask / child-terminal Decision ran
# without a Dispatcher wired in. That validation moved out of Engine in
# candidate A (CONTEXT.md: "the Engine has no knowledge of the dispatcher");
# the responsibility now belongs to the runtime owner (Worker / test fixture)
# calling
# ``wire_default_observers``. A no-observer run silently fails to enqueue
# the child — a caller bug, not an Engine invariant — so there is nothing
# left for Engine to assert and these two tests have been removed.


def test_engine_tool_call_denied_emits_no_messages_appended() -> None:
    """When every tool_call is denied, no MessagesAppended event is written."""
    from noeta.core.hooks import HookManager
    from noeta.protocols.hooks import (
        Guard,
        GuardContext,
        ProposedAction,
        ProposedToolCall,
        Verdict,
        VerdictResult,
    )

    class DenyAllToolCalls(Guard):
        priority = 10

        def check(
            self, action: ProposedAction, ctx: GuardContext
        ) -> VerdictResult:
            if isinstance(action, ProposedToolCall):
                return VerdictResult(verdict=Verdict.DENY, reason="nope")
            return VerdictResult(verdict=Verdict.ALLOW)

    hooks = HookManager()
    hooks.register(DenyAllToolCalls())
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[ToolCall(call_id="c1", tool_name="echo", arguments={})]
            ),
            FinishDecision(answer="done"),
        ]
    )
    engine, log, _store, dispatcher = _wire(
        policy=policy,
        tools={"echo": FakeTool(name="echo", script={(): "ok"})},
        dispatcher_factory=InMemoryDispatcher,
        hooks=hooks,
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    lease_id = _lease_for(dispatcher, task.task_id)
    engine.run_one_step(task, lease_id=lease_id)
    events = log.read(task.task_id)
    denied = [e for e in events if e.type == "ToolCallDenied"]
    assert len(denied) == 1


def test_engine_latest_trace_id_empty_log_falls_back() -> None:
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    engine = Engine(
        event_log=log,
        content_store=store,
        composer=trivial_three_segment(store),
        policy=StubFinishPolicy("ok"),
    )
    assert engine._latest_trace_id("unknown-task") == "trace-unknown"


# ---------------------------------------------------------------------------
# fold reducer coverage
# ---------------------------------------------------------------------------


def _envelope(task_id: str, seq: int, type_: str, payload) -> EventEnvelope:
    return EventEnvelope(
        id=f"evt-{type_}-{seq}",
        task_id=task_id,
        seq=seq,
        type=type_,
        schema_version=1,
        occurred_at=0.0,
        actor="engine",
        trace_id="trace-1",
        correlation_id=task_id,
        causation_id=None,
        payload=payload,
    )


class _StubEventLog:
    def __init__(self, events: list[EventEnvelope]) -> None:
        self._events = events

    def read(self, task_id, *, after_seq=None):
        if after_seq is None:
            return [e for e in self._events if e.task_id == task_id]
        return [
            e
            for e in self._events
            if e.task_id == task_id and e.seq > after_seq
        ]

    def find_latest_snapshot(self, task_id):
        return None


# Issue 14: tests that fold a stream with MessagesAppended need a
# real ContentStore body to dereference. ``_messages_ref_for`` builds
# the ref and stashes the canonical body in this module-global dict
# which ``_StubContentStore`` is initialised with at test time.
_PRELOADED_MESSAGES_BODIES: dict[str, bytes] = {}


def _messages_ref_for(messages):
    import hashlib

    from noeta.protocols.canonical import to_canonical_bytes
    from noeta.protocols.values import ContentRef

    body = to_canonical_bytes(messages)
    digest = hashlib.sha256(body).hexdigest()
    _PRELOADED_MESSAGES_BODIES[digest] = body
    return ContentRef(hash=digest, size=len(body), media_type="application/json")


class _StubContentStore:
    """Phase 0 stub. Issue 14: MessagesAppended fold now dereferences
    ``messages_ref`` so tests that include that event must supply a
    real bytes-keyed store. The constructor accepts an optional dict
    so call sites pre-load the bodies they expect fold to dereference.
    """

    def __init__(self, blobs: dict[str, bytes] | None = None) -> None:
        self._blobs = blobs or {}

    def put(self, body: bytes, *, media_type: str):  # noqa: ARG002
        import hashlib

        from noeta.protocols.values import ContentRef

        digest = hashlib.sha256(body).hexdigest()
        self._blobs[digest] = body
        return ContentRef(hash=digest, size=len(body), media_type=media_type)

    def get(self, ref):
        if ref.hash not in self._blobs:
            from noeta.protocols.errors import ContentNotFound

            raise ContentNotFound(ref.hash)
        return self._blobs[ref.hash]


def test_fold_empty_stream_returns_bare_task() -> None:
    log = _StubEventLog([])
    task = fold(log, _StubContentStore(), "t-empty")
    assert task.task_id == "t-empty"
    assert task.status == "pending"


def test_fold_wrong_genesis_raises() -> None:
    from noeta.protocols.values import ContentRef

    bogus = _envelope(
        "t-x",
        seq=1,
        type_="MessagesAppended",
        payload=MessagesAppendedPayload(
            messages_ref=ContentRef(hash="x", size=0, media_type="application/json"),
            count=0,
        ),
    )
    log = _StubEventLog([bogus])
    with pytest.raises(ValueError, match="expected TaskCreated"):
        fold(log, _StubContentStore(), "t-x")


def test_fold_replays_full_lifecycle_events() -> None:
    tid = "t-life"
    events = [
        _envelope(
            tid, 1, "TaskCreated",
            TaskCreatedPayload(goal="g", policy_name="p", agent_name="a"),
        ),
        _envelope(tid, 2, "TaskStarted", object()),
        _envelope(
            tid, 3, "TaskStatePatched",
            TaskStatePatchedPayload(patch={"set_goal": "patched"}),
        ),
        # Issue 14: MessagesAppended carries a ref + count; the body
        # lives in ContentStore. ``cs`` is pre-loaded below.
        _envelope(
            tid, 4, "MessagesAppended",
            MessagesAppendedPayload(
                messages_ref=_messages_ref_for(
                    [Message(role="user", content=[TextBlock(text="hi")])]
                ),
                count=1,
            ),
        ),
        _envelope(
            tid, 5, "TaskSuspended",
            TaskSuspendedPayload(
                reason="waiting_subtask",
                wake_on=SubtaskCompleted(subtask_id="t-c"),
            ),
        ),
        _envelope(
            tid, 6, "TaskWoken",
            TaskWokenPayload(wake_event=SubtaskCompleted(subtask_id="t-c")),
        ),
        _envelope(tid, 7, "TaskSnapshot", TaskSnapshotPayload(
            state_ref=ContentRef(hash="h", size=1, media_type="application/json")
        )),
        _envelope(
            tid, 8, "SubtaskCompleted",
            SubtaskCompletedPayload(
                subtask_id="t-c",
                result=SubtaskResult(status="completed", output="ok"),
            ),
        ),
        _envelope(tid, 9, "TaskCompleted", TaskCompletedPayload(answer="ok")),
    ]
    log = _StubEventLog(events)
    cs = _StubContentStore(_PRELOADED_MESSAGES_BODIES)
    task = fold(log, cs, tid)
    assert task.state.goal == "patched"
    assert task.runtime.messages == [
        Message(role="user", content=[TextBlock(text="hi")])
    ]
    assert task.status == "terminal"
    assert len(task.governance.subtask_results) == 1


def test_fold_task_failed_marks_terminal() -> None:
    tid = "t-fail"
    events = [
        _envelope(
            tid, 1, "TaskCreated",
            TaskCreatedPayload(goal="g", policy_name="p", agent_name="a"),
        ),
        _envelope(
            tid, 2, "TaskFailed",
            TaskFailedPayload(reason="boom", retryable=False),
        ),
    ]
    task = fold(_StubEventLog(events), _StubContentStore(), tid)
    assert task.status == "terminal"


def test_fold_unknown_event_type_is_skipped_with_warning(caplog) -> None:
    tid = "t-unknown"
    events = [
        _envelope(
            tid, 1, "TaskCreated",
            TaskCreatedPayload(goal="g", policy_name="p", agent_name="a"),
        ),
        _envelope(tid, 2, "FutureEventType", object()),
    ]
    with caplog.at_level("WARNING"):
        task = fold(_StubEventLog(events), _StubContentStore(), tid)
    assert task.status == "pending"
    assert any("unknown event type" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Snapshot helpers — round-trip every typed shape
# ---------------------------------------------------------------------------


def _round_trip_state(task: Task) -> Task:
    body = serialize_task_state(task)
    return rehydrate_task(deserialize_task_state(body))


def test_snapshot_round_trip_contentref_in_context_state() -> None:
    ref = ContentRef(hash="abc", size=4, media_type="text/plain")
    task = Task(
        task_id="t-ctx",
        status="running",
        context=ContextState(plan_ref=ref),
    )
    rehydrated = _round_trip_state(task)
    assert rehydrated.context.plan_ref == ref


def test_snapshot_round_trip_human_response_wake() -> None:
    task = Task(
        task_id="t-hum",
        status="suspended",
        wake_on=HumanResponseReceived(handle="approve-1"),
    )
    rehydrated = _round_trip_state(task)
    assert rehydrated.wake_on == HumanResponseReceived(handle="approve-1")


def test_snapshot_round_trip_subtask_completed_wake() -> None:
    task = Task(
        task_id="t-sub",
        status="suspended",
        wake_on=SubtaskCompleted(subtask_id="t-child"),
    )
    rehydrated = _round_trip_state(task)
    assert rehydrated.wake_on == SubtaskCompleted(subtask_id="t-child")


def test_snapshot_round_trip_subtask_result_in_governance() -> None:
    task = Task(
        task_id="t-gov",
        status="running",
        governance=GovernanceState(
            subtask_results=[
                SubtaskResult(status="completed", output={"k": "v"}),
                SubtaskResult(status="failed", error="boom"),
            ]
        ),
    )
    rehydrated = _round_trip_state(task)
    assert rehydrated.governance.subtask_results[0].output == {"k": "v"}
    assert rehydrated.governance.subtask_results[1].error == "boom"


def test_snapshot_round_trip_no_wake_on_remains_none() -> None:
    task = Task(task_id="t-none", status="running")
    rehydrated = _round_trip_state(task)
    assert rehydrated.wake_on is None


def test_canonical_round_trip_contentref() -> None:
    from noeta.protocols.canonical import (
        from_canonical_bytes,
        to_canonical_bytes,
    )

    ref = ContentRef(hash="abc", size=4, media_type="text/plain")
    restored = from_canonical_bytes(to_canonical_bytes(ref))
    assert restored == ref


def test_canonical_round_trip_human_response() -> None:
    from noeta.protocols.canonical import (
        from_canonical_bytes,
        to_canonical_bytes,
    )

    wake = HumanResponseReceived(handle="approve-7")
    assert from_canonical_bytes(to_canonical_bytes(wake)) == wake


def test_canonical_round_trip_subtask_result() -> None:
    from noeta.protocols.canonical import (
        from_canonical_bytes,
        to_canonical_bytes,
    )

    res = SubtaskResult(status="completed", output={"k": "v"}, error=None)
    assert from_canonical_bytes(to_canonical_bytes(res)) == res


def test_canonical_untagged_dict_passes_through() -> None:
    """Untagged input survives the round-trip as a plain dict."""
    from noeta.protocols.canonical import (
        from_canonical_bytes,
        to_canonical_bytes,
    )

    assert from_canonical_bytes(to_canonical_bytes({"weird": True})) == {
        "weird": True
    }
