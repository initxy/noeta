"""Engine handling of CompactionRequestedDecision (③ D-3, D-3b).

The unified compaction contract: a ``CompactionRequestedDecision`` is a
loop-continuing step handled inside ``Engine.run_one_step`` (NOT a
background worker). The handler:

1. anti-spiral check (D-B3): judged on **boundary progress** — if the
   boundary this summarizing step would write does not advance past
   ``task.context.summary_boundary`` (the durable, fold-written cumulative
   boundary), the step ESCALATES to a non-retryable ``TaskFailed`` rather than
   re-summarizing the same prefix forever. (This replaced the old sticky
   ``RuntimeState.last_transition`` tag check, which wrongly killed legitimate
   multi-step compactions.)
2. emits ``StepTransitionMarked`` (``overflow_recovery`` for passive /
   ``compaction_retry`` for proactive) so the next overflow can see it;
3. emits ``CompactionRequested`` (observability) + ``Compacted`` (durable
   result, fold writes the summary slice);
4. loop-continues so the next compose sees the compacted history.
"""

from __future__ import annotations

from typing import Any

from noeta.core.engine import Engine
from noeta.core.hooks import HookManager
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import CompactionRequestedDecision, FinishDecision
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment


def _run(decisions: list[Any]):
    dispatcher = InMemoryDispatcher()
    content_store = InMemoryContentStore()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    wire_default_observers(event_log, dispatcher)
    composer = trivial_three_segment(content_store)
    tool_runtime = ToolRuntime(event_log=event_log, content_store=content_store)
    policy = StubScriptedPolicy(decisions)
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=policy,
        tools={},
        tool_runtime=tool_runtime,
        hooks=HookManager(),
    )
    task = engine.create_task(goal="compaction", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None
    final = engine.run_one_step(task, lease_id=lease.lease_id)
    return engine, event_log, task, final


def test_passive_compaction_emits_events_and_continues() -> None:
    engine, log, task, final = _run(
        [
            CompactionRequestedDecision(
                reason="overflow",
                estimated_tokens=999,
                summary="condensed history",
                boundary_count=3,
            ),
            FinishDecision(answer="done"),
        ]
    )
    assert final.status == "terminal"
    types = [e.type for e in log.read(task.task_id)]
    assert "CompactionRequested" in types
    assert "Compacted" in types
    # passive trigger → overflow_recovery tag
    marks = [
        e.payload.reason
        for e in log.read(task.task_id)
        if e.type == "StepTransitionMarked"
    ]
    assert marks == ["overflow_recovery"]
    # the Compacted event carried the summary; fold wrote the slice
    assert final.context.summary_ref is not None
    assert final.context.summary_boundary == 3


def test_proactive_compaction_uses_compaction_retry_tag() -> None:
    engine, log, task, final = _run(
        [
            CompactionRequestedDecision(
                reason="proactive",
                estimated_tokens=900_000,
                summary="condensed",
                boundary_count=5,
            ),
            FinishDecision(answer="done"),
        ]
    )
    marks = [
        e.payload.reason
        for e in log.read(task.task_id)
        if e.type == "StepTransitionMarked"
    ]
    assert marks == ["compaction_retry"]


def test_prune_only_compaction_emits_no_compacted_event() -> None:
    """When prune alone brought the estimate under the window (no summary),
    the step still records the CompactionRequested anchor but emits NO
    Compacted event (nothing was summarized)."""
    engine, log, task, final = _run(
        [
            CompactionRequestedDecision(
                reason="overflow", estimated_tokens=999, summary=None
            ),
            FinishDecision(answer="done"),
        ]
    )
    assert final.status == "terminal"
    types = [e.type for e in log.read(task.task_id)]
    assert "CompactionRequested" in types
    assert "Compacted" not in types
    assert final.context.summary_ref is None


def test_anti_spiral_second_overflow_no_boundary_progress_escalates() -> None:
    """D-B3 (boundary-progress arm): a second overflow compaction whose
    boundary does NOT advance past ``summary_boundary`` (here 2 → 2, the first
    Compacted already collapsed the [:2] prefix) made no progress → escalate to
    a non-retryable TaskFailed instead of compacting forever."""
    engine, log, task, final = _run(
        [
            CompactionRequestedDecision(
                reason="overflow", summary="s1", boundary_count=2
            ),
            CompactionRequestedDecision(
                reason="overflow", summary="s2", boundary_count=2
            ),
            FinishDecision(answer="unreachable"),
        ]
    )
    assert final.status == "terminal"
    failed = [e for e in log.read(task.task_id) if e.type == "TaskFailed"]
    assert len(failed) == 1
    assert failed[0].payload.retryable is False
    assert "compaction" in failed[0].payload.reason


def test_anti_spiral_second_proactive_no_boundary_progress_escalates() -> None:
    """Finding 3 (boundary-progress arm): a repeated PROACTIVE compaction whose
    boundary fails to advance (2 → 2: the same prefix would be re-summarised)
    made no progress → escalate to a non-retryable TaskFailed. The escalation
    is judged on ``boundary_count <= task.context.summary_boundary`` (the
    durable, fold-written cumulative boundary), NOT on the sticky
    ``last_transition`` continuation tag — the tag wrongly killed legitimate
    multi-step compactions (see test_compaction_multistep_regression)."""
    engine, log, task, final = _run(
        [
            CompactionRequestedDecision(
                reason="proactive", summary="s1", boundary_count=2
            ),
            CompactionRequestedDecision(
                reason="proactive", summary="s2", boundary_count=2
            ),
            FinishDecision(answer="unreachable"),
        ]
    )
    assert final.status == "terminal"
    failed = [e for e in log.read(task.task_id) if e.type == "TaskFailed"]
    assert len(failed) == 1
    assert failed[0].payload.retryable is False
    assert "compaction" in failed[0].payload.reason
    # The second compaction must NOT have produced its own Compacted event —
    # the spiral check fires before the durable write.
    compacted = [e for e in log.read(task.task_id) if e.type == "Compacted"]
    assert len(compacted) == 1  # only the first proactive compaction landed


def test_two_proactive_compactions_with_advancing_boundary_do_not_escalate() -> None:
    """Fix A (kernel arm): two consecutive PROACTIVE compactions whose
    boundaries STRICTLY advance (2 → 5) are legitimate progress and must NOT
    escalate — even though the sticky ``compaction_retry`` continuation tag is
    present after the first one. This is the exact wrongful kill the old
    sticky-tag check produced; the boundary-progress arm reads
    ``summary_boundary`` (= 2 after
    the first Compacted) and lets the larger boundary (5 > 2) through."""
    engine, log, task, final = _run(
        [
            CompactionRequestedDecision(
                reason="proactive", summary="s1", boundary_count=2
            ),
            CompactionRequestedDecision(
                reason="proactive", summary="s2", boundary_count=5
            ),
            FinishDecision(answer="done"),
        ]
    )
    assert final.status == "terminal"
    assert not [e for e in log.read(task.task_id) if e.type == "TaskFailed"]
    completed = [e for e in log.read(task.task_id) if e.type == "TaskCompleted"]
    assert len(completed) == 1
    # BOTH compactions landed (each advanced the boundary).
    compacted = [e for e in log.read(task.task_id) if e.type == "Compacted"]
    assert [e.payload.boundary_count for e in compacted] == [2, 5]
    # The final folded boundary is the second (larger) one.
    assert final.context.summary_boundary == 5


def test_single_proactive_then_normal_turn_does_not_escalate() -> None:
    """A lone proactive compaction followed by a normal decision is the happy
    path — the anti-spiral arm must only fire on a *consecutive* proactive."""
    engine, log, task, final = _run(
        [
            CompactionRequestedDecision(
                reason="proactive", summary="s1", boundary_count=2
            ),
            FinishDecision(answer="done"),
        ]
    )
    assert final.status == "terminal"
    completed = [e for e in log.read(task.task_id) if e.type == "TaskCompleted"]
    assert len(completed) == 1
    assert not [e for e in log.read(task.task_id) if e.type == "TaskFailed"]
