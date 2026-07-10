"""Issue 14: Engine emits ContextPlanComposed and fold writes plan_ref.

PRD §C / Grill round 2 #8: ContextPlanComposed is emitted in front of
the LLM round-trip — for non-LLM Stub policies it lands right after
``TaskStarted`` and before any decision-derived event (it's the very
first thing the Engine does inside ``run_one_step`` after the
bootstrap). fold then writes ``task.context.plan_ref`` from the last
``ContextPlanComposed`` (single writer per grill #4).
"""

from __future__ import annotations

from noeta.context.composer import ThreeSegmentComposer
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.policies.stub import StubFinishPolicy
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


def _bootstrap(answer: str = "hello") -> tuple[
    Engine, InMemoryEventLog, InMemoryContentStore, InMemoryDispatcher, str, str
]:
    content_store = InMemoryContentStore()
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    composer = ThreeSegmentComposer(
        system_prompt="be helpful",
        tools={},
        content_store=content_store,
    )
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=StubFinishPolicy(answer=answer),
    )
    task = engine.create_task(goal="say hello", policy_name="stub_finish")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w-test")
    assert lease is not None
    engine.run_one_step(task, lease_id=lease.lease_id)
    return engine, event_log, content_store, dispatcher, task.task_id, lease.lease_id


def test_context_plan_composed_appears_before_first_decision_event() -> None:
    _engine, event_log, _cs, _disp, task_id, _lease = _bootstrap()

    types = [e.type for e in event_log.read(task_id)]
    assert "ContextPlanComposed" in types
    plan_idx = types.index("ContextPlanComposed")
    # No MessagesAppended / ToolCallStarted / TaskCompleted may appear
    # before the ContextPlanComposed event of the same step.
    for forbidden in ("MessagesAppended", "ToolCallStarted", "TaskCompleted"):
        if forbidden in types:
            assert types.index(forbidden) > plan_idx, (
                f"{forbidden} preceded ContextPlanComposed: {types}"
            )


def test_context_plan_composed_payload_carries_plan_ref() -> None:
    _engine, event_log, _cs, _disp, task_id, _lease = _bootstrap()

    plan_events = [
        e for e in event_log.read(task_id) if e.type == "ContextPlanComposed"
    ]
    assert len(plan_events) >= 1
    payload = plan_events[0].payload
    assert payload.plan_ref is not None
    assert payload.plan_ref.media_type == "application/json"
    assert len(payload.plan_ref.hash) == 64


def test_fold_writes_context_state_plan_ref_from_last_event() -> None:
    _engine, event_log, content_store, _disp, task_id, _lease = _bootstrap()

    rebuilt = fold(event_log, content_store, task_id)

    plan_events = [
        e for e in event_log.read(task_id) if e.type == "ContextPlanComposed"
    ]
    assert plan_events, "expected at least one ContextPlanComposed event"
    last_ref = plan_events[-1].payload.plan_ref
    assert rebuilt.context.plan_ref == last_ref
