"""Round 3a single-host-multi-worker integration tests.

Covers the resident worker pool replacing the per-command daemon thread
in ``EngineRoom`` (T5 background_drive):

* ``num_workers=N`` drives tasks concurrently; N>1 actually runs steps
  in parallel (CAS on the dispatcher queue prevents duplicate leases).
* The non-durable ``ResolveApprovalPrelude`` stashed at seed time by
  approve() is picked up by a worker and applied exactly once.
* Unit coverage of ``_render_subtask_wake``: the prelude correctly pairs
  a dangling ``spawn_subagent`` tool_use when processing a
  ``SubtaskCompleted`` wake on the worker thread (prevents the composer
  from rejecting an unpaired tool_use).

The first two tests drive the real EngineRoom + Client stack (same seam
``test_backend_background_drive.py`` uses). The third covers the
subtask-rendering prelude at the WorkerLoop unit level because the
full two-agent fan-out through the LLM driver is already exercised by
``test_subtask_full_loop.py`` / ``test_subtask_fanout.py`` for the
synchronous path; what is new in 3a is that the prelude now runs on a
resident worker rather than on the in-request drain.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from noeta.agent.backend import EngineRoom
from noeta.core.engine import Engine
from noeta.core.wiring import wire_default_observers
from noeta.protocols.events import SubtaskSpawnedPayload
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.wake import SubtaskCompleted, SubtaskResult
from noeta.runtime.worker import _render_subtask_wake
from noeta.sdk import Options
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment
from noeta.testing.fake_llm import FakeLLMProvider


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def _bypass_options() -> Options:
    return Options(
        system_prompt="you finish immediately",
        name="main",
        allowed_tools=(),
        permission_mode="bypassPermissions",
    )


def _room(
    workspace: Path,
    provider: FakeLLMProvider,
    *,
    num_workers: int = 2,
    background: bool = True,
    options: Options | None = None,
) -> EngineRoom:
    return EngineRoom(
        options or _bypass_options(),
        provider=provider,
        workspace_dir=workspace,
        background_drive=background,
        num_workers=num_workers,
    )


# ---------------------------------------------------------------------------
# 1. concurrent tasks with 2 workers
# ---------------------------------------------------------------------------


def test_multiworker_drives_concurrent_tasks_to_settled(tmp_path: Path) -> None:
    """With num_workers=2, starting 4 fast tasks from the main thread
    yields 4 tasks all reaching the suspended state (multi-turn NEXT_GOAL
    wrapper), every LLM round-trip is invoked exactly once (no
    double-drive / duplicate lease CAS losers), and join_drives returns
    True within a bounded wait."""
    call_lock = threading.Lock()
    calls: list[str] = []

    def _responder(request: LLMRequest) -> LLMResponse:
        with call_lock:
            calls.append("x")
        return _end("ok")

    provider = FakeLLMProvider(responder=_responder)
    room = _room(tmp_path, provider, num_workers=2)
    try:
        ids = [room.start(goal=f"g{i}") for i in range(4)]
        assert room.join_drives(timeout=15.0), "tasks did not settle"
        assert len(calls) == 4, (
            f"expected exactly 4 LLM calls, got {len(calls)} (duplicate drive?)"
        )
        for tid in ids:
            types = [e.type for e in room.events(tid)]
            assert "TaskSuspended" in types, (
                f"task {tid} never suspended: types={types}"
            )
            assert "TaskFailed" not in types, (
                f"task {tid} failed: {types}"
            )
    finally:
        room.shutdown()


def test_multiworker_two_workers_actually_parallelize(tmp_path: Path) -> None:
    """Two workers must make progress on two tasks in parallel (one worker
    doesn't hog the queue). Both LLM responder invocations must be
    entered at the same time (peak concurrency == 2)."""
    in_llm = threading.Event()
    unblock = threading.Event()
    concurrent_lock = threading.Lock()
    inside = 0
    peak = 0

    def _responder(request: LLMRequest) -> LLMResponse:
        nonlocal inside, peak
        with concurrent_lock:
            inside += 1
            peak = max(peak, inside)
            if inside >= 2:
                in_llm.set()
        try:
            assert unblock.wait(timeout=10.0), "test released"
            return _end("ok")
        finally:
            with concurrent_lock:
                inside -= 1

    provider = FakeLLMProvider(responder=_responder)
    room = _room(tmp_path, provider, num_workers=2)
    try:
        t1 = room.start(goal="a")
        t2 = room.start(goal="b")
        assert in_llm.wait(timeout=10.0), (
            "two workers never entered LLM concurrently"
        )
        unblock.set()
        assert room.join_drives(timeout=10.0)
        assert peak == 2, f"expected 2 concurrent LLM calls, peak was {peak}"
        for tid in (t1, t2):
            assert "TaskSuspended" in [e.type for e in room.events(tid)]
    finally:
        unblock.set()
        room.shutdown()


# ---------------------------------------------------------------------------
# 2. ResolveApprovalPrelude cross-thread handoff
# ---------------------------------------------------------------------------


def test_multiworker_approve_handoff_executes_tool_once(tmp_path: Path) -> None:
    """An approval-required tool (write, under permission_mode=default)
    suspends on approval-w1. approve() on the main thread seeds the wake,
    stashes a ResolveApprovalPrelude on the host, and yields the lease. A
    worker picks the lease up, pops the prelude, runs the approved tool
    (exactly once), and drives the turn to the next suspension. The
    post-tool LLM call (where the model sees the ToolResult) also happens
    exactly once."""
    finish_lock = threading.Lock()
    finish_calls = 0

    def _responder(request: LLMRequest) -> LLMResponse:
        nonlocal finish_calls
        has_result = any(
            isinstance(b, ToolResultBlock)
            for m in request.messages
            for b in (m.content or [])
        )
        if has_result:
            with finish_lock:
                finish_calls += 1
            return _end("wrote it")
        return LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="w1",
                    tool_name="write",
                    arguments={"path": "out.txt", "content": "hello"},
                )
            ],
            usage=Usage(uncached=1, output=1),
        )

    opts = Options(
        system_prompt="you write a file then finish",
        name="main",
        allowed_tools=("write",),
        permission_mode="default",
    )
    provider = FakeLLMProvider(responder=_responder)
    room = _room(tmp_path, provider, num_workers=2, options=opts)
    try:
        tid = room.start(goal="create out.txt")
        assert room.join_drives(timeout=10.0), "first turn did not settle to approval"
        types0 = [e.type for e in room.events(tid)]
        assert "ToolCallApprovalRequested" in types0, types0
        # Approve → prelude stashed on host, lease yielded; worker drives
        # the approved tool + one more LLM round to finish.
        room.approve(tid, call_id="w1")
        assert room.join_drives(timeout=10.0), "post-approve turn did not settle"
        types1 = [e.type for e in room.events(tid)]
        assert "ToolCallApprovalResolved" in types1, types1
        assert "ToolResultRecorded" in types1, types1
        with finish_lock:
            assert finish_calls == 1, (
                f"post-tool LLM called {finish_calls} times (expected 1)"
            )
    finally:
        room.shutdown()


# ---------------------------------------------------------------------------
# 3. _render_subtask_wake prelude pairs a dangling spawn tool_use
#
# When a parent task is woken by SubtaskCompleted on a resident worker,
# _render_subtask_wake must append the paired ToolResultBlock between
# note_woken and run_one_step. If it did not, the composer would hand the
# provider an unpaired tool_use and the request would fail.
# ---------------------------------------------------------------------------


def _make_triple() -> tuple[InMemoryEventLog, InMemoryContentStore, InMemoryDispatcher]:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)
    return log, InMemoryContentStore(), disp


def test_render_subtask_wake_pairs_single_spawn() -> None:
    """Single spawn: a parent transcript has an assistant message with an
    unpaired spawn_subagent tool_use, governance has one SubtaskResult;
    _render_subtask_wake appends the pairing ToolResultBlock exactly
    once and is idempotent on re-entry."""
    log, cs, disp = _make_triple()
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
    )
    parent = engine.create_task(goal="p", policy_name="react")
    # Lease the parent so the lease is valid for subsequent appends.
    disp.enqueue(parent.task_id)
    lease = disp.lease(worker_id="w", lease_seconds=60.0)
    assert lease is not None
    lease_id = lease.lease_id

    # Seed: append an assistant message with an unpaired spawn_subagent
    # tool_use (mirroring what composer + decision flow produces).
    spawn_call_id = "spawn-1"
    parent.runtime.messages.append(
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    call_id=spawn_call_id,
                    tool_name="spawn_subagent",
                    arguments={"agent": "helper", "goal": "g"},
                )
            ],
        )
    )
    # Record a completed child result on governance (normally folded
    # from SubtaskCompleted event).
    parent.governance.subtask_results.append(
        SubtaskResult(status="completed", output="child-answer")
    )
    # Sanity: one unpaired tool_use before rendering.
    def _unpaired(t: Any) -> list[tuple[str, str]]:
        resolved: set[str] = set()
        for m in t.runtime.messages:
            if m.role == "tool":
                for b in m.content or []:
                    if isinstance(b, ToolResultBlock):
                        resolved.add(b.call_id)
        return [
            (b.tool_name, b.call_id)
            for m in t.runtime.messages
            if m.role == "assistant"
            for b in (m.content or [])
            if isinstance(b, ToolUseBlock)
            and b.tool_name in ("spawn_subagent", "run_workflow")
            and b.call_id not in resolved
        ]

    assert _unpaired(parent) == [("spawn_subagent", spawn_call_id)]

    wake = SubtaskCompleted(subtask_id="child-1")
    updated = _render_subtask_wake(engine, parent, wake, lease_id=lease_id)
    # After render: no unpaired tool_use, and one tool message appended.
    assert _unpaired(updated) == []
    assert any(
        m.role == "tool"
        and any(
            isinstance(b, ToolResultBlock) and b.call_id == spawn_call_id
            for b in (m.content or [])
        )
        for m in updated.runtime.messages
    ), "expected a tool message pairing spawn_call_id"

    # Idempotent: calling again on the already-paired task returns
    # unchanged (no duplicate append).
    msg_count_before = len(updated.runtime.messages)
    updated2 = _render_subtask_wake(engine, updated, wake, lease_id=lease_id)
    assert len(updated2.runtime.messages) == msg_count_before, (
        "_render_subtask_wake must be idempotent"
    )
    assert _unpaired(updated2) == []
    disp.release(lease_id, next_state="suspended")


def test_render_subtask_wake_no_op_for_non_subtask_wake() -> None:
    """A HumanResponseReceived wake (non-subtask) is a no-op: the
    transcript is not touched."""
    log, cs, disp = _make_triple()
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
    )
    parent = engine.create_task(goal="p", policy_name="react")
    parent.runtime.messages.append(
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    call_id="spawn-1",
                    tool_name="spawn_subagent",
                    arguments={"agent": "h", "goal": "g"},
                )
            ],
        )
    )
    parent.governance.subtask_results.append(
        SubtaskResult(status="completed", output="x")
    )
    from noeta.protocols.wake import HumanResponseReceived

    before_count = len(parent.runtime.messages)
    updated = _render_subtask_wake(
        engine, parent, HumanResponseReceived(handle="next-goal"), lease_id="L"
    )
    assert len(updated.runtime.messages) == before_count


def test_render_subtask_wake_pairs_result_by_subtask_id_not_last() -> None:
    """Reviewer finding 1: when a parent has spawned two subtasks (A, B)
    and both have completed, a SubtaskCompleted(A) wake must pair
    spawn-A's call_id with A's result — NOT the most-recent unpaired
    call_id (which may be B's) and NOT governance.subtask_results[-1]
    (which is B's result when B finished second).

    Construct:
    - transcript: assistant message with call_a (spawn A) then call_b
      (spawn B) — both unpaired
    - event log: two SubtaskSpawned events in [A, B] order (matching
      message order, because _apply_decision_assistant_message fires
      before handle_spawn_subtask's emit)
    - governance.subtask_results: [A_result, B_result] (A completed first)
    - wake_event: SubtaskCompleted(A, result=A_result)
    After render: the NEW tool_result message must carry call_id=call_a
    and output=A_result.output.
    """
    log, cs, disp = _make_triple()
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
    )
    parent = engine.create_task(goal="p", policy_name="react")
    # Lease so we can emit events under a valid lease.
    disp.enqueue(parent.task_id)
    lease = disp.lease(worker_id="w", lease_seconds=60.0)
    assert lease is not None
    lease_id = lease.lease_id

    call_a = "call-A"
    call_b = "call-B"
    subtask_a = "subtask-A"
    subtask_b = "subtask-B"
    result_a = SubtaskResult(status="completed", output="A-done")
    result_b = SubtaskResult(status="completed", output="B-done")

    # Seed assistant message with the two spawn tool_uses (forward order).
    parent.runtime.messages.append(
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    call_id=call_a,
                    tool_name="spawn_subagent",
                    arguments={"agent": "a", "goal": "ga"},
                ),
                ToolUseBlock(
                    call_id=call_b,
                    tool_name="spawn_subagent",
                    arguments={"agent": "b", "goal": "gb"},
                ),
            ],
        )
    )
    # Emit the two SubtaskSpawned events (matching message order A then B).
    log.emit(
        task_id=parent.task_id,
        type="SubtaskSpawned",
        payload=SubtaskSpawnedPayload(
            subtask_id=subtask_a, agent_name="a", goal="ga"
        ),
        lease_id=lease_id,
        actor="engine",
        origin="engine",
    )
    log.emit(
        task_id=parent.task_id,
        type="SubtaskSpawned",
        payload=SubtaskSpawnedPayload(
            subtask_id=subtask_b, agent_name="b", goal="gb"
        ),
        lease_id=lease_id,
        actor="engine",
        origin="engine",
    )
    # Seed governance.subtask_results in completion order (A then B).
    parent.governance.subtask_results.append(result_a)
    parent.governance.subtask_results.append(result_b)

    # Wake for A (not B). wake_event.result carries A's result, as
    # ChildLifecycleObserver sets it.
    wake = SubtaskCompleted(subtask_id=subtask_a, result=result_a)
    updated = _render_subtask_wake(engine, parent, wake, lease_id=lease_id)

    # Exactly one new tool message, pairing call_a with A's output.
    appended = [
        m for m in updated.runtime.messages
        if m.role == "tool"
        and any(
            isinstance(b, ToolResultBlock)
            for b in (m.content or [])
        )
    ]
    assert len(appended) == 1, (
        f"expected exactly one new tool_result, got "
        f"{[(b.call_id, b.output) for m in appended for b in (m.content or []) if isinstance(b, ToolResultBlock)]}"
    )
    block_a = next(
        b for b in (appended[0].content or [])
        if isinstance(b, ToolResultBlock)
    )
    # Bug reproduces here: old code picks call_b + B-done.
    assert block_a.call_id == call_a, (
        f"expected call_id={call_a}, got {block_a.call_id}"
    )
    assert block_a.success is True
    assert block_a.output == "A-done", (
        f"expected A's output, got {block_a.output!r}"
    )
    # B's call_id is still unpaired (we only rendered A's result).
    resolved_after = {
        b.call_id for m in updated.runtime.messages if m.role == "tool"
        for b in (m.content or []) if isinstance(b, ToolResultBlock)
    }
    assert call_b not in resolved_after, "call_b must remain unpaired"
    disp.release(lease_id, next_state="suspended")

