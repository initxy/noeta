"""Phase 4 I3 — Noeta-Code skills + context wiring.

Three regressions wired into one file:

* :func:`load_workspace_skills` indexes ``<workspace>/.noeta/skills`` (with an
  optional override) using the unchanged Phase-1 ``SkillIndexer``.
* :func:`build_coding_composer` wires the registry's renderer so
  activated skill bodies enter the ``semi_stable`` segment and
  ``ContextPlan.selected_skills`` records them.
* :func:`activate_skills` emits a **durable** ``TaskStatePatched`` event
  through ``Engine.apply_state_patch`` (B11 + B17). The event survives
  fold/replay, so verify reproduces the same active set without the
  model needing to emit ``activate_skills``.

Plus the boundary the architect underlined (B12): Phase 4 records ONLY
``ContextPlan.selected_skills``; ``selected_messages`` / ``dropped_messages``
stay empty in this wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._skill_fixtures import write_skill_raw

from noeta.execution.skills import (
    DEFAULT_SKILLS_SUBDIR,
    activate_skills,
    build_skill_composer as build_coding_composer,
    load_workspace_skills,
)
from noeta.context.composer import ThreeSegmentComposer
from noeta.context.skills import SkillRegistry
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.context_plan import ContextPlan
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.errors import InvalidLease
from noeta.protocols.events import TaskStatePatchedPayload
from noeta.protocols.messages import TextBlock
from noeta.protocols.task import Task, TaskState
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


_SKILL_FIX_PYTHON_TEST = """\
---
name: fix-python-test
description: minimal-patch loop for a failing pytest
priority: 50
---
1. Run pytest to surface the failing test.
2. Read the failing file, locate the bug.
3. Apply the smallest possible edit patch.
4. Rerun pytest to confirm.
"""

_SKILL_PROJECT_NOTES = """\
---
name: project-notes
description: workspace conventions and quirks
priority: 200
---
- Tests live under tests/.
- Use edit for one-line fixes.
"""


# ---------------------------------------------------------------------------
# load_workspace_skills
# ---------------------------------------------------------------------------


def test_load_workspace_skills_uses_default_subdir(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    skills_dir = workspace / DEFAULT_SKILLS_SUBDIR
    skills_dir.mkdir(parents=True)
    write_skill_raw(skills_dir, "fix-python-test", _SKILL_FIX_PYTHON_TEST)

    registry = load_workspace_skills(workspace)
    assert "fix-python-test" in registry.names()
    desc = registry.get("fix-python-test")
    assert desc is not None
    assert desc.description == "minimal-patch loop for a failing pytest"
    assert desc.priority == 50


def test_load_workspace_skills_override_dir_wins(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # Default dir would be EMPTY; override points at a real pack.
    override = tmp_path / "alt_skills"
    write_skill_raw(override, "project-notes", _SKILL_PROJECT_NOTES)
    registry = load_workspace_skills(workspace, override_skills_dir=override)
    assert registry.names() == ("project-notes",)


def test_load_workspace_skills_missing_dir_returns_empty_registry(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    registry = load_workspace_skills(workspace)
    assert registry.names() == ()


# ---------------------------------------------------------------------------
# build_coding_composer + ContextPlan.selected_skills
# ---------------------------------------------------------------------------


def _make_composer(
    tmp_path: Path, body: str = _SKILL_FIX_PYTHON_TEST
) -> tuple[InMemoryContentStore, SkillRegistry, ThreeSegmentComposer]:
    skills_dir = tmp_path / "skills_pack"
    write_skill_raw(skills_dir, "fix-python-test", body)
    registry = load_workspace_skills(
        tmp_path / "ws", override_skills_dir=skills_dir
    )
    cs = InMemoryContentStore()
    composer = build_coding_composer(
        system_prompt="coding-Agent role + tools + safety",
        tools={},
        content_store=cs,
        skill_registry=registry,
    )
    return cs, registry, composer


def _trivial_task() -> Task:
    """Return a Task with empty active_skills + empty runtime.messages."""
    return Task(task_id="t1", status="running", state=TaskState(goal="g"))


def _read_plan(cs: InMemoryContentStore, view: object) -> dict[str, object]:
    """Restore the ``ContextPlan`` body the composer wrote to the store."""
    import json

    plan_ref = view.plan_ref  # type: ignore[attr-defined]
    assert plan_ref is not None
    plan: dict[str, object] = json.loads(cs.get(plan_ref).decode("utf-8"))
    return plan


def test_compose_before_activation_yields_empty_skill_segment(
    tmp_path: Path,
) -> None:
    cs, _, composer = _make_composer(tmp_path)
    task = _trivial_task()
    view = composer.compose(task)
    plan = _read_plan(cs, view)
    assert plan["selected_skills"] == []
    semi_stable = next(s for s in view.segments if s.name == "semi_stable")
    assert semi_stable.content == []


def test_compose_after_activation_materialises_skill_body(tmp_path: Path) -> None:
    cs, _, composer = _make_composer(tmp_path)
    task = _trivial_task()
    TaskStatePatch(activate_skills=["fix-python-test"]).apply(task.state)

    view = composer.compose(task)
    semi_stable = next(s for s in view.segments if s.name == "semi_stable")
    assert len(semi_stable.content) == 1
    first_block = semi_stable.content[0].content[0]
    assert isinstance(first_block, TextBlock)
    rendered = first_block.text
    assert "Activated skill: fix-python-test" in rendered
    assert "Run pytest" in rendered  # from the skill body
    assert "Apply the smallest possible edit patch" in rendered

    plan = _read_plan(cs, view)
    assert plan["selected_skills"] == ["fix-python-test"]
    # Phase 4 boundary (B12): no message-selection provenance fields.
    assert plan["selected_messages"] == []
    assert plan["dropped_messages"] == []


def test_compose_drops_unknown_active_name(tmp_path: Path) -> None:
    cs, _, composer = _make_composer(tmp_path)
    task = _trivial_task()
    TaskStatePatch(
        activate_skills=["fix-python-test", "does-not-exist"]
    ).apply(task.state)
    view = composer.compose(task)
    plan = _read_plan(cs, view)
    # Unknown name is dropped at the renderer level; only the indexed
    # skill enters `selected_skills`.
    assert plan["selected_skills"] == ["fix-python-test"]


# ---------------------------------------------------------------------------
# Durable TaskStatePatched event (B11 + B17)
# ---------------------------------------------------------------------------


def _engine_with_leased_task(
    skills_dir: Path,
) -> tuple[Engine, Task, str, InMemoryEventLog, InMemoryContentStore]:
    """Build a coding-session-shaped Engine + lease a task ready for activation."""
    workspace = skills_dir.parent / "ws"
    workspace.mkdir(exist_ok=True)
    registry = load_workspace_skills(
        workspace, override_skills_dir=skills_dir
    )
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    cs = InMemoryContentStore()
    composer = build_coding_composer(
        system_prompt="coding-Agent role",
        tools={},
        content_store=cs,
        skill_registry=registry,
    )
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=composer,
    )
    task = engine.create_task(goal="fix the failing test", policy_name="react")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="rec-worker")
    assert lease is not None
    return engine, task, lease.lease_id, log, cs


def test_activate_skills_emits_durable_task_state_patched_event(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    write_skill_raw(skills_dir, "fix-python-test", _SKILL_FIX_PYTHON_TEST)
    engine, task, lease_id, log, _ = _engine_with_leased_task(skills_dir)

    activate_skills(
        engine, task, skills=["fix-python-test"], lease_id=lease_id
    )

    events = log.read(task.task_id)
    patched = [e for e in events if e.type == "TaskStatePatched"]
    assert len(patched) == 1
    assert isinstance(patched[0].payload, TaskStatePatchedPayload)
    assert patched[0].payload.patch["activate_skills"] == ["fix-python-test"]
    # The in-memory Task is also updated (Engine remains single writer).
    assert task.state.active_skills == ["fix-python-test"]


def test_activation_survives_fold_replay(tmp_path: Path) -> None:
    """The hard B17 proof: the active set materialises out of the
    EventLog alone, so a fresh fold (verify/replay's path) recovers it
    without any in-memory carry-over."""
    skills_dir = tmp_path / "skills"
    write_skill_raw(skills_dir, "fix-python-test", _SKILL_FIX_PYTHON_TEST)
    engine, task, lease_id, log, _ = _engine_with_leased_task(skills_dir)
    activate_skills(
        engine, task, skills=["fix-python-test"], lease_id=lease_id
    )

    # `fold` reads from the EventLog directly — passing the same `log`
    # used by the Engine + the task id yields the rebuilt state from the
    # recorded events alone (the in-memory `task` object is irrelevant).
    rebuilt = fold(log, _content_store_for_fold(), task.task_id)
    assert rebuilt.state.active_skills == ["fix-python-test"]


def _content_store_for_fold() -> InMemoryContentStore:
    """A throwaway store; fold only consults it if a snapshot is present
    (no snapshot was emitted here)."""
    return InMemoryContentStore()


def test_activate_skills_empty_list_is_no_op(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    engine, task, lease_id, log, _ = _engine_with_leased_task(skills_dir)
    before = list(log.read(task.task_id))

    activate_skills(engine, task, skills=[], lease_id=lease_id)

    after = log.read(task.task_id)
    assert len(after) == len(before)
    assert task.state.active_skills == []


def test_activation_order_is_idempotent(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    write_skill_raw(skills_dir, "fix-python-test", _SKILL_FIX_PYTHON_TEST)
    write_skill_raw(skills_dir, "project-notes", _SKILL_PROJECT_NOTES)
    engine, task, lease_id, _, _ = _engine_with_leased_task(skills_dir)

    activate_skills(
        engine,
        task,
        skills=["fix-python-test", "project-notes", "fix-python-test"],
        lease_id=lease_id,
    )
    # Phase-1 patch semantics: dedup + order-preserved union.
    assert task.state.active_skills == ["fix-python-test", "project-notes"]


# ---------------------------------------------------------------------------
# Engine.apply_state_patch seam
# ---------------------------------------------------------------------------


def test_engine_apply_state_patch_emits_and_applies(tmp_path: Path) -> None:
    """The new Engine seam is independent of the runner — it can be
    used to apply any operator-side patch as long as the lease is valid."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    engine, task, lease_id, log, _ = _engine_with_leased_task(skills_dir)
    patch = TaskStatePatch(set_phase="research", activate_skills=["s1"])

    engine.apply_state_patch(task, patch=patch, lease_id=lease_id)

    events = log.read(task.task_id)
    patched = [e for e in events if e.type == "TaskStatePatched"]
    assert len(patched) == 1
    assert patched[0].payload.patch["set_phase"] == "research"
    assert task.state.phase == "research"
    assert task.state.active_skills == ["s1"]


def test_engine_apply_state_patch_rejects_invalid_lease(tmp_path: Path) -> None:
    """``apply_state_patch`` must be a leased write. A stale /
    fabricated lease_id is rejected by the EventLog's `lease_validator`
    and ``task.state`` is NOT mutated. The check belongs at the
    operator seam too — not only on the Policy-driven path."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    engine, task, lease_id, log, _ = _engine_with_leased_task(skills_dir)
    fabricated = "lease-DOES-NOT-EXIST"
    assert fabricated != lease_id
    patch = TaskStatePatch(activate_skills=["s1"])

    with pytest.raises(InvalidLease):
        engine.apply_state_patch(task, patch=patch, lease_id=fabricated)

    # No TaskStatePatched landed in the log, and the in-memory task
    # state is unchanged (apply() runs AFTER emit, so a failed emit
    # short-circuits before mutating state).
    events = log.read(task.task_id)
    assert not any(e.type == "TaskStatePatched" for e in events)
    assert task.state.active_skills == []


def test_engine_apply_state_patch_records_canonical_bytes(tmp_path: Path) -> None:
    """The decision path and the operator path emit byte-equal payloads
    for the same logical patch — proof verify/replay treat both
    equivalently."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    engine, task, lease_id, log, _ = _engine_with_leased_task(skills_dir)
    patch = TaskStatePatch(activate_skills=["one"])
    engine.apply_state_patch(task, patch=patch, lease_id=lease_id)
    events = log.read(task.task_id)
    patched_payload = next(e for e in events if e.type == "TaskStatePatched").payload
    assert to_canonical_bytes(patched_payload) == to_canonical_bytes(
        TaskStatePatchedPayload(patch=patch.to_dict())
    )


# ---------------------------------------------------------------------------
# ContextPlan boundary (B12)
# ---------------------------------------------------------------------------


def test_context_plan_phase_4_records_only_selected_skills() -> None:
    """``ContextPlan`` keeps the same L0 shape — Phase 4 only sets the
    `selected_skills` field; `selected_messages` and `dropped_messages`
    remain empty."""
    plan = ContextPlan(
        composer_version="three_segment.v1",
        segment_hashes={"stable_prefix": "h", "semi_stable": "h", "dynamic_suffix": "h"},
        selected_skills=["fix-python-test"],
        selected_messages=[],
        dropped_messages=[],
    )
    assert plan.selected_skills == ["fix-python-test"]
    assert plan.selected_messages == []
    assert plan.dropped_messages == []
