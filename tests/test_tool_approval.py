"""Phase 4.5 Issue A — interactive tool-call approval (HITL) at the
runtime layer.

Exercises the full approve/deny resume contract on the generic Engine:

* `PermissionGuard.require_approval_tools` makes a real tool return
  `require_approval`;
* the suspend emits the durable `ToolCallApprovalRequested` anchor and
  populates `governance.pending_approvals`;
* `Engine.resolve_tool_approval` (the public seam the worker/runner calls
  after `note_woken`) emits the single authoritative
  `ToolCallApprovalResolved` and either invokes the recovered call
  (approve) or appends a `role="tool"` denial-feedback message (deny);
* a stale/duplicate resolution raises `ApprovalNotPending` and emits
  nothing;
* the pending anchor survives a fresh fold (process-restart robustness),
  including through SQLite.

These are runtime-layer tests; the `noeta code` end-to-end slices live
separately.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.hooks import HookManager
from noeta.core.wiring import wire_default_observers
from noeta.guards.permission import PermissionGuard, PermissionPolicy
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, ToolCall, ToolCallsDecision
from noeta.protocols.errors import ApprovalNotPending
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.messages import ToolResultBlock
from noeta.protocols.values import EVENT_PAYLOAD_MAX_BYTES, ContentRef
from noeta.protocols.wake import HumanResponseReceived
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.storage.sqlite import SqliteContentStore, SqliteEventLog
from noeta.testing.composer import trivial_three_segment
from noeta.tools.fake import FakeTool


CALL_ID = "c-bar"
HANDLE = f"approval-{CALL_ID}"


def _bar_call() -> ToolCall:
    return ToolCall(tool_name="bar", arguments={"k": "x"}, call_id=CALL_ID)


def _build(
    *,
    db: str | None = None,
    second: Any = None,
    first: ToolCall | None = None,
) -> tuple[Engine, Any, Any, InMemoryDispatcher, str, Any]:
    """Engine wired with PermissionGuard gating ``bar`` for approval.

    The policy proposes one ``bar`` tool call (``first``, default
    :func:`_bar_call`), then (``second``) a follow-up decision used to
    drive the resumed loop to terminal. Pass ``db`` to back the run with
    SQLite storage (else in-memory).
    """
    dispatcher = InMemoryDispatcher()
    if db is not None:
        content_store: Any = SqliteContentStore(db)
        event_log: Any = SqliteEventLog(db, lease_validator=dispatcher)
    else:
        content_store = InMemoryContentStore()
        event_log = InMemoryEventLog(lease_validator=dispatcher)
    wire_default_observers(event_log, dispatcher)
    composer = trivial_three_segment(content_store)
    bar = FakeTool(name="bar", script={("x",): "out"})
    tool_runtime = ToolRuntime(event_log=event_log, content_store=content_store)
    decisions: list[Any] = [ToolCallsDecision(calls=[first or _bar_call()])]
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
    return engine, event_log, content_store, dispatcher, lease.lease_id, task


def _suspend_for_approval(engine: Engine, task: Any, lease_id: str) -> Any:
    suspended = engine.run_one_step(task, lease_id=lease_id)
    assert suspended.status == "suspended"
    assert isinstance(suspended.wake_on, HumanResponseReceived)
    assert suspended.wake_on.handle == HANDLE
    return suspended


def _wake_and_lease(
    dispatcher: InMemoryDispatcher, engine: Engine, task: Any, lease_id: str
) -> tuple[Any, str]:
    """Drive the real wake → targeted-lease → note_woken contract."""
    dispatcher.release(lease_id, next_state="suspended", wake_on=task.wake_on)
    dispatcher.wake(task.task_id, HumanResponseReceived(handle=HANDLE))
    lease = dispatcher.lease(worker_id="w", task_id=task.task_id)
    assert lease is not None and lease.wake_event is not None
    woken = engine.note_woken(
        task, lease_id=lease.lease_id, wake_event=lease.wake_event
    )
    return woken, lease.lease_id


# ---------------------------------------------------------------------------
# suspend: the durable anchor
# ---------------------------------------------------------------------------


def test_require_approval_emits_anchor_and_populates_pending() -> None:
    engine, log, _cs, _disp, lease_id, task = _build()
    suspended = _suspend_for_approval(engine, task, lease_id)

    types = [e.type for e in log.read(task.task_id)]
    # anchor emitted BEFORE the suspend (and before the snapshot).
    assert "ToolCallApprovalRequested" in types
    assert types.index("ToolCallApprovalRequested") < types.index("TaskSuspended")
    assert types.index("ToolCallApprovalRequested") < types.index("TaskSnapshot")
    # tool did not run.
    assert "ToolCallStarted" not in types

    pending = suspended.governance.pending_approvals
    assert pending == {CALL_ID: {"tool_name": "bar", "arguments": {"k": "x"}}}


def test_oversized_approval_arguments_are_offloaded_and_recovered() -> None:
    # A gated call whose arguments exceed the 4-KB envelope ceiling: before
    # the offload, emitting the ToolCallApprovalRequested anchor raised
    # PayloadTooLarge and the suspend never happened.
    big_args = {"k": "x", "blob": "y" * (EVENT_PAYLOAD_MAX_BYTES + 1000)}
    big_call = ToolCall(tool_name="bar", arguments=big_args, call_id=CALL_ID)
    engine, log, _cs, _disp, lease_id, task = _build(first=big_call)

    suspended = _suspend_for_approval(engine, task, lease_id)

    anchor = [
        e for e in log.read(task.task_id)
        if e.type == "ToolCallApprovalRequested"
    ][0]
    # Arguments went by reference, and the anchor payload itself now fits
    # comfortably under the ceiling the EventLog enforces.
    assert anchor.payload.arguments is None
    assert isinstance(anchor.payload.arguments_ref, ContentRef)
    assert len(to_canonical_bytes(anchor.payload)) <= EVENT_PAYLOAD_MAX_BYTES
    # The durable recovery anchor still recovers the FULL arguments: the
    # fold dereferences them back so resume reconstructs the exact call.
    assert suspended.governance.pending_approvals == {
        CALL_ID: {"tool_name": "bar", "arguments": big_args}
    }


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


def test_approve_runs_recovered_call_and_records_resolution() -> None:
    engine, log, _cs, disp, lease_id, task = _build(
        second=FinishDecision(answer="done")
    )
    suspended = _suspend_for_approval(engine, task, lease_id)
    woken, new_lease = _wake_and_lease(disp, engine, suspended, lease_id)

    resolved = engine.resolve_tool_approval(
        woken, call_id=CALL_ID, approved=True, resolver="host", lease_id=new_lease
    )

    types = [e.type for e in log.read(task.task_id)]
    # resolution, then the SAME call runs through the normal tool path.
    assert types.index("ToolCallApprovalResolved") < types.index("ToolCallStarted")
    for t in ("ToolCallStarted", "ToolResultRecorded", "ToolCallFinished"):
        assert t in types
    # governance: audited as approved, pending cleared, not denied.
    assert resolved.governance.pending_approvals == {}
    assert resolved.governance.approvals == [
        {
            "call_id": CALL_ID,
            "tool_name": "bar",
            "approved": True,
            "reason": None,
            "resolver": "host",
        }
    ]
    assert resolved.governance.denied == []

    # the resumed loop continues cleanly to terminal (no re-suspend).
    final = engine.run_one_step(resolved, lease_id=new_lease)
    assert final.status == "terminal"


# ---------------------------------------------------------------------------
# deny
# ---------------------------------------------------------------------------


def test_deny_appends_feedback_message_and_does_not_run_tool() -> None:
    engine, log, cs, disp, lease_id, task = _build(
        second=FinishDecision(answer="done")
    )
    suspended = _suspend_for_approval(engine, task, lease_id)
    woken, new_lease = _wake_and_lease(disp, engine, suspended, lease_id)

    resolved = engine.resolve_tool_approval(
        woken,
        call_id=CALL_ID,
        approved=False,
        reason="not allowed in prod",
        resolver="host",
        lease_id=new_lease,
    )

    types = [e.type for e in log.read(task.task_id)]
    # single authoritative resolution; NO separate ToolCallDenied; tool
    # never ran; a MessagesAppended (the feedback) follows the resolution.
    assert "ToolCallDenied" not in types
    assert "ToolCallStarted" not in types
    assert types.index("MessagesAppended", types.index("ToolCallApprovalResolved")) \
        > types.index("ToolCallApprovalResolved")

    # the denial-feedback message: role="tool", same call_id, failure.
    last_msg = resolved.runtime.messages[-1]
    assert last_msg.role == "tool"
    block = last_msg.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.call_id == CALL_ID
    assert block.success is False
    assert block.error == "not allowed in prod"

    # governance: audited in approvals AND denied; pending cleared.
    assert resolved.governance.pending_approvals == {}
    assert resolved.governance.approvals[0]["approved"] is False
    assert any(
        d.get("call_id") == CALL_ID and d.get("type") == "ToolCallApprovalResolved"
        for d in resolved.governance.denied
    )

    final = engine.run_one_step(resolved, lease_id=new_lease)
    assert final.status == "terminal"


# ---------------------------------------------------------------------------
# stale / duplicate — fail closed, emit nothing
# ---------------------------------------------------------------------------


def test_resolve_unknown_call_id_raises_and_emits_nothing() -> None:
    engine, log, _cs, disp, lease_id, task = _build()
    suspended = _suspend_for_approval(engine, task, lease_id)
    woken, new_lease = _wake_and_lease(disp, engine, suspended, lease_id)
    before = len(log.read(task.task_id))

    with pytest.raises(ApprovalNotPending):
        engine.resolve_tool_approval(
            woken, call_id="c-nope", approved=True, lease_id=new_lease
        )
    assert len(log.read(task.task_id)) == before


def test_duplicate_resolution_raises_and_emits_no_second_event() -> None:
    engine, log, _cs, disp, lease_id, task = _build(
        second=FinishDecision(answer="done")
    )
    suspended = _suspend_for_approval(engine, task, lease_id)
    woken, new_lease = _wake_and_lease(disp, engine, suspended, lease_id)
    engine.resolve_tool_approval(
        woken, call_id=CALL_ID, approved=True, lease_id=new_lease
    )
    after_first = [e.type for e in log.read(task.task_id)].count(
        "ToolCallApprovalResolved"
    )
    assert after_first == 1

    with pytest.raises(ApprovalNotPending):
        engine.resolve_tool_approval(
            woken, call_id=CALL_ID, approved=True, lease_id=new_lease
        )
    again = [e.type for e in log.read(task.task_id)].count(
        "ToolCallApprovalResolved"
    )
    assert again == 1


# ---------------------------------------------------------------------------
# restart robustness — recover the pending call from a fresh fold
# ---------------------------------------------------------------------------


def test_restart_fold_recovers_pending_and_approves(tmp_path: Path) -> None:
    """Drop the in-memory task; rebuild purely from the EventLog (+
    snapshot) and resolve — proving the approved call is recoverable
    after a process restart, not just from runner memory (watchpoint #2)."""
    db = str(tmp_path / "approval.sqlite")
    engine, log, cs, disp, lease_id, task = _build(
        db=db, second=FinishDecision(answer="done")
    )
    suspended = _suspend_for_approval(engine, task, lease_id)
    _woken, new_lease = _wake_and_lease(disp, engine, suspended, lease_id)

    # Throw away the in-memory task; fold from durable state alone.
    rebuilt = fold(log, cs, task.task_id)
    assert rebuilt.governance.pending_approvals == {
        CALL_ID: {"tool_name": "bar", "arguments": {"k": "x"}}
    }

    resolved = engine.resolve_tool_approval(
        rebuilt, call_id=CALL_ID, approved=True, lease_id=new_lease
    )
    types = [e.type for e in log.read(task.task_id)]
    assert "ToolResultRecorded" in types
    assert resolved.governance.pending_approvals == {}


# ---------------------------------------------------------------------------
# SQLite typed-payload round-trip for the two new events
# ---------------------------------------------------------------------------


def test_sqlite_round_trip_of_approval_events(tmp_path: Path) -> None:
    db = str(tmp_path / "rt.sqlite")
    engine, log, cs, disp, lease_id, task = _build(
        db=db, second=FinishDecision(answer="done")
    )
    suspended = _suspend_for_approval(engine, task, lease_id)
    woken, new_lease = _wake_and_lease(disp, engine, suspended, lease_id)
    engine.resolve_tool_approval(
        woken, call_id=CALL_ID, approved=True, resolver="host", lease_id=new_lease
    )

    by_type = {e.type: e for e in log.read(task.task_id)}
    req = by_type["ToolCallApprovalRequested"].payload
    assert req.call_id == CALL_ID
    assert req.tool_name == "bar"
    assert req.arguments == {"k": "x"}
    res = by_type["ToolCallApprovalResolved"].payload
    assert res.approved is True
    assert res.resolver == "host"
    assert res.call_id == CALL_ID
