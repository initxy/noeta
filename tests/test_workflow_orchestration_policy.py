"""OrchestrationPolicy spine, direct (unit level).

Without the ``run_workflow`` control tool or main agent, drive one
spawn→worker→resume→finish directly with :class:`OrchestrationPolicy` as the
parent policy, verifying:

* First ``decide``: the script's ``agent()`` spawns **exactly one** real subtask
  (``SpawnSubtaskDecision``, deterministic call_id ``wf-0``); the parent
  suspends on ``SubtaskCompleted``.
* After the worker finishes and its result folds back into the parent stream
  (paired ``tool_result`` call_id=wf-0), the second ``decide`` re-runs the
  script from the top; that ``agent()`` returns instantly from the recording,
  the script ``return``s → ``FinishDecision`` carrying the worker's output.
* The subtask has its own EventLog stream; the parent stream records the full
  spawn→suspend→woken→finish.

This is the minimal slice pinning the "orchestration Policy + script host +
re-run from the top + result fold-back" quartet on a single-worker sequential
model (end-to-end drain/control-tool coverage is in test_workflow_run_tool_e2e.py).
"""

from __future__ import annotations

from typing import Any

from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.policies.orchestration import OrchestrationPolicy, WORKFLOW_CALL_PREFIX
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision
from noeta.protocols.messages import ToolUseBlock
from noeta.protocols.wake import SubtaskCompleted
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment


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


def test_single_agent_workflow_spine() -> None:
    log, cs, disp = _make_runtime()

    script = 'return agent("review the diff", agent="child_agent")\n'
    orch_engine = _engine_for(
        log=log,
        cs=cs,
        policy=OrchestrationPolicy(script=script, args={}),
    )
    worker_engine = _engine_for(
        log=log,
        cs=cs,
        policy=StubScriptedPolicy([FinishDecision(answer="looks good")]),
    )

    orch = orch_engine.create_task(goal="workflow", policy_name="scripted")
    disp.enqueue(orch.task_id)

    # Step 1: run the orchestration task → script's first agent() suspends it
    # on a single SubtaskCompleted (one real child spawned, call_id wf-0).
    o_lease_1 = disp.lease(worker_id="w1")
    assert o_lease_1 is not None and o_lease_1.task_id == orch.task_id
    orch = orch_engine.run_one_step(orch, lease_id=o_lease_1.lease_id)
    assert orch.status == "suspended"
    assert isinstance(orch.wake_on, SubtaskCompleted)
    # The synthetic spawn turn carries a deterministic wf-0 spawn tool_use.
    spawn_uses = [
        b
        for m in orch.runtime.messages
        if m.role == "assistant"
        for b in m.content
        if isinstance(b, ToolUseBlock)
    ]
    assert len(spawn_uses) == 1
    assert spawn_uses[0].call_id == f"{WORKFLOW_CALL_PREFIX}0"
    assert spawn_uses[0].arguments == {"agent": "child_agent", "goal": "review the diff"}
    disp.release(o_lease_1.lease_id, next_state="suspended", wake_on=orch.wake_on)

    # Step 2: worker leases the spawned child (its own independent stream).
    c_lease = disp.lease(worker_id="w1")
    assert c_lease is not None
    child_id = c_lease.task_id
    assert child_id != orch.task_id
    child = fold(log, cs, child_id)
    child = worker_engine.run_one_step(child, lease_id=c_lease.lease_id)
    assert child.status == "terminal"
    disp.release(c_lease.lease_id, next_state="terminal")
    # Child stream is independent: it has its own TaskCreated, no parent events.
    child_types = [e.type for e in log.read(child_id)]
    assert "TaskCreated" in child_types
    assert "SubtaskSpawned" not in child_types

    # Step 3: parent (orchestration) is re-queued by the child-completion
    # observer. Re-lease, fold, woken, render the child result as a paired
    # tool_result (mirrors subtask_drain._resume_parent), then re-step.
    o_lease_2 = disp.lease(worker_id="w1")
    assert o_lease_2 is not None and o_lease_2.task_id == orch.task_id
    orch = fold(log, cs, orch.task_id)
    result = orch.governance.subtask_results[-1]
    assert result.status == "completed"
    orch_engine.note_woken(
        orch,
        lease_id=o_lease_2.lease_id,
        wake_event=SubtaskCompleted(subtask_id=child_id),
    )
    orch = orch_engine.append_subagent_result_message(
        orch,
        call_id=f"{WORKFLOW_CALL_PREFIX}0",
        output=result.output,
        success=result.status == "completed",
        lease_id=o_lease_2.lease_id,
    )
    # Step 4: re-running the script now sees the recorded agent() result and
    # the workflow finishes with that result as its answer.
    orch = orch_engine.run_one_step(orch, lease_id=o_lease_2.lease_id)
    assert orch.status == "terminal"
    disp.release(o_lease_2.lease_id, next_state="terminal")

    # Parent stream records the full life: spawn → suspend → woken → finish.
    parent_types = [e.type for e in log.read(orch.task_id)]
    for expected in (
        "TaskCreated",
        "SubtaskSpawned",
        "TaskSuspended",
        "TaskWoken",
        "TaskCompleted",
    ):
        assert expected in parent_types, f"missing {expected} in {parent_types}"

    completed = [e for e in log.read(orch.task_id) if e.type == "TaskCompleted"]
    assert completed and completed[-1].payload.answer == "looks good"


def _view(blocks: Any = ()) -> Any:
    """A minimal stand-in View — ``OrchestrationPolicy.decide`` only reads
    ``view.rolling_history`` (and ignores ``ctx``)."""
    from types import SimpleNamespace

    from noeta.protocols.messages import Message

    history = [Message(role="tool", content=list(blocks))] if blocks else []
    return SimpleNamespace(rolling_history=history)


def _wf_result(index: int, *, output: Any, success: bool, error: Any = None) -> Any:
    from noeta.protocols.messages import ToolResultBlock

    return ToolResultBlock(
        call_id=f"{WORKFLOW_CALL_PREFIX}{index}",
        output=output,
        success=success,
        error=error,
    )


def test_failed_worker_halts_workflow_loudly() -> None:
    """A helper that terminated in FAILURE makes the workflow FAIL (surfacing
    the child's reason) instead of silently yielding ''."""
    from noeta.protocols.decisions import FailDecision

    view = _view([_wf_result(0, output="", success=False, error="llm_truncated")])
    decision = OrchestrationPolicy(
        script='return agent("scan", agent="explore")\n', args={}
    ).decide(None, view)  # type: ignore[arg-type]
    assert isinstance(decision, FailDecision)
    assert decision.retryable is False
    assert "workflow halted" in decision.reason
    assert "explore" in decision.reason and "llm_truncated" in decision.reason


def test_successful_worker_result_still_passes_through() -> None:
    view = _view([_wf_result(0, output="42 files", success=True)])
    decision = OrchestrationPolicy(
        script='return agent("count")\n', args={}
    ).decide(None, view)  # type: ignore[arg-type]
    assert isinstance(decision, FinishDecision)
    assert decision.answer == "42 files"


def test_script_may_tolerate_a_failed_worker() -> None:
    """``agent()`` raises a normal ``Exception`` on failure, so a script can
    ``try/except`` to tolerate it — the workflow then continues."""
    view = _view([_wf_result(0, output="", success=False, error="boom")])
    script = (
        "try:\n"
        "    r = agent('x', agent='explore')\n"
        "except Exception:\n"
        "    r = 'fallback'\n"
        "return r\n"
    )
    decision = OrchestrationPolicy(script=script, args={}).decide(None, view)  # type: ignore[arg-type]
    assert isinstance(decision, FinishDecision)
    assert decision.answer == "fallback"


def test_try_except_never_swallows_the_spawn_suspend() -> None:
    """The spawn-suspend is a ``BaseException``, so a script's ``except
    Exception`` (meant for failed helpers) can NEVER swallow it — otherwise the
    worker would never be spawned and the workflow would run on a phantom value."""
    from noeta.protocols.decisions import SpawnSubtaskDecision

    view = _view([])  # no recorded result yet → first agent() must SUSPEND
    script = (
        "try:\n"
        "    r = agent('x', agent='explore')\n"
        "except Exception:\n"
        "    r = 'fallback'\n"
        "return r\n"
    )
    decision = OrchestrationPolicy(script=script, args={}).decide(None, view)  # type: ignore[arg-type]
    assert isinstance(decision, SpawnSubtaskDecision)


def test_multiline_triple_quoted_string_literal_survives_the_wrap() -> None:
    """``_run_script`` wraps the script in a synthetic function so a
    top-level ``return`` is legal. An earlier ``textwrap.indent``-based
    string wrap prepended 4 spaces to every physical line of the script,
    which silently corrupted the interior lines of any multi-line (e.g.
    triple-quoted) string literal — the added indentation became part of
    the string's value. The AST-splice wrap must reproduce the literal
    byte-for-byte instead."""
    from noeta.protocols.decisions import SpawnSubtaskDecision

    script = (
        'goal = """first line\n'
        'second line\n'
        'third line"""\n'
        "return agent(goal)\n"
    )
    view = _view([])  # no recorded result yet -> the agent() call suspends
    decision = OrchestrationPolicy(script=script, args={}).decide(None, view)  # type: ignore[arg-type]

    assert isinstance(decision, SpawnSubtaskDecision)
    assert decision.goal == "first line\nsecond line\nthird line"


def test_multiline_string_literal_nested_in_a_block_survives_the_wrap() -> None:
    """Same corruption risk, but with the literal's own source indentation
    nested inside an ``if`` block — pins that the fix is not merely
    "strip a fixed 4-space prefix" but a real AST splice that never touches
    the literal's interior lines regardless of surrounding structure."""
    from noeta.protocols.decisions import SpawnSubtaskDecision

    script = (
        "if True:\n"
        '    goal = """alpha\n'
        "beta\n"
        'gamma"""\n'
        "    result = agent(goal)\n"
        "return result\n"
    )
    view = _view([])
    decision = OrchestrationPolicy(script=script, args={}).decide(None, view)  # type: ignore[arg-type]

    assert isinstance(decision, SpawnSubtaskDecision)
    assert decision.goal == "alpha\nbeta\ngamma"


def test_no_agent_calls_finishes_immediately() -> None:
    """Script never calls ``agent()`` → spawns nothing, finishes directly (the spine's degenerate case)."""
    log, cs, disp = _make_runtime()
    orch_engine = _engine_for(
        log=log,
        cs=cs,
        policy=OrchestrationPolicy(script='return args["x"] + 1\n', args={"x": 41}),
    )
    orch = orch_engine.create_task(goal="workflow", policy_name="scripted")
    disp.enqueue(orch.task_id)
    lease = disp.lease(worker_id="w1")
    assert lease is not None
    orch = orch_engine.run_one_step(orch, lease_id=lease.lease_id)
    assert orch.status == "terminal"
    types = [e.type for e in log.read(orch.task_id)]
    assert "SubtaskSpawned" not in types
    completed = [e for e in log.read(orch.task_id) if e.type == "TaskCompleted"]
    assert completed and completed[-1].payload.answer == 42
