"""CompactionWorker: snapshot threshold, fold consistency, races.

A snapshot is an acceleration and never a source of truth, so the Worker may
only append, and the state it snapshots must fold byte-equal to a from-scratch
replay. It runs beside live writers, which makes the interesting cases the
losing ones: any path where an event lands between the fold and the append has
to end with no snapshot at all, because a snapshot at seq S that does not cover
events 0..S-1 would silently corrupt every later fold.
"""

from __future__ import annotations

import threading
from typing import Any

from noeta.core.fold import fold
from noeta.protocols.events import (
    ContextPlanComposedPayload,
    LLMRequestFinishedPayload,
    SubtaskSpawnedPayload,
    TaskCreatedPayload,
    ToolCallStartedPayload,
)
from noeta.protocols.values import ContentRef
from noeta.runtime.compaction import CompactionResult, CompactionWorker
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ref(tag: str, size: int = 10) -> ContentRef:
    return ContentRef(hash=tag * 64, size=size, media_type="application/json")


def _new_runtime() -> tuple[InMemoryEventLog, InMemoryContentStore]:
    return InMemoryEventLog(), InMemoryContentStore()


def _seed_task_created(log: InMemoryEventLog, task_id: str = "t1") -> None:
    log.emit(
        task_id=task_id,
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )


def _seed_governance_events(
    log: InMemoryEventLog,
    cs: InMemoryContentStore,
    task_id: str,
    *,
    plans: int = 0,
    tool_starts: int = 0,
    subtasks: int = 0,
    llm_finishes: int = 0,
) -> None:
    """Emit a parameterised mix of governance-affecting events, so the
    snapshot under test carries non-default counters to round-trip."""
    for i in range(plans):
        log.emit(
            task_id=task_id,
            type="ContextPlanComposed",
            payload=ContextPlanComposedPayload(plan_ref=_ref(f"p{i}", size=i + 1)),
        )
    for i in range(tool_starts):
        log.emit(
            task_id=task_id,
            type="ToolCallStarted",
            payload=ToolCallStartedPayload(
                call_id=f"c{i}", tool_name="t", arguments={}
            ),
        )
    for i in range(subtasks):
        log.emit(
            task_id=task_id,
            type="SubtaskSpawned",
            payload=SubtaskSpawnedPayload(
                subtask_id=f"sub-{i}", agent_name="a", goal="g"
            ),
        )
    for i in range(llm_finishes):
        log.emit(
            task_id=task_id,
            type="LLMRequestFinished",
            payload=LLMRequestFinishedPayload(
                call_id=f"L{i}", success=True, cost_usd=0.10
            ),
        )


# ---------------------------------------------------------------------------
# Basic API + threshold
# ---------------------------------------------------------------------------


def test_compact_on_empty_stream_returns_noop_result() -> None:
    log, cs = _new_runtime()
    w = CompactionWorker(event_log=log, content_store=cs)
    res = w.compact_if_needed("t-missing")
    assert res == CompactionResult(
        task_id="t-missing",
        compacted=False,
        events_since_latest_snapshot=0,
        latest_snapshot_seq_before=None,
        new_snapshot_seq=None,
    )


def test_compact_below_threshold_is_no_op() -> None:
    log, cs = _new_runtime()
    _seed_task_created(log)
    _seed_governance_events(log, cs, "t1", plans=3)   # 4 events total
    w = CompactionWorker(
        event_log=log, content_store=cs, max_uncompacted_events=50
    )
    res = w.compact_if_needed("t1")
    assert res.compacted is False
    assert res.events_since_latest_snapshot == 4
    assert res.latest_snapshot_seq_before is None
    assert res.new_snapshot_seq is None
    assert all(e.type != "TaskSnapshot" for e in log.read("t1"))


def test_compact_at_or_above_threshold_emits_snapshot() -> None:
    log, cs = _new_runtime()
    _seed_task_created(log)
    _seed_governance_events(log, cs, "t1", plans=4, tool_starts=3)  # 8 events
    w = CompactionWorker(
        event_log=log, content_store=cs, max_uncompacted_events=5
    )
    res = w.compact_if_needed("t1")
    assert res.compacted is True
    assert res.events_since_latest_snapshot == 8  # whole stream uncompacted
    assert res.latest_snapshot_seq_before is None
    assert res.new_snapshot_seq is not None
    snapshots = [e for e in log.read("t1") if e.type == "TaskSnapshot"]
    assert len(snapshots) == 1
    assert snapshots[0].origin == "system"
    assert snapshots[0].actor == "compaction"


def test_compact_uses_gap_since_latest_snapshot_not_total_event_count() -> None:
    """If an Engine-driven snapshot already exists, Worker measures
    the gap from that snapshot, not from stream start."""
    log, cs = _new_runtime()
    _seed_task_created(log)
    # Pretend Engine wrote a snapshot at seq 1.
    log.emit(
        task_id="t1",
        type="TaskSnapshot",
        payload=__import__(
            "noeta.protocols.events", fromlist=["TaskSnapshotPayload"]
        ).TaskSnapshotPayload(state_ref=_ref("init", size=1)),
    )
    _seed_governance_events(log, cs, "t1", plans=3)   # 3 more events
    w = CompactionWorker(
        event_log=log, content_store=cs, max_uncompacted_events=5
    )
    res = w.compact_if_needed("t1")
    # Gap from snapshot seq 1 to latest seq 4 = 3, below threshold.
    assert res.compacted is False
    assert res.events_since_latest_snapshot == 3
    assert res.latest_snapshot_seq_before == 1


def test_compact_twice_back_to_back_is_idempotent_no_op() -> None:
    """Sequential repeat: second call short-circuits because gap=0."""
    log, cs = _new_runtime()
    _seed_task_created(log)
    _seed_governance_events(log, cs, "t1", plans=10)
    w = CompactionWorker(
        event_log=log, content_store=cs, max_uncompacted_events=5
    )
    first = w.compact_if_needed("t1")
    assert first.compacted is True
    second = w.compact_if_needed("t1")
    assert second.compacted is False
    assert second.events_since_latest_snapshot == 0
    assert second.latest_snapshot_seq_before == first.new_snapshot_seq
    snapshots = [e for e in log.read("t1") if e.type == "TaskSnapshot"]
    assert len(snapshots) == 1


def test_threshold_parameter_is_configurable() -> None:
    log, cs = _new_runtime()
    _seed_task_created(log)
    _seed_governance_events(log, cs, "t1", plans=2)  # 3 events
    # With threshold=3, gap=3 >= 3 → compact.
    w = CompactionWorker(
        event_log=log, content_store=cs, max_uncompacted_events=3
    )
    assert w.compact_if_needed("t1").compacted is True


# ---------------------------------------------------------------------------
# Append-only history
# ---------------------------------------------------------------------------


def test_compaction_only_appends_does_not_rewrite_history() -> None:
    """Compacting appends one event and leaves every earlier envelope
    untouched — replay from seq 0 must stay possible forever."""
    log, cs = _new_runtime()
    _seed_task_created(log)
    _seed_governance_events(log, cs, "t1", plans=5)
    before = log.read("t1")
    snapshot_of_before = [(e.seq, e.type, e.id) for e in before]

    w = CompactionWorker(
        event_log=log, content_store=cs, max_uncompacted_events=5
    )
    w.compact_if_needed("t1")

    after = log.read("t1")
    assert len(after) == len(before) + 1
    # First N events are byte-identical (same seq, type, id).
    for env, (seq, type_, id_) in zip(after[: len(before)], snapshot_of_before):
        assert env.seq == seq
        assert env.type == type_
        assert env.id == id_


# ---------------------------------------------------------------------------
# Content is never collected
# ---------------------------------------------------------------------------


def test_compaction_only_calls_content_store_put_no_delete() -> None:
    """The ContentStore is wrapped in a surface that offers only ``put`` and
    ``get``: compaction that reclaimed bodies would break replay of the events
    still pointing at them."""
    log, cs = _new_runtime()
    _seed_task_created(log)
    _seed_governance_events(log, cs, "t1", plans=10)

    calls: list[str] = []

    class _Tracking:
        def __init__(self, inner: InMemoryContentStore) -> None:
            self._inner = inner

        def put(self, body: bytes, *, media_type: str) -> ContentRef:
            calls.append("put")
            return self._inner.put(body, media_type=media_type)

        def get(self, ref: ContentRef) -> bytes:
            calls.append("get")
            return self._inner.get(ref)

    tracking = _Tracking(cs)
    w = CompactionWorker(
        event_log=log,
        content_store=tracking,
        max_uncompacted_events=5,
    )
    w.compact_if_needed("t1")
    assert "put" in calls
    # A delete attempt would have raised AttributeError above rather than
    # failed an assertion here.


# ---------------------------------------------------------------------------
# Fold consistency across the accelerated and from-scratch paths
# ---------------------------------------------------------------------------


def test_compaction_snapshot_outcome_byte_equal_via_ignore_snapshots() -> None:
    """Whole-Task equality between ``fold(ignore_snapshots=True)`` and
    ``fold(ignore_snapshots=False)`` over a stream carrying a Worker snapshot.
    Field-by-field checks can only catch what they enumerate; this is the
    guarantee that makes a snapshot safe to trust."""
    log, cs = _new_runtime()
    _seed_task_created(log)
    _seed_governance_events(
        log, cs, "t1", plans=10, tool_starts=4, subtasks=2, llm_finishes=3
    )
    w = CompactionWorker(
        event_log=log, content_store=cs, max_uncompacted_events=5
    )
    w.compact_if_needed("t1")

    accelerated = fold(log, cs, "t1", ignore_snapshots=False)
    from_scratch = fold(log, cs, "t1", ignore_snapshots=True)

    assert accelerated == from_scratch


def test_compaction_preserves_issue18_governance_fields() -> None:
    """The governance accumulators are what BudgetGuard reads, so a snapshot
    that dropped or reset one would let a task run past its budget: the
    accelerated fold must report the same counters as the from-scratch fold."""
    log, cs = _new_runtime()
    _seed_task_created(log)
    _seed_governance_events(
        log, cs, "t1", plans=6, tool_starts=4, subtasks=2, llm_finishes=3
    )
    w = CompactionWorker(
        event_log=log, content_store=cs, max_uncompacted_events=5
    )
    w.compact_if_needed("t1")

    accelerated = fold(log, cs, "t1", ignore_snapshots=False)
    from_scratch = fold(log, cs, "t1", ignore_snapshots=True)

    for attr in (
        "iterations",
        "tool_calls",
        "spawned_subtasks",
        "cost_usd",
        "denied",
        "subtask_results",
    ):
        assert getattr(accelerated.governance, attr) == getattr(
            from_scratch.governance, attr
        ), attr

    # Sanity: the counters reflect what was emitted.
    assert accelerated.governance.iterations == 6
    assert accelerated.governance.tool_calls == 4
    assert accelerated.governance.spawned_subtasks == 2
    assert abs(accelerated.governance.cost_usd - 0.30) < 1e-9


def test_compaction_snapshot_is_not_treated_as_legacy_by_issue18_guard() -> None:
    """fold discards a snapshot body whose governance dict has no
    ``spawned_subtasks`` key, because such a body cannot carry trustworthy
    prefix counters. A Worker snapshot must clear that bar, or acceleration
    silently degrades to a full replay with nobody noticing."""
    log, cs = _new_runtime()
    _seed_task_created(log)
    _seed_governance_events(log, cs, "t1", plans=10)
    w = CompactionWorker(
        event_log=log, content_store=cs, max_uncompacted_events=5
    )
    res = w.compact_if_needed("t1")
    assert res.compacted

    # Reach into the snapshot body: the key is fold's sentinel for a body it
    # is allowed to accelerate from.
    state_dict = __import__(
        "noeta.core.snapshot", fromlist=["deserialize_task_state"]
    ).deserialize_task_state(
        cs.get(_latest_snapshot_ref(log, "t1"))
    )
    assert "spawned_subtasks" in state_dict["governance"]


def _latest_snapshot_ref(log: InMemoryEventLog, task_id: str) -> ContentRef:
    for env in reversed(log.read(task_id)):
        if env.type == "TaskSnapshot":
            return env.payload.state_ref
    raise AssertionError("no TaskSnapshot in stream")


# ---------------------------------------------------------------------------
# Best-effort semantics: stale-snapshot safety under concurrent writers
# ---------------------------------------------------------------------------


def test_race_during_put_is_caught_by_expected_seq_no_stale_snapshot() -> None:
    """An event landing during ``ContentStore.put`` is past the Worker's own
    pre-put short circuit, so only the EventLog's ``expected_seq`` CAS can
    catch it — which is the point: the CAS is the correctness gate and the
    short circuit is a cost optimisation. No stale snapshot is emitted."""
    log, cs = _new_runtime()
    _seed_task_created(log)
    _seed_governance_events(log, cs, "t1", plans=10)

    injected = {"done": False}
    real_put = cs.put

    def racing_put(body: bytes, *, media_type: str) -> ContentRef:
        ref = real_put(body, media_type=media_type)
        if not injected["done"]:
            log.emit(
                task_id="t1",
                type="ContextPlanComposed",
                payload=ContextPlanComposedPayload(
                    plan_ref=_ref("injected", size=42)
                ),
            )
            injected["done"] = True
        return ref

    cs.put = racing_put  # type: ignore[method-assign]
    w = CompactionWorker(
        event_log=log, content_store=cs, max_uncompacted_events=5
    )
    res = w.compact_if_needed("t1")

    assert res.compacted is False
    snapshots = [e for e in log.read("t1") if e.type == "TaskSnapshot"]
    assert len(snapshots) == 0

    accelerated = fold(log, cs, "t1", ignore_snapshots=False)
    from_scratch = fold(log, cs, "t1", ignore_snapshots=True)
    assert accelerated == from_scratch
    assert accelerated.governance.iterations == 11


def test_guard_a_short_circuits_known_stale_fold_before_put() -> None:
    """When a concurrent writer lands an event while the fold is running, the
    Worker re-reads the tail and gives up before ``ContentStore.put`` — the
    snapshot body would be thrown away anyway, and the discarded blob would
    stay in the store forever. Pins that the short circuit really fires."""
    log, cs = _new_runtime()
    _seed_task_created(log)
    _seed_governance_events(log, cs, "t1", plans=10)

    # Wrap ``fold`` so that **during** the fold call (after Worker's
    # first ``read`` but before the fold's own read returns), a new
    # event slips into the EventLog. Guard A's re-read will then see
    # the new tail and abort before ``ContentStore.put``.
    from noeta.runtime import compaction as compaction_module

    put_calls: list[bytes] = []
    real_put = cs.put

    def tracking_put(body: bytes, *, media_type: str) -> ContentRef:
        put_calls.append(body)
        return real_put(body, media_type=media_type)

    cs.put = tracking_put  # type: ignore[method-assign]

    injected = {"done": False}
    real_fold = compaction_module.fold

    def racing_fold(event_log, content_store, task_id):  # type: ignore[no-untyped-def]
        result = real_fold(event_log, content_store, task_id)
        if not injected["done"]:
            log.emit(
                task_id="t1",
                type="ContextPlanComposed",
                payload=ContextPlanComposedPayload(
                    plan_ref=_ref("between-read-and-guardA", size=7)
                ),
            )
            injected["done"] = True
        return result

    compaction_module.fold = racing_fold  # type: ignore[assignment]
    try:
        w = CompactionWorker(
            event_log=log, content_store=cs, max_uncompacted_events=5
        )
        res = w.compact_if_needed("t1")
    finally:
        compaction_module.fold = real_fold  # type: ignore[assignment]

    # Guard A fired → put was never called → no snapshot, no orphan
    # blob.
    assert res.compacted is False
    assert put_calls == []
    snapshots = [e for e in log.read("t1") if e.type == "TaskSnapshot"]
    assert len(snapshots) == 0

    # Fold remains consistent across paths.
    accelerated = fold(log, cs, "t1", ignore_snapshots=False)
    from_scratch = fold(log, cs, "t1", ignore_snapshots=True)
    assert accelerated == from_scratch
    assert accelerated.governance.iterations == 11


def test_race_during_emit_is_caught_by_expected_seq_no_stale_snapshot() -> None:
    """The TOCTOU window between Worker's last read and EventLog's
    append is closed by ``expected_seq``. If another writer lands an
    event during that window, the EventLog raises ``StaleSequence``,
    Worker reports ``compacted=False``, and no stale TaskSnapshot is
    emitted. Fold invariant preserved: any snapshot at seq S always
    covers events 0..S-1."""
    log, cs = _new_runtime()
    _seed_task_created(log)
    _seed_governance_events(log, cs, "t1", plans=10)

    # Wrap the EventLog so an extra event lands between Worker's
    # last read and the snapshot ``emit`` call. The race injection
    # simulates a concurrent writer slipping in just before our
    # append acquires the EventLog's internal lock.
    injected = {"done": False}
    real_emit = log.emit

    def racing_emit(*args: Any, **kwargs: Any):
        if (
            not injected["done"]
            and kwargs.get("type") == "TaskSnapshot"
            and kwargs.get("origin") == "system"
        ):
            log.emit(
                task_id="t1",
                type="ContextPlanComposed",
                payload=ContextPlanComposedPayload(
                    plan_ref=_ref("raced", size=42)
                ),
            )
            injected["done"] = True
        return real_emit(*args, **kwargs)

    log.emit = racing_emit  # type: ignore[method-assign]

    w = CompactionWorker(
        event_log=log, content_store=cs, max_uncompacted_events=5
    )
    res = w.compact_if_needed("t1")

    # Race caught → no snapshot emitted.
    assert res.compacted is False
    assert res.new_snapshot_seq is None
    snapshots = [e for e in log.read("t1") if e.type == "TaskSnapshot"]
    assert len(snapshots) == 0

    # Fold invariant intact: with no snapshot, both paths agree.
    accelerated = fold(log, cs, "t1", ignore_snapshots=False)
    from_scratch = fold(log, cs, "t1", ignore_snapshots=True)
    assert accelerated == from_scratch
    # The raced event survives in the stream.
    assert accelerated.governance.iterations == 11


def test_concurrent_compact_same_task_exactly_one_winner() -> None:
    """Two threads racing on ``compact_if_needed`` for the same task
    produce **exactly one** TaskSnapshot event. The ``expected_seq``
    CAS lets one writer through; the other catches
    :class:`StaleSequence` and returns ``compacted=False``. fold
    must agree on the final state; a sequential follow-up call
    short-circuits."""
    log, cs = _new_runtime()
    _seed_task_created(log)
    _seed_governance_events(log, cs, "t1", plans=10)

    w = CompactionWorker(
        event_log=log, content_store=cs, max_uncompacted_events=5
    )

    results: list[CompactionResult] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait()
            results.append(w.compact_if_needed("t1"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    snapshots = [e for e in log.read("t1") if e.type == "TaskSnapshot"]
    # Exactly one TaskSnapshot — the CAS winner. The loser saw
    # ``StaleSequence`` and reported ``compacted=False``.
    assert len(snapshots) == 1
    winners = [r for r in results if r.compacted]
    losers = [r for r in results if not r.compacted]
    assert len(winners) == 1
    assert len(losers) == 1

    accelerated = fold(log, cs, "t1", ignore_snapshots=False)
    from_scratch = fold(log, cs, "t1", ignore_snapshots=True)
    assert accelerated == from_scratch

    follow_up = w.compact_if_needed("t1")
    assert follow_up.compacted is False


# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


def test_emitted_snapshot_has_compaction_actor_and_system_origin() -> None:
    log, cs = _new_runtime()
    _seed_task_created(log)
    _seed_governance_events(log, cs, "t1", plans=10)
    w = CompactionWorker(
        event_log=log, content_store=cs, max_uncompacted_events=5
    )
    w.compact_if_needed("t1")
    snap = [e for e in log.read("t1") if e.type == "TaskSnapshot"][-1]
    assert snap.actor == "compaction"
    assert snap.origin == "system"


def test_emitted_snapshot_trace_id_inherits_latest_event() -> None:
    log, cs = _new_runtime()
    _seed_task_created(log)
    log.emit(
        task_id="t1",
        type="ContextPlanComposed",
        payload=ContextPlanComposedPayload(plan_ref=_ref("p", size=1)),
        trace_id="trace-latest",
    )
    _seed_governance_events(log, cs, "t1", plans=10)
    # The latest event before Worker emits will be one of the plan events
    # we just seeded; capture its trace_id explicitly.
    expected_trace = log.read("t1")[-1].trace_id
    w = CompactionWorker(
        event_log=log, content_store=cs, max_uncompacted_events=5
    )
    w.compact_if_needed("t1")
    snap = [e for e in log.read("t1") if e.type == "TaskSnapshot"][-1]
    assert snap.trace_id == expected_trace


def test_actor_override_via_constructor() -> None:
    log, cs = _new_runtime()
    _seed_task_created(log)
    _seed_governance_events(log, cs, "t1", plans=10)
    w = CompactionWorker(
        event_log=log,
        content_store=cs,
        max_uncompacted_events=5,
        actor="custom-compactor",
    )
    w.compact_if_needed("t1")
    snap = [e for e in log.read("t1") if e.type == "TaskSnapshot"][-1]
    assert snap.actor == "custom-compactor"
