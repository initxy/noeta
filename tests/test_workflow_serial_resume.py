"""Serial agent() calls + reactive branching + crash-restart resume.

Acceptance criteria covered:

1. **Serial fan-out (>=3 sequential agent()) + each spawned exactly once** (AC1 + AC3):
   ``test_three_serial_agents_each_spawned_once`` — E2E run of a three-step script
   via the SDK host (SdkHost + InteractionDriver); asserts the orchestration
   subtask has exactly 3 SubtaskSpawned (no duplicates) and the final answer is correct.

2. **Reactive branching (if/else on the previous result)** (AC2):
   ``test_reactive_branch_taken`` — worker1 returns a string containing "bug" -> triggers a second agent().
   ``test_reactive_branch_skipped`` — worker1 returns no "bug" -> only 1 worker spawned.

3. **Cold-restart resume (recover from EventLog; completed agent() not re-run)** (AC4):
   ``test_cold_restart_resume_no_redispatch`` — manual stepping; after the first worker
   finishes, discard all in-memory objects and keep driving with a fresh
   OrchestrationPolicy instance + fresh Engine. Asserts: no duplicate
   SubtaskSpawned after restart, and correct final completion.

LLM call order (shared global FakeLLMProvider, three-step script):
  1. main agent -> run_workflow tool_use
  2. worker(explore) -> end_turn, emits step-a result
  3. worker(explore) -> end_turn, emits step-b result
  4. worker(explore) -> end_turn, emits step-c result
  5. main agent -> end_turn wrap-up
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.policies.control_tools import RUN_WORKFLOW_TOOL, WORKFLOW_AGENT_NAME
from noeta.policies.orchestration import OrchestrationPolicy, WORKFLOW_CALL_PREFIX
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.wake import SubtaskCompleted
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import (
    coding_replay_budget,
    make_driver,
    make_host,
    make_registry,
    preset_spec,
    runner_main_spec,
)


# ---------------------------------------------------------------------------
# Script constants
# ---------------------------------------------------------------------------

#: Three-step serial script: a -> b -> c, results concatenated in order.
SERIAL_SCRIPT = (
    'a = agent("step a", agent="explore")\n'
    'b = agent("step b", agent="explore")\n'
    'c = agent("step c", agent="explore")\n'
    'return a + "|" + b + "|" + c\n'
)

#: Reactive-branch script: if the first result contains "bug" -> spawn a second agent().
REACTIVE_SCRIPT = (
    'first = agent("classify the diff", agent="explore")\n'
    'if "bug" in first:\n'
    '    return agent("fix the bug", agent="explore")\n'
    'return "no action"\n'
)

STEP_A = "result-a"
STEP_B = "result-b"
STEP_C = "result-c"
FINAL_SERIAL_ANSWER = f"{STEP_A}|{STEP_B}|{STEP_C}"
FINAL_ANSWER = "workflow done"
WORKFLOW_CALL_ID = "wf-call"


# ---------------------------------------------------------------------------
# Shared helpers (aligned with test_workflow_run_tool_e2e.py)
# ---------------------------------------------------------------------------

def _run_workflow_call(script: str) -> LLMResponse:
    """LLM response for the main agent calling run_workflow."""
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=WORKFLOW_CALL_ID,
                tool_name=RUN_WORKFLOW_TOOL,
                arguments={"script": script},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": WORKFLOW_CALL_ID},
    )


def _end(text: str) -> LLMResponse:
    """An end_turn LLM response."""
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    return ws


def _session(ws: Path, *, script: str, responses: list[LLMResponse]):
    """A one-shot SDK host with workflow enabled (host ``workflow_allowed=True``
    + delegation on the main spec) that may delegate to ``explore``. ``script``
    is documentary — the real script rides the ``run_workflow`` tool call in
    ``responses``. The reserved ``__workflow__`` orchestration child is built by
    the host itself. Returns ``(host, driver)``."""
    main = runner_main_spec("main", delegation=True, spawnable=("explore",))
    host = make_host(
        make_registry(main, preset_spec("explore")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responses=responses),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        workflow_allowed=True,
        budget=coding_replay_budget(3),
    )
    return host, make_driver(host)


def _events_of(host, task_id: str) -> list[str]:
    return [e.type for e in host.event_log.read(task_id)]


def _child_ids(host, parent_id: str) -> list[str]:
    """Return the subtask_id list of every SubtaskSpawned under parent_id (ordered)."""
    return [
        str(e.payload.subtask_id)
        for e in host.event_log.read(parent_id)
        if e.type == "SubtaskSpawned"
    ]


# ---------------------------------------------------------------------------
# Low-level helpers for manual stepping (aligned with test_workflow_orchestration_policy.py)
# ---------------------------------------------------------------------------

def _make_runtime() -> tuple[InMemoryEventLog, InMemoryContentStore, InMemoryDispatcher]:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)
    return (log, InMemoryContentStore(), disp)


def _engine_for(
    *,
    log: InMemoryEventLog,
    cs: InMemoryContentStore,
    policy: Any,
) -> Engine:
    return Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=policy,
    )


def _run_single_worker(
    *,
    log: InMemoryEventLog,
    cs: InMemoryContentStore,
    disp: InMemoryDispatcher,
    answer: str,
) -> str:
    """Run the next pending worker (subtask) to terminal; return its task_id."""
    worker_engine = _engine_for(
        log=log,
        cs=cs,
        policy=StubScriptedPolicy([FinishDecision(answer=answer)]),
    )
    c_lease = disp.lease(worker_id="w1")
    assert c_lease is not None
    child_id = c_lease.task_id
    child = fold(log, cs, child_id)
    child = worker_engine.run_one_step(child, lease_id=c_lease.lease_id)
    assert child.status == "terminal"
    disp.release(c_lease.lease_id, next_state="terminal")
    return child_id


def _resume_orch(
    *,
    log: InMemoryEventLog,
    cs: InMemoryContentStore,
    disp: InMemoryDispatcher,
    orch_engine: Engine,
    orch_task_id: str,
    child_id: str,
    call_index: int,
) -> Any:
    """After the child completes, the observer wakes the orchestration task; re-lease + fold + note_woken + append + step.

    Returns the new orch Task object (either suspended or terminal).
    """
    o_lease = disp.lease(worker_id="w1")
    assert o_lease is not None and o_lease.task_id == orch_task_id

    orch = fold(log, cs, orch_task_id)
    result = orch.governance.subtask_results[-1]
    assert result.status == "completed"

    orch_engine.note_woken(
        orch,
        lease_id=o_lease.lease_id,
        wake_event=SubtaskCompleted(subtask_id=child_id),
    )
    orch = orch_engine.append_subagent_result_message(
        orch,
        call_id=f"{WORKFLOW_CALL_PREFIX}{call_index}",
        output=result.output,
        success=True,
        lease_id=o_lease.lease_id,
    )
    orch = orch_engine.run_one_step(orch, lease_id=o_lease.lease_id)

    if orch.status == "suspended":
        disp.release(o_lease.lease_id, next_state="suspended", wake_on=orch.wake_on)
    elif orch.status == "terminal":
        disp.release(o_lease.lease_id, next_state="terminal")
    return orch


# ===========================================================================
# Test 1: three serial steps + each spawned exactly once (AC1 + AC3)
# ===========================================================================

def test_three_serial_agents_each_spawned_once(tmp_path: Path) -> None:
    """Each agent() call in the three-step serial script is spawned exactly once; final answer concatenated in order.

    Covers AC1 (>=3 sequential agent(), each subtask spawned exactly once) + AC3 (stable positional mapping).

    LLM call order: run_workflow_call -> end(A) -> end(B) -> end(C) -> end(final),
    5 calls total. The orchestration Policy itself makes 0 LLM calls.
    """
    ws = _make_ws(tmp_path)
    host, driver = _session(
        ws,
        script=SERIAL_SCRIPT,
        responses=[
            _run_workflow_call(SERIAL_SCRIPT),
            _end(STEP_A),   # worker for "step a"
            _end(STEP_B),   # worker for "step b"
            _end(STEP_C),   # worker for "step c"
            _end(FINAL_ANSWER),  # main agent wrap-up
        ],
    )
    out = driver.start(goal="run a workflow", agent="main")
    assert out.status == "terminal"

    main_id = out.task_id

    # The main task has exactly 1 orchestration subtask (__workflow__).
    orch_ids = _child_ids(host, main_id)
    assert len(orch_ids) == 1, f"expected 1 orch subtask, got {orch_ids}"
    orch_id = orch_ids[0]

    # Confirm the orchestration subtask is the workflow agent.
    orch_created = next(
        e for e in host.event_log.read(orch_id) if e.type == "TaskCreated"
    )
    assert orch_created.payload.agent_name == WORKFLOW_AGENT_NAME

    # Exactly 3 workers (SubtaskSpawned) under the orchestration subtask, no duplicates.
    worker_ids = _child_ids(host, orch_id)
    assert len(worker_ids) == 3, (
        f"expected exactly 3 worker SubtaskSpawned, got {len(worker_ids)}: {worker_ids}"
    )
    # The 3 worker ids are all distinct (no duplicate dispatch).
    assert len(set(worker_ids)) == 3, f"duplicate worker ids detected: {worker_ids}"

    # All 3 workers are explore agents and end terminal.
    for wid in worker_ids:
        w_created = next(
            e for e in host.event_log.read(wid) if e.type == "TaskCreated"
        )
        assert w_created.payload.agent_name == "explore", wid
        assert "TaskCompleted" in [
            e.type for e in host.event_log.read(wid)
        ], f"worker {wid} not completed"

    # Orchestration subtask final answer = the three worker outputs concatenated in order.
    orch_completed = [
        e for e in host.event_log.read(orch_id) if e.type == "TaskCompleted"
    ]
    assert orch_completed, "orchestration task not completed"
    assert orch_completed[-1].payload.answer == FINAL_SERIAL_ANSWER, (
        f"expected '{FINAL_SERIAL_ANSWER}', got '{orch_completed[-1].payload.answer}'"
    )

    # All tasks are terminal (main + orchestration + 3 workers).
    for tid in [main_id, orch_id] + worker_ids:
        assert "TaskCompleted" in _events_of(host, tid), f"{tid} not completed"


# ===========================================================================
# Test 2a: reactive branch — bug path taken (AC2)
# ===========================================================================

def test_reactive_branch_taken(tmp_path: Path) -> None:
    """worker1 returns a string containing 'bug' -> if branch holds -> spawn a second agent().

    Covers AC2 (the branch follows the real result recorded in the EventLog).

    LLM call order: run_workflow -> end("found a bug here") -> end("fixed") -> end(final)
    """
    ws = _make_ws(tmp_path)
    first_output = "found a bug here"
    fix_output = "fixed"

    host, driver = _session(
        ws,
        script=REACTIVE_SCRIPT,
        responses=[
            _run_workflow_call(REACTIVE_SCRIPT),
            _end(first_output),  # classify worker -> contains "bug"
            _end(fix_output),    # fix worker (branch taken)
            _end(FINAL_ANSWER),
        ],
    )
    out = driver.start(goal="run a workflow", agent="main")
    assert out.status == "terminal"

    main_id = out.task_id
    orch_ids = _child_ids(host, main_id)
    assert len(orch_ids) == 1
    orch_id = orch_ids[0]

    # The orchestration subtask should have 2 workers (classify + fix).
    worker_ids = _child_ids(host, orch_id)
    assert len(worker_ids) == 2, (
        f"expected 2 workers (branch taken), got {len(worker_ids)}: {worker_ids}"
    )

    # The orchestration subtask answer should be the second worker's (fix) output.
    orch_completed = [
        e for e in host.event_log.read(orch_id) if e.type == "TaskCompleted"
    ]
    assert orch_completed[-1].payload.answer == fix_output, (
        f"expected '{fix_output}', got '{orch_completed[-1].payload.answer}'"
    )


# ===========================================================================
# Test 2b: reactive branch — bug path not taken (AC2)
# ===========================================================================

def test_reactive_branch_skipped(tmp_path: Path) -> None:
    """worker1 returns a string with no 'bug' -> if branch fails -> only 1 agent() spawned, answer='no action'.

    Covers AC2 (the branch follows the real result recorded in the EventLog: negative case).

    LLM call order: run_workflow -> end("all looks clean") -> end(final)
    """
    ws = _make_ws(tmp_path)
    first_output = "all looks clean"

    host, driver = _session(
        ws,
        script=REACTIVE_SCRIPT,
        responses=[
            _run_workflow_call(REACTIVE_SCRIPT),
            _end(first_output),  # classify worker -> no "bug"
            _end(FINAL_ANSWER),
        ],
    )
    out = driver.start(goal="run a workflow", agent="main")
    assert out.status == "terminal"

    main_id = out.task_id
    orch_ids = _child_ids(host, main_id)
    assert len(orch_ids) == 1
    orch_id = orch_ids[0]

    # Only 1 worker (classify) under the orchestration subtask, no fix worker.
    worker_ids = _child_ids(host, orch_id)
    assert len(worker_ids) == 1, (
        f"expected 1 worker (branch skipped), got {len(worker_ids)}: {worker_ids}"
    )

    # The orchestration subtask answer should be "no action" (the script returns the literal directly).
    orch_completed = [
        e for e in host.event_log.read(orch_id) if e.type == "TaskCompleted"
    ]
    assert orch_completed[-1].payload.answer == "no action", (
        f"expected 'no action', got '{orch_completed[-1].payload.answer}'"
    )


# ===========================================================================
# Test 3: cold-restart resume — completed agent() not re-run (AC4)
# ===========================================================================

def test_cold_restart_resume_no_redispatch() -> None:
    """Simulate a cold restart after the process is killed: once the first worker
    finishes, discard all in-memory objects and keep driving with a fresh
    OrchestrationPolicy + fresh Engine, folding from the EventLog.

    Asserts:
    - No duplicate SubtaskSpawned after restart (the first agent() is not re-spawned).
    - The orchestration task completes correctly with the right answer.

    Covers AC4 (cold-restart resume from EventLog; completed agent() passes instantly).
    The crux: the Policy is stateless — all state lives in the EventLog.
    """
    # --- Phase 1: set up the runtime; run the orchestration task until the first worker finishes ---
    log, cs, disp = _make_runtime()

    # Two-step script: a -> b, answer = a + "|" + b
    script = (
        'a = agent("step a", agent="worker")\n'
        'b = agent("step b", agent="worker")\n'
        'return a + "|" + b\n'
    )

    orch_engine_1 = _engine_for(
        log=log,
        cs=cs,
        policy=OrchestrationPolicy(script=script, args={}),
    )

    # Create and start the orchestration task.
    orch = orch_engine_1.create_task(goal="two-step workflow", policy_name="scripted")
    orch_task_id = orch.task_id
    disp.enqueue(orch_task_id)

    # Step 1: the orchestration task runs -> the script's first agent() suspends, spawns worker-0.
    o_lease_1 = disp.lease(worker_id="w1")
    assert o_lease_1 is not None and o_lease_1.task_id == orch_task_id
    orch = orch_engine_1.run_one_step(orch, lease_id=o_lease_1.lease_id)
    assert orch.status == "suspended"
    disp.release(o_lease_1.lease_id, next_state="suspended", wake_on=orch.wake_on)

    # Count the orchestration task's SubtaskSpawned events in the EventLog at this point.
    spawned_before = [
        e for e in log.read(orch_task_id) if e.type == "SubtaskSpawned"
    ]
    assert len(spawned_before) == 1, (
        f"expected 1 SubtaskSpawned before restart, got {len(spawned_before)}"
    )
    first_worker_call_id = spawned_before[0].payload.subtask_id

    # Run the first worker (step a) to completion.
    child_id_0 = _run_single_worker(
        log=log, cs=cs, disp=disp, answer="result-a"
    )

    # --- Phase 2: simulate the process restart ---
    # Discard all in-memory engine and task objects (orch, orch_engine_1 no longer used).
    # The EventLog and ContentStore stay alive (representing persistent storage).
    del orch, orch_engine_1

    # Use a fresh OrchestrationPolicy instance + fresh Engine (simulating reconstruction after restart).
    orch_engine_2 = _engine_for(
        log=log,
        cs=cs,
        policy=OrchestrationPolicy(script=script, args={}),  # fresh instance, no in-memory state
    )

    # Fully fold the orchestration task from the EventLog (simulating recovery from storage after restart).
    orch = fold(log, cs, orch_task_id)
    result_0 = orch.governance.subtask_results[-1]
    assert result_0.status == "completed"

    # Resume after restart: note_woken + append_subagent_result + run_one_step.
    o_lease_2 = disp.lease(worker_id="w1")
    assert o_lease_2 is not None and o_lease_2.task_id == orch_task_id

    orch_engine_2.note_woken(
        orch,
        lease_id=o_lease_2.lease_id,
        wake_event=SubtaskCompleted(subtask_id=child_id_0),
    )
    orch = orch_engine_2.append_subagent_result_message(
        orch,
        call_id=f"{WORKFLOW_CALL_PREFIX}0",
        output=result_0.output,
        success=True,
        lease_id=o_lease_2.lease_id,
    )
    # Re-run the script: step a already has a result (passes instantly) -> reaches step b -> suspends, spawns worker-1.
    orch = orch_engine_2.run_one_step(orch, lease_id=o_lease_2.lease_id)
    assert orch.status == "suspended", (
        f"after restart, expected suspended (waiting for step b), got {orch.status}"
    )
    disp.release(o_lease_2.lease_id, next_state="suspended", wake_on=orch.wake_on)

    # --- Key assertion: after restart, total SubtaskSpawned is 2, not 3 (step a not re-spawned) ---
    spawned_after_restart = [
        e for e in log.read(orch_task_id) if e.type == "SubtaskSpawned"
    ]
    assert len(spawned_after_restart) == 2, (
        f"after restart, expected total 2 SubtaskSpawned (no re-dispatch of step a), "
        f"got {len(spawned_after_restart)}"
    )
    # The first spawn's subtask_id matches the pre-restart one (no newly produced duplicate spawn).
    assert spawned_after_restart[0].payload.subtask_id == first_worker_call_id, (
        "first SubtaskSpawned changed after restart — positional cursor broken"
    )

    # --- Phase 3: run the second worker (step b); the orchestration task completes ---
    child_id_1 = _run_single_worker(
        log=log, cs=cs, disp=disp, answer="result-b"
    )

    # Second resume (orch_engine_2 still alive this time, no "restart" needed).
    orch = _resume_orch(
        log=log,
        cs=cs,
        disp=disp,
        orch_engine=orch_engine_2,
        orch_task_id=orch_task_id,
        child_id=child_id_1,
        call_index=1,
    )
    assert orch.status == "terminal", (
        f"expected terminal after step b, got {orch.status}"
    )

    # Final answer is correct.
    completed_events = [
        e for e in log.read(orch_task_id) if e.type == "TaskCompleted"
    ]
    assert completed_events, "orchestration task never completed"
    assert completed_events[-1].payload.answer == "result-a|result-b", (
        f"expected 'result-a|result-b', got '{completed_events[-1].payload.answer}'"
    )

    # Full event trail: spawn x2 + suspend x2 + woken x2 + TaskCompleted.
    event_types = [e.type for e in log.read(orch_task_id)]
    for expected in ("TaskCreated", "SubtaskSpawned", "TaskSuspended", "TaskWoken", "TaskCompleted"):
        assert expected in event_types, f"missing {expected} in {event_types}"
