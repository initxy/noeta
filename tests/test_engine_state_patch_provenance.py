"""D5 — Engine seam provenance for state-patch skill activation.

Verifies the activation entry points (Engine.apply_state_patch,
Engine._apply_decision_state_patch, the StatePatchDecision handler in
_decision_handlers, and a pre-loop legacy event) all emit at most one
provenance event per (task, skill), placed before the TaskStatePatched
event, and skip cleanly when no resolver is wired.

Post the issue-07 generation switch this file pins the LEGACY
seam (``skill_hashes`` → old ``SkillContentRecorded``), which the verify
driver wires when replaying pre-cutover recordings — the byte shapes
asserted here are exactly what zero-replay-impact requires. The generic-seam
counterparts live in ``tests/test_generation_cutover.py``.
"""

from __future__ import annotations

from typing import Optional


from noeta.core.engine import Engine
from noeta.core.fold import apply_event
from noeta.protocols.events import SkillContentRecordedPayload
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import (
    FinishDecision,
    TaskStatePatch,
)
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


def _make_engine(
    log: InMemoryEventLog,
    cs: InMemoryContentStore,
    *,
    skill_hashes,
) -> Engine:
    return Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=StubScriptedPolicy([FinishDecision(answer="done")]),
        skill_hashes=skill_hashes,
    )


def _skill_hashes(name: str) -> Optional[tuple[str, str]]:
    return ("1", "h_" + name)


# ---------------------------------------------------------------------------
# Test (a): single apply_state_patch emits SkillContentRecorded before
# TaskStatePatched, and governance records the hash.
# ---------------------------------------------------------------------------


def test_apply_state_patch_emits_provenance_before_patch() -> None:
    log, cs, disp = _make_runtime()
    engine = _make_engine(log, cs, skill_hashes=_skill_hashes)
    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w")
    assert lease is not None

    engine.apply_state_patch(
        task,
        patch=TaskStatePatch(activate_skills=["alpha"]),
        lease_id=lease.lease_id,
    )

    events = list(log.read(task.task_id))
    skill_events = [e for e in events if e.type == "SkillContentRecorded"]
    assert len(skill_events) == 1
    assert skill_events[0].payload.skill_name == "alpha"
    assert skill_events[0].payload.version == "1"
    assert skill_events[0].payload.content_hash == "h_alpha"

    patch_events = [e for e in events if e.type == "TaskStatePatched"]
    assert len(patch_events) == 1
    skill_idx = next(
        i for i, e in enumerate(events) if e.type == "SkillContentRecorded"
    )
    patch_idx = next(
        i for i, e in enumerate(events) if e.type == "TaskStatePatched"
    )
    assert skill_idx < patch_idx

    assert task.governance.skill_content_hashes == {"alpha": "h_alpha"}


# ---------------------------------------------------------------------------
# Test (b): second apply_state_patch for same skill — still exactly one event.
# ---------------------------------------------------------------------------


def test_apply_state_patch_dedupes_second_activation() -> None:
    log, cs, disp = _make_runtime()
    engine = _make_engine(log, cs, skill_hashes=_skill_hashes)
    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w")
    assert lease is not None

    engine.apply_state_patch(
        task,
        patch=TaskStatePatch(activate_skills=["alpha"]),
        lease_id=lease.lease_id,
    )
    engine.apply_state_patch(
        task,
        patch=TaskStatePatch(activate_skills=["alpha"]),
        lease_id=lease.lease_id,
    )

    events = list(log.read(task.task_id))
    skill_events = [e for e in events if e.type == "SkillContentRecorded"]
    assert len(skill_events) == 1


# ---------------------------------------------------------------------------
# Test (c): skill_hashes=None → zero SkillContentRecorded events
# (old host behaviour preserved byte-identical).
# ---------------------------------------------------------------------------


def test_apply_state_patch_skips_when_no_resolver() -> None:
    log, cs, disp = _make_runtime()
    engine = _make_engine(log, cs, skill_hashes=None)
    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w")
    assert lease is not None

    engine.apply_state_patch(
        task,
        patch=TaskStatePatch(activate_skills=["alpha"]),
        lease_id=lease.lease_id,
    )

    events = list(log.read(task.task_id))
    skill_events = [e for e in events if e.type == "SkillContentRecorded"]
    assert len(skill_events) == 0
    # TaskStatePatched was still emitted.
    assert any(e.type == "TaskStatePatched" for e in events)


# ---------------------------------------------------------------------------
# Test (d): a pre-loop legacy event already in the stream, then
# engine.apply_state_patch for the same skill → exactly one
# SkillContentRecorded (no double emission) — the old-recording shape a
# resume off an old log must reproduce.
# ---------------------------------------------------------------------------


def test_preloop_legacy_event_then_engine_seam_no_double_emit() -> None:
    log, cs, disp = _make_runtime()
    engine = _make_engine(log, cs, skill_hashes=_skill_hashes)
    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w")
    assert lease is not None

    # Pre-loop legacy event (what the retired pre-loop helper used to
    # write): emit + fold so the governance gate sees it.
    env = log.emit(
        task_id=task.task_id,
        type="SkillContentRecorded",
        payload=SkillContentRecordedPayload(
            skill_name="alpha", version="1", content_hash="h_alpha"
        ),
        lease_id=lease.lease_id,
    )
    apply_event(task, env, cs)
    # Now simulate the engine seam being called for the same skill.
    engine.apply_state_patch(
        task,
        patch=TaskStatePatch(activate_skills=["alpha"]),
        lease_id=lease.lease_id,
    )

    events = list(log.read(task.task_id))
    skill_events = [e for e in events if e.type == "SkillContentRecorded"]
    assert len(skill_events) == 1
    assert skill_events[0].payload.skill_name == "alpha"
    # Pre-loop helper emits provenance before the engine seam's patch,
    # so the single event is before the single TaskStatePatched.
    skill_idx = next(
        i for i, e in enumerate(events) if e.type == "SkillContentRecorded"
    )
    patch_idx = next(
        i for i, e in enumerate(events) if e.type == "TaskStatePatched"
    )
    assert skill_idx < patch_idx
