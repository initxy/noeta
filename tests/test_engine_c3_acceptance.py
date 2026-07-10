"""C3 implementation acceptance tests (task #25).

Pins the design-doc acceptance criteria that aren't already covered
by the existing test suite:

* **state_patch / assistant_message ordering**: under a
  ``ToolCallsDecision`` carrying both, the ``TaskStatePatched`` and
  ``MessagesAppended`` envelopes must land **before** any
  ``ToolCallStarted`` envelope on the stream — proves Engine still
  applies the two helpers BEFORE the branch handler runs.
* **child ``TaskCreated`` byte-equal**: a spawn-subtask decision
  writes a child genesis envelope whose payload byte-equals the
  pre-refactor inline write (``policy_name="scripted"`` literal at
  ``engine.py:723``).

The Engine class-body line budget (≤ 350 lines per design acceptance)
is gated by :file:`tests/test_lint_engine_budget.py` against the
500-line limit; with C3 the actual body is ~279 lines so
the gate now has ~70 lines of headroom even at the tighter design
target.
"""

from __future__ import annotations

from typing import Any


from noeta.context.composer import ThreeSegmentComposer
from noeta.core.engine import Engine
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.decisions import (
    FinishDecision,
    SpawnSubtaskDecision,
    TaskStatePatch,
    ToolCall,
    ToolCallsDecision,
)
from noeta.protocols.events import TaskCreatedPayload
from noeta.protocols.messages import Message, TextBlock
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


class _EchoTool:
    name = "echo"
    risk_level = "low"
    input_schema = {"type": "object", "additionalProperties": True}

    def invoke(
        self, arguments: dict[str, Any], ctx: ToolContext  # noqa: ARG002
    ) -> ToolResult:
        return ToolResult(output={"echoed": arguments}, success=True)


def _build_runtime() -> tuple[
    InMemoryEventLog, InMemoryContentStore, InMemoryDispatcher
]:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    cs = InMemoryContentStore()
    return log, cs, disp


# ---------------------------------------------------------------------------
# Ordering: state_patch + assistant_message before ToolCallStarted
# ---------------------------------------------------------------------------


def test_state_patch_and_assistant_message_land_before_tool_call_started() -> None:
    """C3 acceptance: when a ToolCallsDecision carries state_patch and
    assistant_message, Engine emits TaskStatePatched and
    MessagesAppended (the assistant_message) **before** any
    ToolCallStarted envelope. This proves
    ``_apply_decision_state_patch`` and
    ``_apply_decision_assistant_message`` still run in
    ``run_one_step`` *before* the tool_calls branch handler.
    """
    log, cs, disp = _build_runtime()
    tool = _EchoTool()
    decision = ToolCallsDecision(
        calls=[ToolCall(tool_name="echo", arguments={"x": 1}, call_id="c1")],
        state_patch=TaskStatePatch(set_goal="patched goal"),
        assistant_message=Message(
            role="assistant", content=[TextBlock(text="planning")]
        ),
    )
    policy = StubScriptedPolicy([decision, FinishDecision(answer="done")])
    composer = ThreeSegmentComposer(
        system_prompt="", tools={"echo": tool}, content_store=cs
    )
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=composer,
        policy=policy,
        tools={"echo": tool},
    )

    task = engine.create_task(goal="t", policy_name="stub")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w", lease_seconds=60.0)
    assert lease is not None
    engine.run_one_step(task, lease_id=lease.lease_id)
    disp.release(lease.lease_id, next_state=task.status)

    types = [e.type for e in log.read(task.task_id)]
    # Locate the indices to assert strict before-relationship.
    state_idx = types.index("TaskStatePatched")
    msg_idx_for_assistant = next(
        i
        for i, e in enumerate(log.read(task.task_id))
        if e.type == "MessagesAppended"
    )
    tool_idx = types.index("ToolCallStarted")

    assert state_idx < tool_idx, (
        f"TaskStatePatched (idx={state_idx}) must precede ToolCallStarted "
        f"(idx={tool_idx}); full sequence: {types}"
    )
    assert msg_idx_for_assistant < tool_idx, (
        f"assistant MessagesAppended (idx={msg_idx_for_assistant}) must "
        f"precede ToolCallStarted (idx={tool_idx}); full sequence: {types}"
    )


# ---------------------------------------------------------------------------
# Child TaskCreated payload byte-equal
# ---------------------------------------------------------------------------


def test_spawn_subtask_child_task_created_payload_is_byte_equal_to_pre_refactor() -> None:
    """C3 acceptance (rev2 B6): the child ``TaskCreatedPayload`` written
    when a SpawnSubtaskDecision lands matches the pre-refactor inline
    write at ``engine.py:723`` byte-equal. ``policy_name="scripted"``
    is a literal constant on ``Engine._SUBTASK_DEFAULT_POLICY_NAME``;
    if a future Decision variant carries an explicit policy_name, the
    fix is to add a kw-only field, not to flip this default.
    """
    log, cs, disp = _build_runtime()
    decision = SpawnSubtaskDecision(
        agent_name="helper", goal="subgoal", inputs={"k": "v"}
    )
    policy = StubScriptedPolicy([decision])
    composer = ThreeSegmentComposer(
        system_prompt="", tools={}, content_store=cs
    )
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=composer,
        policy=policy,
        id_factory=lambda: "child-fixed-id",
    )
    # The ChildLifecycleObserver picks up the child TaskCreated and
    # enqueues it; we need it wired so the parent's stream isn't
    # the only stream visible.
    unsubscribe = wire_default_observers(log, disp)
    try:
        task = engine.create_task(goal="parent", policy_name="stub")
        disp.enqueue(task.task_id)
        lease = disp.lease(worker_id="w", lease_seconds=60.0)
        assert lease is not None
        engine.run_one_step(task, lease_id=lease.lease_id)
        disp.release(
            lease.lease_id, next_state=task.status, wake_on=task.wake_on
        )
    finally:
        unsubscribe()

    # Find the child stream's TaskCreated and assert its payload's
    # canonical bytes match the expected literal payload exactly.
    child_events = log.read("child-fixed-id")
    assert child_events, "expected at least one event on child stream"
    child_created = child_events[0]
    assert child_created.type == "TaskCreated"

    expected_payload = TaskCreatedPayload(
        goal="subgoal",
        policy_name="scripted",  # the byte-equal-preserved literal
        agent_name="helper",
        parent_task_id=task.task_id,
        inputs={"k": "v"},
        # SR1: a child of a root (depth 0) is recorded at depth 1.
        subtask_depth=1,
    )
    assert to_canonical_bytes(child_created.payload) == to_canonical_bytes(
        expected_payload
    )
    # Engine class constant is the single source for the literal.
    assert (
        child_created.payload.policy_name
        == Engine._SUBTASK_DEFAULT_POLICY_NAME
        == "scripted"
    )
