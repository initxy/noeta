"""TaskRewound as a snapshot-shaped fold baseline.

The conversation-rewind skeleton re-bases ``fold`` onto a recorded baseline by
**appending** a ``TaskRewound{target_seq, state_ref}`` marker — it never deletes
or rewrites a prior event (append-only). These runtime-level tests pin
the two invariants the rest of the feature stands on:

* ``find_latest_snapshot`` (the accelerated fold baseline lookup) returns the
  latest of ``{TaskSnapshot, TaskRewound}`` by seq, so a rewind re-bases through
  the SAME code path a snapshot does;
* a from-scratch fold (``ignore_snapshots=True``, the path Verify uses to prove
  byte-equality) lands BYTE-EQUAL to the accelerated fold even with a rewind on
  the stream — the marker re-bases the conversation identically either way.

The fixture records a normal two-finish-ish loop, then hand-emits a TaskRewound
whose ``state_ref`` is the state folded through ``target_seq`` (exactly what the
``InteractionDriver.rewind`` control-plane write does).
"""

from __future__ import annotations

from typing import Any

from noeta.testing.composer import trivial_three_segment
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.snapshot import (
    serialize_task_state,
    snapshot_media_type,
)
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, ToolCall, ToolCallsDecision
from noeta.protocols.events import TaskRewoundPayload
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.tools.fake import FakeTool


def _build_engine(
    *, policy: object, tools: dict[str, object]
) -> tuple[Engine, InMemoryEventLog, InMemoryContentStore, str, Any]:
    content_store = InMemoryContentStore()
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    tool_runtime = ToolRuntime(event_log=event_log, content_store=content_store)
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=trivial_three_segment(content_store),
        policy=policy,
        tools=tools,
        tool_runtime=tool_runtime,
    )
    task = engine.create_task(goal="loop", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w-test")
    assert lease is not None
    return engine, event_log, content_store, lease.lease_id, task


def _emit_rewind(
    event_log: InMemoryEventLog,
    content_store: InMemoryContentStore,
    task_id: str,
    *,
    target_seq: int,
) -> None:
    """Append a TaskRewound whose state_ref is the fold through target_seq.

    Mirrors ``InteractionDriver.rewind`` without the SDK driver: fold a view of
    the stream truncated at ``target_seq``, serialise it, and append the marker.
    """
    events = event_log.read(task_id)
    truncated = [e for e in events if e.seq <= target_seq]

    class _Bounded:
        def read(self, _tid: str, *, after_seq: int | None = None) -> list[Any]:
            if after_seq is None:
                return list(truncated)
            return [e for e in truncated if e.seq > after_seq]

        def find_latest_snapshot(self, _tid: str) -> Any:
            for env in reversed(truncated):
                if env.type in ("TaskSnapshot", "TaskRewound"):
                    return env
            return None

    baseline = fold(_Bounded(), content_store, task_id)
    state_ref = content_store.put(
        serialize_task_state(baseline), media_type=snapshot_media_type()
    )
    event_log.system_emit(
        task_id=task_id,
        type="TaskRewound",
        payload=TaskRewoundPayload(target_seq=target_seq, state_ref=state_ref),
        actor="test",
        origin="system",
    )


def _record_two_tool_loop() -> tuple[
    str, InMemoryEventLog, InMemoryContentStore
]:
    script: list[Any] = [
        ToolCallsDecision(
            calls=[ToolCall(tool_name="t", arguments={"i": 0}, call_id="c0")]
        ),
        ToolCallsDecision(
            calls=[ToolCall(tool_name="t", arguments={"i": 1}, call_id="c1")]
        ),
        FinishDecision(answer="done"),
    ]
    tool = FakeTool(name="t", script={(0,): "out-0", (1,): "out-1"})
    engine, log, cs, lease_id, task = _build_engine(
        policy=StubScriptedPolicy(script), tools={"t": tool}
    )
    engine.run_one_step(task, lease_id=lease_id)
    return task.task_id, log, cs


# ---------------------------------------------------------------------------
# find_latest_snapshot now returns the latest of {TaskSnapshot, TaskRewound}
# ---------------------------------------------------------------------------


def test_find_latest_snapshot_returns_rewound_when_it_is_newest() -> None:
    task_id, log, cs = _record_two_tool_loop()
    # A real TaskSnapshot exists from the loop; appending a later TaskRewound
    # must win the baseline lookup (higher seq).
    snap_before = log.find_latest_snapshot(task_id)
    assert snap_before is not None and snap_before.type == "TaskSnapshot"

    _emit_rewind(log, cs, task_id, target_seq=3)
    baseline = log.find_latest_snapshot(task_id)
    assert baseline is not None
    assert baseline.type == "TaskRewound"
    assert baseline.payload.target_seq == 3


# ---------------------------------------------------------------------------
# Append-only: rewind only appends
# ---------------------------------------------------------------------------


def test_rewind_marker_is_append_only() -> None:
    task_id, log, cs = _record_two_tool_loop()
    before = list(log.read(task_id))
    _emit_rewind(log, cs, task_id, target_seq=4)
    after = list(log.read(task_id))
    assert after[: len(before)] == before
    assert len(after) == len(before) + 1
    assert after[-1].type == "TaskRewound"


# ---------------------------------------------------------------------------
# Replay safety: from-scratch fold == accelerated fold, BYTE-EQUAL, with rewind
# ---------------------------------------------------------------------------


def test_from_scratch_fold_byte_equal_to_accelerated_with_rewind() -> None:
    task_id, log, cs = _record_two_tool_loop()
    _emit_rewind(log, cs, task_id, target_seq=4)

    accelerated = fold(log, cs, task_id)
    from_scratch = fold(log, cs, task_id, ignore_snapshots=True)

    # The fold-acceleration invariant: accelerated and from-scratch folds
    # produce the SAME state bytes even with the rewind marker on the stream.
    assert serialize_task_state(from_scratch) == serialize_task_state(accelerated)


def test_rewound_fold_matches_truncated_prefix_state() -> None:
    """The rewound fold equals a fold of the stream truncated at target_seq —
    the dead ``target_seq+1..M`` segment contributes nothing."""
    task_id, log, cs = _record_two_tool_loop()
    target_seq = 4

    # Build the truth: fold a copy of the log containing only seqs <= target_seq.
    truncated_log = InMemoryEventLog()
    for env in log.read(task_id):
        if env.seq <= target_seq:
            truncated_log.system_emit(
                task_id=env.task_id,
                type=env.type,
                payload=env.payload,
                actor=env.actor,
                origin=env.origin,
            )
    truth = fold(truncated_log, cs, task_id)

    _emit_rewind(log, cs, task_id, target_seq=target_seq)
    rewound = fold(log, cs, task_id)

    assert serialize_task_state(rewound) == serialize_task_state(truth)


def test_double_rewind_uses_the_latest_marker() -> None:
    """A second rewind re-bases onto an even earlier target; the latest marker
    (highest seq) wins the baseline lookup."""
    task_id, log, cs = _record_two_tool_loop()
    _emit_rewind(log, cs, task_id, target_seq=6)
    _emit_rewind(log, cs, task_id, target_seq=3)

    baseline = log.find_latest_snapshot(task_id)
    assert baseline is not None and baseline.payload.target_seq == 3

    accelerated = fold(log, cs, task_id)
    from_scratch = fold(log, cs, task_id, ignore_snapshots=True)
    assert serialize_task_state(from_scratch) == serialize_task_state(accelerated)
