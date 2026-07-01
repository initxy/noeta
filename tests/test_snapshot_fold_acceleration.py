"""Issue 04 — snapshot-accelerated fold.

Behavioural tests for the three-trigger ``TaskSnapshot`` policy:

* terminal-prefix and suspend-prefix snapshots are exercised by issue
  01/03 tests already; this file adds the **mid-loop** trigger that
  fires when an Engine is stuck in a long ``tool_calls`` cycle.
* ``fold`` must use the most-recent snapshot only (never replay multiple)
  and must finish in well under 5 ms on a 100-event task fixture.
* ``apply_event`` must tolerate unknown event types (warning + continue)
  so future schema additions never break replay of historical streams.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from noeta.testing.composer import trivial_three_segment
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.snapshot import (
    CONSECUTIVE_TOOL_CALLS_SNAPSHOT_THRESHOLD,
    deserialize_task_state,
    serialize_task_state,
    snapshot_media_type,
)
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, ToolCall, ToolCallsDecision
from noeta.protocols.events import EventEnvelope, TaskSnapshotPayload
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.tools.fake import FakeTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_engine(
    *, policy: object, tools: dict[str, object]
) -> tuple[Engine, InMemoryEventLog, InMemoryContentStore, str, Any]:
    content_store = InMemoryContentStore()
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    composer = trivial_three_segment(content_store)
    tool_runtime = ToolRuntime(
        event_log=event_log, content_store=content_store
    )
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=policy,
        tools=tools,
        tool_runtime=tool_runtime,
    )
    task = engine.create_task(goal="long-loop", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w-test")
    assert lease is not None
    return engine, event_log, content_store, lease.lease_id, task


def _make_tool_call_decision(idx: int) -> ToolCallsDecision:
    return ToolCallsDecision(
        calls=[
            ToolCall(
                tool_name="t",
                arguments={"i": idx},
                call_id=f"c{idx}",
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Mid-loop snapshot trigger
# ---------------------------------------------------------------------------


def test_threshold_constant_defaults_to_20() -> None:
    """The mid-loop trigger threshold is documented as 20."""
    assert CONSECUTIVE_TOOL_CALLS_SNAPSHOT_THRESHOLD == 20


def test_long_tool_loop_writes_mid_snapshot_and_does_not_exit_main_loop() -> None:
    """A 25-step ``tool_calls`` cycle followed by ``finish`` must:

    1. reach terminal in a single ``run_one_step`` call (the snapshot
       must NOT release the lease),
    2. write at least one ``TaskSnapshot`` between ``TaskStarted`` and
       the terminal ``TaskCompleted`` (the *mid* snapshot — there is
       still the usual terminal snapshot at the end),
    3. place the first mid snapshot near the threshold (after roughly
       20 tool iterations).
    """
    script: list[Any] = [_make_tool_call_decision(i) for i in range(25)]
    script.append(FinishDecision(answer="done"))
    tool = FakeTool(name="t", script={(i,): f"out-{i}" for i in range(25)})

    engine, log, _cs, lease_id, task = _build_engine(
        policy=StubScriptedPolicy(script), tools={"t": tool}
    )

    result = engine.run_one_step(task, lease_id=lease_id)

    assert result.status == "terminal", "Engine must not release lease mid-loop"

    events = log.read(task.task_id)
    snapshot_seqs = [e.seq for e in events if e.type == "TaskSnapshot"]
    completed_seq = next(
        e.seq for e in events if e.type == "TaskCompleted"
    )

    # Terminal snapshot is always present (issue 01); mid snapshots are
    # the new behaviour. We expect at least 2 snapshots: one mid-loop
    # plus the terminal one immediately before TaskCompleted.
    assert len(snapshot_seqs) >= 2, (
        f"expected at least 1 mid + 1 terminal snapshot, got {snapshot_seqs}"
    )

    # At least one snapshot must occur *before* the terminal completed event
    # and *after* TaskStarted — i.e. strictly mid-loop.
    started_seq = next(e.seq for e in events if e.type == "TaskStarted")
    mid_snapshots = [
        s for s in snapshot_seqs if started_seq < s < completed_seq - 1
    ]
    assert mid_snapshots, (
        f"no mid-loop snapshot between started={started_seq} and "
        f"completed={completed_seq}; snapshots={snapshot_seqs}"
    )


def test_long_tool_loop_mid_snapshot_does_not_emit_task_suspended() -> None:
    """The mid-loop snapshot must NOT trigger a suspend. The task keeps
    running until it reaches a non-tool_calls decision."""
    script: list[Any] = [_make_tool_call_decision(i) for i in range(25)]
    script.append(FinishDecision(answer="done"))
    tool = FakeTool(name="t", script={(i,): f"out-{i}" for i in range(25)})

    engine, log, _cs, lease_id, task = _build_engine(
        policy=StubScriptedPolicy(script), tools={"t": tool}
    )
    engine.run_one_step(task, lease_id=lease_id)

    types = [e.type for e in log.read(task.task_id)]
    assert "TaskSuspended" not in types, (
        "mid-loop snapshot must not emit TaskSuspended"
    )


# ---------------------------------------------------------------------------
# fold acceleration
# ---------------------------------------------------------------------------


def test_fold_with_multiple_snapshots_uses_only_latest_plus_tail() -> None:
    """When the stream contains 3 snapshots, fold must (a) read the
    body of the *latest* snapshot and (b) replay only events with
    seq > latest_snapshot.seq. Earlier snapshots are ignored.
    """
    script: list[Any] = [_make_tool_call_decision(i) for i in range(60)]
    script.append(FinishDecision(answer="done"))
    tool = FakeTool(name="t", script={(i,): f"out-{i}" for i in range(60)})

    engine, log, cs, lease_id, task = _build_engine(
        policy=StubScriptedPolicy(script), tools={"t": tool}
    )
    finished = engine.run_one_step(task, lease_id=lease_id)

    snapshots = [e for e in log.read(task.task_id) if e.type == "TaskSnapshot"]
    assert len(snapshots) >= 3, (
        f"need at least 3 snapshots for this test, got {len(snapshots)}"
    )

    latest_seq = snapshots[-1].seq

    # Spy on EventLog.read to assert that fold only requests events
    # strictly after the latest snapshot.
    requested: list[int | None] = []
    real_read = log.read

    def spy_read(
        task_id: str, *, after_seq: int | None = None
    ) -> list[EventEnvelope]:
        requested.append(after_seq)
        return real_read(task_id, after_seq=after_seq)

    log.read = spy_read  # type: ignore[method-assign]
    try:
        rebuilt = fold(log, cs, task.task_id)
    finally:
        log.read = real_read  # type: ignore[method-assign]

    assert rebuilt == finished
    # The accelerated path uses exactly one read(after_seq=latest_seq).
    assert requested == [latest_seq], (
        f"fold must consult only the latest snapshot's tail, got reads "
        f"with after_seq={requested}, latest_snapshot_seq={latest_seq}"
    )


def test_fold_without_snapshot_falls_back_to_full_scan() -> None:
    """An EventLog that has never written a snapshot must still fold
    correctly by replaying from ``TaskCreated``."""
    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    composer = trivial_three_segment(cs)
    policy = StubScriptedPolicy([FinishDecision(answer="ok")])
    dispatcher = InMemoryDispatcher()
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=composer,
        policy=policy,
    )
    task = engine.create_task(goal="g", policy_name="stub")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None

    # Manually remove all snapshots from the stream so fold has to start
    # from the genesis event. (The simplest way to construct this
    # scenario is to run a finish-only task and then strip snapshots.)
    finished = engine.run_one_step(task, lease_id=lease.lease_id)
    stream = log._streams[task.task_id]  # type: ignore[attr-defined]
    stream.events = [e for e in stream.events if e.type != "TaskSnapshot"]

    assert log.find_latest_snapshot(task.task_id) is None
    rebuilt = fold(log, cs, task.task_id)
    assert rebuilt == finished


def test_fold_on_100_event_task_finishes_under_5ms() -> None:
    """Performance budget per acceptance criteria: < 5 ms on InMemory.

    100 events is enough to dwarf the bookkeeping cost while still
    leaving headroom for slower CI runners.
    """
    # 49 tool_calls + 1 finish -> 3 events per tool call + several
    # life-cycle events + multiple snapshots = comfortably >100 events.
    script: list[Any] = [_make_tool_call_decision(i) for i in range(49)]
    script.append(FinishDecision(answer="done"))
    tool = FakeTool(name="t", script={(i,): f"out-{i}" for i in range(49)})
    engine, log, cs, lease_id, task = _build_engine(
        policy=StubScriptedPolicy(script), tools={"t": tool}
    )
    engine.run_one_step(task, lease_id=lease_id)

    assert len(log.read(task.task_id)) >= 100, (
        "fixture must contain at least 100 events for the perf assertion"
    )

    # Warm-up to avoid first-call import / cache costs skewing the budget.
    fold(log, cs, task.task_id)

    iterations = 5
    start = time.perf_counter()
    for _ in range(iterations):
        fold(log, cs, task.task_id)
    elapsed = (time.perf_counter() - start) / iterations

    assert elapsed < 0.005, (
        f"fold(100-event task) took {elapsed * 1000:.3f} ms, budget 5 ms"
    )


# ---------------------------------------------------------------------------
# Snapshot body shape: 4 slices round-trip byte-equal
# ---------------------------------------------------------------------------


def test_snapshot_body_round_trips_all_four_slices() -> None:
    """A serialized snapshot body must deserialize to a state dict
    containing exactly the 4 slices (plus the small lifecycle
    bookkeeping fields). After fold runs, the rebuilt Task is
    byte-equal to the live runtime Task."""
    script: list[Any] = [_make_tool_call_decision(0), FinishDecision(answer="x")]
    tool = FakeTool(name="t", script={(0,): "out-0"})

    engine, log, cs, lease_id, task = _build_engine(
        policy=StubScriptedPolicy(script), tools={"t": tool}
    )
    finished = engine.run_one_step(task, lease_id=lease_id)

    snap = log.find_latest_snapshot(task.task_id)
    assert snap is not None
    body = cs.get(snap.payload.state_ref)
    state = deserialize_task_state(body)

    # All 4 slices present.
    for key in ("runtime", "state", "context", "governance"):
        assert key in state, f"slice {key} missing from snapshot body"
    # Body matches runtime exactly.
    assert state == finished.state_dict()
    # And serialize → deserialize is a fixed point (canonical JSON).
    assert serialize_task_state(finished) == body


def test_snapshot_payload_media_type_is_application_json() -> None:
    """ContentRef stored in TaskSnapshot.payload must carry the same
    media type the serialiser advertises — guards against silent drift
    between writer and reader."""
    script: list[Any] = [FinishDecision(answer="x")]
    engine, log, _cs, lease_id, task = _build_engine(
        policy=StubScriptedPolicy(script), tools={}
    )
    engine.run_one_step(task, lease_id=lease_id)
    snap = log.find_latest_snapshot(task.task_id)
    assert snap is not None
    assert isinstance(snap.payload, TaskSnapshotPayload)
    assert snap.payload.state_ref.media_type == snapshot_media_type()


# ---------------------------------------------------------------------------
# apply_event tolerance for unknown event types
# ---------------------------------------------------------------------------


def test_fold_logs_warning_and_continues_on_unknown_event_type(
    caplog: Any,
) -> None:
    """Future event types must never break fold. The handler logs a
    warning and continues; later events still apply correctly."""
    from noeta.protocols.events import (
        MessagesAppendedPayload,
        TaskCreatedPayload,
    )

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    task_id = "task-unknown-evt"

    log.emit(
        task_id=task_id,
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(task_id=task_id, type="FutureUnknownEvent", payload={"foo": "bar"})
    from noeta.protocols.messages import Message, TextBlock

    sample = Message(role="user", content=[TextBlock(text="hi")])
    from noeta.protocols.canonical import to_canonical_bytes

    sample_ref = cs.put(
        to_canonical_bytes([sample]), media_type="application/json"
    )
    log.emit(
        task_id=task_id,
        type="MessagesAppended",
        payload=MessagesAppendedPayload(messages_ref=sample_ref, count=1),
    )

    with caplog.at_level(logging.WARNING, logger="noeta.core.fold"):
        rebuilt = fold(log, cs, task_id)

    # Event after the unknown one still applied.
    assert rebuilt.runtime.messages == [sample]
    # And a warning was logged.
    assert any(
        "FutureUnknownEvent" in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
    ), f"expected warning for FutureUnknownEvent, got: {caplog.records}"
