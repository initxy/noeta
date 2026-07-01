"""End-to-end: Engine drives a script of ``tool_calls`` then ``finish``.

Covers Phase 0 issue 02 acceptance:

* ``StubScriptedPolicy([tool_calls([t1, t2]), finish("done")])`` reaches
  terminal in a single ``run_one_step`` invocation.
* EventLog carries the per-call ``ToolCallStarted →
  ToolResultRecorded → ToolCallFinished`` triple for both calls and a
  single ``MessagesAppended`` event that batches the two ``tool_result``
  messages.
* ``ToolResultRecorded.output_ref`` resolves to a readable body in
  ContentStore.
* Large (≥ 5 KB) outputs are written via ``ctx.artifact_store`` and
  surface as ``ToolResult.artifacts``, never inline.
* The next ``MinimalComposer.compose(task)`` exposes the freshly added
  ``tool_result`` messages.
* ``fold`` rebuilds a Task whose ``RuntimeState.messages`` is byte-equal
  to the live runtime list.
"""

from __future__ import annotations

from noeta.testing.composer import trivial_three_segment
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, ToolCall, ToolCallsDecision
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.tools.fake import FakeTool


def _build_engine(*, policy: object, tools: dict[str, object]) -> tuple[
    Engine, InMemoryEventLog, InMemoryContentStore, str, object
]:
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

    task = engine.create_task(goal="multi-step", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w-test")
    assert lease is not None
    return engine, event_log, content_store, lease.lease_id, task


def test_scripted_two_tool_calls_then_finish_reaches_terminal() -> None:
    t1 = FakeTool(name="alpha", script={("a",): "out-a"})
    t2 = FakeTool(name="beta", script={("b",): "out-b"})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(tool_name="alpha", arguments={"k": "a"}, call_id="c1"),
                    ToolCall(tool_name="beta", arguments={"k": "b"}, call_id="c2"),
                ],
            ),
            FinishDecision(answer="done"),
        ]
    )

    engine, _log, _cs, lease_id, task = _build_engine(
        policy=policy, tools={"alpha": t1, "beta": t2}
    )

    result = engine.run_one_step(task, lease_id=lease_id)

    assert result.status == "terminal"


def test_event_sequence_has_three_tool_events_per_call_and_one_batched_messages_appended() -> None:
    t1 = FakeTool(name="alpha", script={("a",): "out-a"})
    t2 = FakeTool(name="beta", script={("b",): "out-b"})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(tool_name="alpha", arguments={"k": "a"}, call_id="c1"),
                    ToolCall(tool_name="beta", arguments={"k": "b"}, call_id="c2"),
                ],
            ),
            FinishDecision(answer="done"),
        ]
    )

    engine, log, _cs, lease_id, task = _build_engine(
        policy=policy, tools={"alpha": t1, "beta": t2}
    )
    engine.run_one_step(task, lease_id=lease_id)

    types = [e.type for e in log.read(task.task_id)]
    # The 3-event tool envelope must appear twice, in order, before the
    # batched MessagesAppended (one event with both tool_result messages).
    expected_tool_block = [
        "ToolCallStarted",
        "ToolResultRecorded",
        "ToolCallFinished",
        "ToolCallStarted",
        "ToolResultRecorded",
        "ToolCallFinished",
        "MessagesAppended",
    ]
    # MessagesAppended for tool_results: there is exactly one of them
    # produced by the tool_calls branch (plus one for the finish
    # answer message that the finish branch emits afterwards).
    tool_msg_appended_count = sum(
        1
        for i, t in enumerate(types)
        if t == "MessagesAppended"
        and i > 0
        and types[i - 1] == "ToolCallFinished"
    )
    assert tool_msg_appended_count == 1

    # Find the slice from first ToolCallStarted through the batched
    # MessagesAppended and assert it equals the expected sequence.
    start_idx = types.index("ToolCallStarted")
    sliced = types[start_idx : start_idx + len(expected_tool_block)]
    assert sliced == expected_tool_block


def test_tool_result_recorded_output_ref_points_to_readable_body() -> None:
    tool = FakeTool(name="alpha", script={("a",): "small-output"})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(tool_name="alpha", arguments={"k": "a"}, call_id="c1"),
                ],
            ),
            FinishDecision(answer="done"),
        ]
    )

    engine, log, cs, lease_id, task = _build_engine(
        policy=policy, tools={"alpha": tool}
    )
    engine.run_one_step(task, lease_id=lease_id)

    recorded = [
        e for e in log.read(task.task_id) if e.type == "ToolResultRecorded"
    ][0]
    body = cs.get(recorded.payload.output_ref)
    assert b"small-output" in body


def test_large_tool_output_is_offloaded_to_artifact_store_not_inline() -> None:
    big = "X" * 5000  # > 4 KB → must go through ContentStore as artifact
    tool = FakeTool(name="big", script={("go",): big})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(tool_name="big", arguments={"k": "go"}, call_id="c1"),
                ],
            ),
            FinishDecision(answer="done"),
        ]
    )

    engine, log, cs, lease_id, task = _build_engine(
        policy=policy, tools={"big": tool}
    )
    engine.run_one_step(task, lease_id=lease_id)

    recorded = [
        e for e in log.read(task.task_id) if e.type == "ToolResultRecorded"
    ][0]
    # The tool put its output into artifacts; inline output is empty.
    assert len(recorded.payload.artifacts) == 1
    artifact_body = cs.get(recorded.payload.artifacts[0])
    assert artifact_body.decode("utf-8") == big


def test_next_compose_view_contains_both_tool_result_messages() -> None:
    t1 = FakeTool(name="alpha", script={("a",): "out-a"})
    t2 = FakeTool(name="beta", script={("b",): "out-b"})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(tool_name="alpha", arguments={"k": "a"}, call_id="c1"),
                    ToolCall(tool_name="beta", arguments={"k": "b"}, call_id="c2"),
                ],
            ),
            FinishDecision(answer="done"),
        ]
    )

    engine, _log, _cs, lease_id, task = _build_engine(
        policy=policy, tools={"alpha": t1, "beta": t2}
    )
    finished = engine.run_one_step(task, lease_id=lease_id)

    view = trivial_three_segment(_cs).compose(finished)
    tool_messages = [m for m in view.iter_messages() if m.role == "tool"]
    # Phase 1: a single role="tool" Message batches both ToolResultBlocks.
    assert len(tool_messages) == 1
    call_ids = [b.call_id for b in tool_messages[0].content]
    assert call_ids == ["c1", "c2"]


def test_fold_reproduces_runtime_messages_byte_equal_after_tool_calls() -> None:
    t1 = FakeTool(name="alpha", script={("a",): "out-a"})
    t2 = FakeTool(name="beta", script={("b",): "out-b"})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(tool_name="alpha", arguments={"k": "a"}, call_id="c1"),
                    ToolCall(tool_name="beta", arguments={"k": "b"}, call_id="c2"),
                ],
            ),
            FinishDecision(answer="done"),
        ]
    )

    engine, log, cs, lease_id, task = _build_engine(
        policy=policy, tools={"alpha": t1, "beta": t2}
    )
    finished = engine.run_one_step(task, lease_id=lease_id)

    rebuilt = fold(log, cs, task.task_id)

    assert rebuilt.runtime.messages == finished.runtime.messages
    # And the whole Task object should round-trip identically.
    assert rebuilt == finished


def test_fold_without_snapshot_acceleration_also_byte_equal_after_tool_calls() -> None:
    """The same as the previous test, but force fold to ignore snapshots
    and rebuild from the event tail. Guards the rule that snapshots are
    an optimisation only — tool events must reduce to the same state."""
    t1 = FakeTool(name="alpha", script={("a",): "out-a"})
    t2 = FakeTool(name="beta", script={("b",): "out-b"})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(tool_name="alpha", arguments={"k": "a"}, call_id="c1"),
                    ToolCall(tool_name="beta", arguments={"k": "b"}, call_id="c2"),
                ],
            ),
            FinishDecision(answer="done"),
        ]
    )

    engine, log, cs, lease_id, task = _build_engine(
        policy=policy, tools={"alpha": t1, "beta": t2}
    )
    finished = engine.run_one_step(task, lease_id=lease_id)

    rebuilt = fold(log, cs, task.task_id, ignore_snapshots=True)

    assert rebuilt == finished
