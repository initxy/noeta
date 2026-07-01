"""Engine-side emission of the StepTransition tag (Foundation B, D-B2 / D-B6).

Only **non-default** continuations emit a ``StepTransitionMarked`` event;
the implicit ``next_turn`` default does not. The single real non-default
continuation that exists today is the approval-resume path
(``Engine.resolve_tool_approval(approved=True)``) — the other reasons are
reserved for ②/③. The emit logic lives in the ``_decision_handlers``
module-level helper (``emit_step_transition``), not in the Engine body
(D-B6, ≤500-line budget).
"""

from __future__ import annotations

from typing import Any

from noeta.core.engine import Engine
from noeta.core.hooks import HookManager
from noeta.core.wiring import wire_default_observers
from noeta.guards.permission import PermissionGuard, PermissionPolicy
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, ToolCall, ToolCallsDecision
from noeta.protocols.wake import HumanResponseReceived
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment
from noeta.tools.fake import FakeTool


CALL_ID = "c-bar"
HANDLE = f"approval-{CALL_ID}"


def _build(second: Any = None):
    dispatcher = InMemoryDispatcher()
    content_store = InMemoryContentStore()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    wire_default_observers(event_log, dispatcher)
    composer = trivial_three_segment(content_store)
    bar = FakeTool(name="bar", script={("x",): "out"})
    tool_runtime = ToolRuntime(event_log=event_log, content_store=content_store)
    decisions: list[Any] = [
        ToolCallsDecision(
            calls=[ToolCall(tool_name="bar", arguments={"k": "x"}, call_id=CALL_ID)]
        )
    ]
    if second is not None:
        decisions.append(second)
    policy = StubScriptedPolicy(decisions)
    hooks = HookManager()
    hooks.register(
        PermissionGuard(
            PermissionPolicy(require_approval_tools=frozenset({"bar"})),
            tools={"bar": bar},
        )
    )
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=policy,
        tools={"bar": bar},
        tool_runtime=tool_runtime,
        hooks=hooks,
    )
    task = engine.create_task(goal="approval-test", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None
    return engine, event_log, dispatcher, lease.lease_id, task


def _wake_and_lease(dispatcher, engine, task, lease_id):
    dispatcher.release(lease_id, next_state="suspended", wake_on=task.wake_on)
    dispatcher.wake(task.task_id, HumanResponseReceived(handle=HANDLE))
    lease = dispatcher.lease(worker_id="w", task_id=task.task_id)
    assert lease is not None and lease.wake_event is not None
    woken = engine.note_woken(
        task, lease_id=lease.lease_id, wake_event=lease.wake_event
    )
    return woken, lease.lease_id


def test_approval_resume_emits_step_transition_marked() -> None:
    """D-B2: approving a pending call is a non-default continuation, so the
    Engine emits a ``StepTransitionMarked(reason='approval_resume')`` and
    fold projects it onto ``last_transition``."""
    engine, log, disp, lease_id, task = _build(
        second=FinishDecision(answer="done")
    )
    suspended = engine.run_one_step(task, lease_id=lease_id)
    assert suspended.status == "suspended"
    woken, new_lease = _wake_and_lease(disp, engine, suspended, lease_id)

    resolved = engine.resolve_tool_approval(
        woken, call_id=CALL_ID, approved=True, resolver="host", lease_id=new_lease
    )

    types = [e.type for e in log.read(task.task_id)]
    assert "StepTransitionMarked" in types
    # The tag is the resume marker — it sits on the resume path, after the
    # resolution event.
    marks = [
        e for e in log.read(task.task_id) if e.type == "StepTransitionMarked"
    ]
    assert [m.payload.reason for m in marks] == ["approval_resume"]
    assert resolved.runtime.last_transition == "approval_resume"


def test_deny_does_not_emit_step_transition_marked() -> None:
    """A denial is not a continuation of the tool loop — it appends denial
    feedback. It must NOT emit an approval_resume tag (the loop did not
    resume the call)."""
    engine, log, disp, lease_id, task = _build(
        second=FinishDecision(answer="done")
    )
    suspended = engine.run_one_step(task, lease_id=lease_id)
    woken, new_lease = _wake_and_lease(disp, engine, suspended, lease_id)

    engine.resolve_tool_approval(
        woken, call_id=CALL_ID, approved=False, reason="no", lease_id=new_lease
    )

    marks = [
        e for e in log.read(task.task_id) if e.type == "StepTransitionMarked"
    ]
    assert marks == []


def test_plain_tool_loop_does_not_emit_next_turn_tag() -> None:
    """D-B2: the implicit ``next_turn`` default is NOT emitted. A normal
    tool-calls round-trip that loops back must produce NO
    ``StepTransitionMarked`` event (stream-volume control)."""
    dispatcher = InMemoryDispatcher()
    content_store = InMemoryContentStore()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    wire_default_observers(event_log, dispatcher)
    composer = trivial_three_segment(content_store)
    bar = FakeTool(name="bar", script={("x",): "out"})
    tool_runtime = ToolRuntime(event_log=event_log, content_store=content_store)
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(tool_name="bar", arguments={"k": "x"}, call_id="c1")
                ]
            ),
            FinishDecision(answer="done"),
        ]
    )
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=policy,
        tools={"bar": bar},
        tool_runtime=tool_runtime,
        hooks=HookManager(),
    )
    task = engine.create_task(goal="loop", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None
    final = engine.run_one_step(task, lease_id=lease.lease_id)
    assert final.status == "terminal"
    types = [e.type for e in event_log.read(task.task_id)]
    assert "StepTransitionMarked" not in types
