"""Runtime generic content-channel pieces.

Covers the four additive generic pieces:

* ``ContextContentRecorded`` — the generic content-fingerprint event
  carrying ``kind`` + drift ``policy`` (``pinned`` / ``evolving``); both
  policy recording shapes round-trip through the Sqlite payload restorer
  and fold into the generic activation map.
* ``TaskState.active_content`` — the kind→names activation map; the old
  ``SkillContentRecorded`` event, the ``activate_skills`` patch sugar,
  and the new event all fold into the same map (three routes, one map).
  Old recordings stay byte-equal (legacy fields untouched, snapshot
  bodies of the pre-generic era rehydrate to the same state a
  from-scratch fold produces).
* ``ContentHashesFn`` — the (kind, name) → (version, hash) resolver seam;
  post the issue-07 generation switch an Engine wired with the generic
  seam emits the generic ``ContextContentRecorded`` (kind="skill",
  policy="pinned"); the retained legacy ``skill_hashes`` seam (verify
  replay of pre-cutover recordings) still emits the old shape.
* The generic read path contains no skill literal branches.
"""

from __future__ import annotations

import inspect
from typing import Optional

from noeta.core import fold as fold_mod
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.snapshot import (
    deserialize_task_state,
    rehydrate_task,
    serialize_task_state,
    snapshot_media_type,
)
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.decisions import FinishDecision, TaskStatePatch
from noeta.protocols.events import (
    ContextContentRecordedPayload,
    SkillContentRecordedPayload,
    TaskCreatedPayload,
    TaskSnapshotPayload,
    TaskStatePatchedPayload,
)
from noeta.protocols.task import Task, TaskState
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.storage.sqlite.eventlog import SqliteEventLog
from noeta.testing.composer import trivial_three_segment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_log_cs() -> tuple[InMemoryEventLog, InMemoryContentStore]:
    return InMemoryEventLog(), InMemoryContentStore()


def _seed_genesis(log: InMemoryEventLog, task_id: str = "t1") -> None:
    log.emit(
        task_id=task_id,
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )


def _content_event(
    *,
    kind: str,
    name: str,
    version: str = "1",
    content_hash: str = "h",
    policy: str = "evolving",
) -> ContextContentRecordedPayload:
    return ContextContentRecordedPayload(
        kind=kind,
        name=name,
        version=version,
        content_hash=content_hash,
        policy=policy,
    )


# ---------------------------------------------------------------------------
# 1) Recording shape — both drift policies round-trip the Sqlite restorer
# ---------------------------------------------------------------------------


def test_pinned_recording_shape_roundtrips_sqlite() -> None:
    log = SqliteEventLog(":memory:")
    _seed_genesis(log)
    log.emit(
        task_id="t1",
        type="ContextContentRecorded",
        payload=_content_event(
            kind="persona",
            name="navigator",
            version="2",
            content_hash="sha-p",
            policy="pinned",
        ),
    )
    env = log.read("t1")[-1]
    assert isinstance(env.payload, ContextContentRecordedPayload)
    assert env.payload.kind == "persona"
    assert env.payload.name == "navigator"
    assert env.payload.version == "2"
    assert env.payload.content_hash == "sha-p"
    assert env.payload.policy == "pinned"
    log.close()


def test_evolving_recording_shape_roundtrips_sqlite() -> None:
    log = SqliteEventLog(":memory:")
    _seed_genesis(log)
    log.emit(
        task_id="t1",
        type="ContextContentRecorded",
        payload=_content_event(
            kind="memory",
            name="index",
            version="3",
            content_hash="sha-m",
            policy="evolving",
        ),
    )
    env = log.read("t1")[-1]
    assert isinstance(env.payload, ContextContentRecordedPayload)
    assert env.payload.kind == "memory"
    assert env.payload.policy == "evolving"
    log.close()


# ---------------------------------------------------------------------------
# 2) New event folds into the generic activation map (both policies)
# ---------------------------------------------------------------------------


def test_fold_context_content_recorded_into_active_content() -> None:
    log, cs = _make_log_cs()
    _seed_genesis(log)
    log.emit(
        task_id="t1",
        type="ContextContentRecorded",
        payload=_content_event(kind="memory", name="index", policy="evolving"),
    )
    log.emit(
        task_id="t1",
        type="ContextContentRecorded",
        payload=_content_event(kind="persona", name="navigator", policy="pinned"),
    )
    task = fold(log, cs, "t1")
    assert task.state.active_content == {
        "memory": ("index",),
        "persona": ("navigator",),
    }
    # The legacy sugar list is untouched by the generic event.
    assert task.state.active_skills == []


def test_fold_context_content_recorded_dedupes_same_kind_name() -> None:
    log, cs = _make_log_cs()
    _seed_genesis(log)
    for _ in range(2):
        log.emit(
            task_id="t1",
            type="ContextContentRecorded",
            payload=_content_event(kind="memory", name="index"),
        )
    task = fold(log, cs, "t1")
    assert task.state.active_content == {"memory": ("index",)}


def test_fold_context_content_recorded_ignores_blank_fields() -> None:
    log, cs = _make_log_cs()
    _seed_genesis(log)
    log.emit(
        task_id="t1",
        type="ContextContentRecorded",
        payload=_content_event(kind="", name="x"),
    )
    log.emit(
        task_id="t1",
        type="ContextContentRecorded",
        payload=_content_event(kind="memory", name=""),
    )
    task = fold(log, cs, "t1")
    assert task.state.active_content == {}


# ---------------------------------------------------------------------------
# 3) Old event + sugar field fold into the same map; legacy fields keep
#    their old behaviour (old recordings replay untouched)
# ---------------------------------------------------------------------------


def test_old_skill_event_and_sugar_fold_into_same_map() -> None:
    log, cs = _make_log_cs()
    _seed_genesis(log)
    log.emit(
        task_id="t1",
        type="SkillContentRecorded",
        payload=SkillContentRecordedPayload(
            skill_name="alpha", version="1", content_hash="h_alpha"
        ),
    )
    log.emit(
        task_id="t1",
        type="TaskStatePatched",
        payload=TaskStatePatchedPayload(
            patch=TaskStatePatch(activate_skills=["alpha"]).to_dict()
        ),
    )
    task = fold(log, cs, "t1")
    assert task.state.active_skills == ["alpha"]
    assert task.state.active_content == {"skill": ("alpha",)}
    # Legacy provenance fold unchanged.
    assert task.governance.skill_content_hashes == {"alpha": "h_alpha"}
    assert task.governance.skill_content_versions == {"alpha": "1"}


def test_deactivate_sugar_keeps_map_in_lockstep() -> None:
    state = TaskState()
    TaskStatePatch(activate_skills=["a", "b"]).apply(state)
    assert state.active_skills == ["a", "b"]
    assert state.active_content == {"skill": ("a", "b")}
    TaskStatePatch(deactivate_skills=["a"]).apply(state)
    assert state.active_skills == ["b"]
    assert state.active_content == {"skill": ("b",)}
    TaskStatePatch(deactivate_skills=["b"]).apply(state)
    assert state.active_skills == []
    assert state.active_content == {}


def test_non_skill_patch_leaves_generic_map_alone() -> None:
    state = TaskState(active_content={"memory": ("index",)})
    TaskStatePatch(set_goal="g2").apply(state)
    assert state.active_content == {"memory": ("index",)}


def test_three_routes_converge_into_one_map() -> None:
    log, cs = _make_log_cs()
    _seed_genesis(log)
    # Route 1: old skill provenance event.
    log.emit(
        task_id="t1",
        type="SkillContentRecorded",
        payload=SkillContentRecordedPayload(
            skill_name="alpha", version="1", content_hash="h"
        ),
    )
    # Route 2: the activate_skills patch sugar.
    log.emit(
        task_id="t1",
        type="TaskStatePatched",
        payload=TaskStatePatchedPayload(
            patch=TaskStatePatch(activate_skills=["alpha"]).to_dict()
        ),
    )
    # Route 3: the generic event — one entry under the same kind (post-07
    # shape, no special-casing needed) and one under a brand-new kind.
    log.emit(
        task_id="t1",
        type="ContextContentRecorded",
        payload=_content_event(kind="skill", name="alpha", policy="pinned"),
    )
    log.emit(
        task_id="t1",
        type="ContextContentRecorded",
        payload=_content_event(kind="memory", name="index", policy="evolving"),
    )
    task = fold(log, cs, "t1")
    assert task.state.active_content == {
        "skill": ("alpha",),
        "memory": ("index",),
    }
    assert task.state.active_skills == ["alpha"]


# ---------------------------------------------------------------------------
# 4) ContentHashesFn seam — skill resolves as one kind of the generic seam
# ---------------------------------------------------------------------------


def _make_engine(
    log: InMemoryEventLog,
    cs: InMemoryContentStore,
    **seams: object,
) -> Engine:
    return Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=StubScriptedPolicy([FinishDecision(answer="done")]),
        **seams,
    )


def _make_runtime() -> tuple[InMemoryEventLog, InMemoryContentStore, InMemoryDispatcher]:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)
    return log, InMemoryContentStore(), disp


def _lease(disp: InMemoryDispatcher, task_id: str) -> str:
    disp.enqueue(task_id)
    lease = disp.lease(worker_id="w")
    assert lease is not None
    return lease.lease_id


def test_content_hashes_seam_resolves_skill_kind() -> None:
    """Post issue-07 generation switch: the generic seam emits the GENERIC
    event (kind="skill", policy="pinned") — see tests/test_generation_cutover.py
    for the full write-side contract."""
    log, cs, disp = _make_runtime()

    def content_hashes(kind: str, name: str) -> Optional[tuple[str, str]]:
        return ("9", f"ch_{kind}_{name}")

    engine = _make_engine(log, cs, content_hashes=content_hashes)
    task = engine.create_task(goal="g", policy_name="scripted")
    lease_id = _lease(disp, task.task_id)

    engine.apply_state_patch(
        task,
        patch=TaskStatePatch(activate_skills=["alpha"]),
        lease_id=lease_id,
    )

    events = list(log.read(task.task_id))
    skill_events = [e for e in events if e.type == "ContextContentRecorded"]
    assert len(skill_events) == 1
    assert skill_events[0].payload.kind == "skill"
    assert skill_events[0].payload.name == "alpha"
    assert skill_events[0].payload.version == "9"
    assert skill_events[0].payload.content_hash == "ch_skill_alpha"
    assert skill_events[0].payload.policy == "pinned"
    assert not [e for e in events if e.type == "SkillContentRecorded"]
    # Provenance precedes the durable patch (old causal order preserved).
    types = [e.type for e in events]
    assert types.index("ContextContentRecorded") < types.index("TaskStatePatched")


def test_legacy_skill_seam_takes_precedence_over_generic() -> None:
    """The retained legacy seam (old-recording replay) wins when both are
    wired — the old event type re-emits byte-equal."""
    log, cs, disp = _make_runtime()

    def content_hashes(kind: str, name: str) -> Optional[tuple[str, str]]:
        return ("9", "generic")

    def skill_hashes(name: str) -> Optional[tuple[str, str]]:
        return ("1", "specific")

    engine = _make_engine(
        log, cs, skill_hashes=skill_hashes, content_hashes=content_hashes
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    lease_id = _lease(disp, task.task_id)
    engine.apply_state_patch(
        task,
        patch=TaskStatePatch(activate_skills=["alpha"]),
        lease_id=lease_id,
    )
    skill_events = [
        e for e in log.read(task.task_id) if e.type == "SkillContentRecorded"
    ]
    assert len(skill_events) == 1
    assert skill_events[0].payload.content_hash == "specific"
    assert not [
        e for e in log.read(task.task_id) if e.type == "ContextContentRecorded"
    ]


def test_generic_seam_unknown_skill_skips_emission() -> None:
    log, cs, disp = _make_runtime()

    def content_hashes(kind: str, name: str) -> Optional[tuple[str, str]]:
        return None

    engine = _make_engine(log, cs, content_hashes=content_hashes)
    task = engine.create_task(goal="g", policy_name="scripted")
    lease_id = _lease(disp, task.task_id)
    engine.apply_state_patch(
        task,
        patch=TaskStatePatch(activate_skills=["alpha"]),
        lease_id=lease_id,
    )
    types = [e.type for e in log.read(task.task_id)]
    assert "SkillContentRecorded" not in types
    assert "ContextContentRecorded" not in types
    assert task.state.active_skills == ["alpha"]


# ---------------------------------------------------------------------------
# 5) Snapshot round-trips — typed shape normalises, pre-generic bodies
#    rehydrate to the same state a from-scratch fold produces
# ---------------------------------------------------------------------------


def test_snapshot_roundtrip_normalises_name_tuples() -> None:
    task = Task(task_id="t1")
    task.state.active_content = {"memory": ("index",)}
    body = serialize_task_state(task)
    rebuilt = rehydrate_task(deserialize_task_state(body))
    assert rebuilt.state.active_content == {"memory": ("index",)}
    assert isinstance(rebuilt.state.active_content["memory"], tuple)


def test_pre_generic_snapshot_body_seeds_skill_entry() -> None:
    # A snapshot written before the generic map existed: it has the sugar
    # list but no ``active_content`` key at all.
    task = Task(task_id="t1")
    task.state.active_skills = ["alpha"]
    state_dict = task.state_dict()
    state_dict["state"].pop("active_content")
    rebuilt = rehydrate_task(state_dict)
    assert rebuilt.state.active_skills == ["alpha"]
    assert rebuilt.state.active_content == {"skill": ("alpha",)}


def test_accelerated_fold_matches_from_scratch_over_old_snapshot() -> None:
    log, cs = _make_log_cs()
    _seed_genesis(log)
    log.emit(
        task_id="t1",
        type="SkillContentRecorded",
        payload=SkillContentRecordedPayload(
            skill_name="alpha", version="1", content_hash="h_a"
        ),
    )
    log.emit(
        task_id="t1",
        type="TaskStatePatched",
        payload=TaskStatePatchedPayload(
            patch=TaskStatePatch(activate_skills=["alpha"]).to_dict()
        ),
    )
    # Mid-stream snapshot whose body pre-dates the generic map (no
    # ``active_content`` key — exactly what old code serialized).
    prefix = fold(log, cs, "t1", ignore_snapshots=True)
    old_body = prefix.state_dict()
    old_body["state"].pop("active_content")
    ref = cs.put(to_canonical_bytes(old_body), media_type=snapshot_media_type())
    log.emit(
        task_id="t1",
        type="TaskSnapshot",
        payload=TaskSnapshotPayload(state_ref=ref),
    )
    # More activation after the snapshot.
    log.emit(
        task_id="t1",
        type="SkillContentRecorded",
        payload=SkillContentRecordedPayload(
            skill_name="beta", version="1", content_hash="h_b"
        ),
    )
    log.emit(
        task_id="t1",
        type="TaskStatePatched",
        payload=TaskStatePatchedPayload(
            patch=TaskStatePatch(activate_skills=["beta"]).to_dict()
        ),
    )

    accelerated = fold(log, cs, "t1")
    from_scratch = fold(log, cs, "t1", ignore_snapshots=True)
    assert accelerated.state.active_content == {"skill": ("alpha", "beta")}
    assert to_canonical_bytes(accelerated.state_dict()) == to_canonical_bytes(
        from_scratch.state_dict()
    )


# ---------------------------------------------------------------------------
# 6) The generic read path carries no skill literal branches
# ---------------------------------------------------------------------------


def test_generic_read_path_has_no_skill_literals() -> None:
    handler_src = inspect.getsource(fold_mod._on_context_content_recorded)
    assert "skill" not in handler_src.lower()
    post_init_src = inspect.getsource(TaskState.__post_init__)
    assert "skill" not in post_init_src.lower()
