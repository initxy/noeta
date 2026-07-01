"""Regression: mixed-batch tool-call approval keeps history balanced.

The single-call approval contract is covered in ``test_tool_approval.py``.
This file pins the *mixed-batch* case that the existing tests never
exercise: a parallel ``ToolCallsDecision`` where some calls auto-allow and
one requires human approval.

The structural invariant under test (see the DENY branch's own comment in
``handle_tool_calls``): every ``tool_use`` block the model emitted in one
assistant turn must get a matching ``tool_result`` before the next
compose → decide, or the resumed provider request carries a dangling
function call that Anthropic / OpenAI reject with a fatal 400.

When the batch suspends mid-way for approval, two classes of dangling
blocks would otherwise appear:

* EARLIER calls already executed into the local ``result_blocks`` — their
  results must be durably flushed before the suspend, not discarded.
* TRAILING calls after the approval-requiring one — never executed, so a
  synthesized "skipped" result must stand in for them.

The approval-requiring call itself is the only ``tool_use`` left for the
resume path (``invoke_approved_tool_call`` on approve /
``append_tool_denial_feedback`` on deny) to balance.
"""

from __future__ import annotations

from typing import Any

from noeta.core.engine import Engine
from noeta.core.hooks import HookManager
from noeta.core.wiring import wire_default_observers
from noeta.guards.permission import PermissionGuard, PermissionPolicy
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, ToolCall, ToolCallsDecision
from noeta.protocols.messages import ToolResultBlock
from noeta.protocols.wake import HumanResponseReceived
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment
from noeta.tools.fake import FakeTool


def _build(
    *,
    calls: list[ToolCall],
    second: Any = None,
) -> tuple[Engine, Any, InMemoryDispatcher, str, Any]:
    """Engine with three scripted tools; ``write`` gated for approval.

    ``read`` and ``grep`` auto-allow; ``write`` returns require_approval
    via the PermissionGuard. The policy proposes ``calls`` as one parallel
    batch, then optionally ``second`` to drive the resumed loop to
    terminal.
    """
    dispatcher = InMemoryDispatcher()
    content_store = InMemoryContentStore()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    wire_default_observers(event_log, dispatcher)
    composer = trivial_three_segment(content_store)
    tools = {
        "read": FakeTool(name="read", script={("r",): "read-out"}),
        "write": FakeTool(name="write", script={("w",): "write-out"}),
        "grep": FakeTool(name="grep", script={("g",): "grep-out"}),
    }
    tool_runtime = ToolRuntime(event_log=event_log, content_store=content_store)
    decisions: list[Any] = [ToolCallsDecision(calls=calls)]
    if second is not None:
        decisions.append(second)
    policy = StubScriptedPolicy(decisions)
    hooks = HookManager()
    hooks.register(
        PermissionGuard(
            PermissionPolicy(require_approval_tools=frozenset({"write"})),
            tools=tools,
        )
    )
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=policy,
        tools=tools,
        tool_runtime=tool_runtime,
        hooks=hooks,
    )
    task = engine.create_task(goal="mixed-approval", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None
    return engine, event_log, dispatcher, lease.lease_id, task


def _wake_and_lease(
    dispatcher: InMemoryDispatcher,
    engine: Engine,
    task: Any,
    lease_id: str,
    *,
    handle: str,
) -> tuple[Any, str]:
    dispatcher.release(lease_id, next_state="suspended", wake_on=task.wake_on)
    dispatcher.wake(task.task_id, HumanResponseReceived(handle=handle))
    lease = dispatcher.lease(worker_id="w", task_id=task.task_id)
    assert lease is not None and lease.wake_event is not None
    woken = engine.note_woken(
        task, lease_id=lease.lease_id, wake_event=lease.wake_event
    )
    return woken, lease.lease_id


def _result_call_ids(task: Any) -> set[str]:
    """Every call_id that has a matching tool_result anywhere in history."""
    ids: set[str] = set()
    for msg in task.runtime.messages:
        if msg.role != "tool":
            continue
        for block in msg.content:
            if isinstance(block, ToolResultBlock):
                ids.add(block.call_id)
    return ids


# ---------------------------------------------------------------------------
# (a) EARLIER auto-allowed call before the approval-requiring one.
# ---------------------------------------------------------------------------


def test_earlier_call_result_is_flushed_before_approval_suspend() -> None:
    read = ToolCall(tool_name="read", arguments={"k": "r"}, call_id="c-read")
    write = ToolCall(tool_name="write", arguments={"k": "w"}, call_id="c-write")
    engine, log, _disp, lease_id, task = _build(calls=[read, write])

    suspended = engine.run_one_step(task, lease_id=lease_id)

    assert suspended.status == "suspended"
    assert isinstance(suspended.wake_on, HumanResponseReceived)
    assert suspended.wake_on.handle == "approval-c-write"

    # The earlier ``read`` actually ran (real side effect)...
    types = [e.type for e in log.read(task.task_id)]
    starts = [
        e.payload.call_id
        for e in log.read(task.task_id)
        if e.type == "ToolCallStarted"
    ]
    assert starts == ["c-read"]
    # ...and its result was durably flushed BEFORE the suspend, so its
    # tool_use is not left dangling across the suspend boundary.
    assert "MessagesAppended" in types
    assert types.index("MessagesAppended") < types.index("TaskSuspended")
    assert "c-read" in _result_call_ids(suspended)
    # The approval-requiring call has NOT been answered yet (resolved on
    # resume), so its result is absent here by design.
    assert "c-write" not in _result_call_ids(suspended)


def test_earlier_call_balanced_after_approve_resume() -> None:
    read = ToolCall(tool_name="read", arguments={"k": "r"}, call_id="c-read")
    write = ToolCall(tool_name="write", arguments={"k": "w"}, call_id="c-write")
    engine, _log, disp, lease_id, task = _build(
        calls=[read, write], second=FinishDecision(answer="done")
    )
    suspended = engine.run_one_step(task, lease_id=lease_id)
    woken, new_lease = _wake_and_lease(
        disp, engine, suspended, lease_id, handle="approval-c-write"
    )

    resolved = engine.resolve_tool_approval(
        woken, call_id="c-write", approved=True, resolver="host",
        lease_id=new_lease,
    )

    # BOTH tool_use blocks now have a matching tool_result — nothing dangling.
    assert {"c-read", "c-write"} <= _result_call_ids(resolved)
    final = engine.run_one_step(resolved, lease_id=new_lease)
    assert final.status == "terminal"


# ---------------------------------------------------------------------------
# (b) TRAILING auto-allowed call after the approval-requiring one.
# ---------------------------------------------------------------------------


def test_trailing_call_gets_synthesized_result_before_suspend() -> None:
    write = ToolCall(tool_name="write", arguments={"k": "w"}, call_id="c-write")
    grep = ToolCall(tool_name="grep", arguments={"k": "g"}, call_id="c-grep")
    engine, log, _disp, lease_id, task = _build(calls=[write, grep])

    suspended = engine.run_one_step(task, lease_id=lease_id)

    assert suspended.status == "suspended"
    assert suspended.wake_on.handle == "approval-c-write"
    # The trailing ``grep`` was never invoked (no real tool envelope)...
    types = [e.type for e in log.read(task.task_id)]
    assert "ToolCallStarted" not in types
    # ...but it still has a synthesized FAILED result so its tool_use is not
    # left dangling across the suspend boundary.
    result_ids = _result_call_ids(suspended)
    assert "c-grep" in result_ids
    assert "c-write" not in result_ids  # answered on resume
    grep_block = next(
        b
        for m in suspended.runtime.messages
        if m.role == "tool"
        for b in m.content
        if isinstance(b, ToolResultBlock) and b.call_id == "c-grep"
    )
    assert grep_block.success is False
    assert grep_block.error  # non-empty skip reason


def test_trailing_call_balanced_after_approve_resume() -> None:
    write = ToolCall(tool_name="write", arguments={"k": "w"}, call_id="c-write")
    grep = ToolCall(tool_name="grep", arguments={"k": "g"}, call_id="c-grep")
    engine, _log, disp, lease_id, task = _build(
        calls=[write, grep], second=FinishDecision(answer="done")
    )
    suspended = engine.run_one_step(task, lease_id=lease_id)
    woken, new_lease = _wake_and_lease(
        disp, engine, suspended, lease_id, handle="approval-c-write"
    )

    resolved = engine.resolve_tool_approval(
        woken, call_id="c-write", approved=True, resolver="host",
        lease_id=new_lease,
    )

    # Every tool_use the model emitted (write + grep) has a matching result.
    assert {"c-write", "c-grep"} <= _result_call_ids(resolved)
    final = engine.run_one_step(resolved, lease_id=new_lease)
    assert final.status == "terminal"


def test_trailing_call_balanced_after_deny_resume() -> None:
    write = ToolCall(tool_name="write", arguments={"k": "w"}, call_id="c-write")
    grep = ToolCall(tool_name="grep", arguments={"k": "g"}, call_id="c-grep")
    engine, _log, disp, lease_id, task = _build(
        calls=[write, grep], second=FinishDecision(answer="done")
    )
    suspended = engine.run_one_step(task, lease_id=lease_id)
    woken, new_lease = _wake_and_lease(
        disp, engine, suspended, lease_id, handle="approval-c-write"
    )

    resolved = engine.resolve_tool_approval(
        woken, call_id="c-write", approved=False, reason="nope",
        resolver="host", lease_id=new_lease,
    )

    # Deny path is balanced too: grep's synthesized result (from suspend) +
    # write's denial feedback (from resume) cover both tool_use blocks.
    assert {"c-write", "c-grep"} <= _result_call_ids(resolved)
    final = engine.run_one_step(resolved, lease_id=new_lease)
    assert final.status == "terminal"
