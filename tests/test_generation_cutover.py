"""Generation cutover + legacy-recording retention.

The single generation switch of the context-supply batch:

* **New recordings use the generic shape.** A host wired with only the
  generic ``content_hashes`` seam emits ``ContextContentRecorded``
  (kind="skill", policy="pinned") for every skill activation — pre-loop
  (``activate_skills``) and mid-loop (state-patch seams) alike. The old
  ``SkillContentRecorded`` never appears in a new recording.
* **Old recordings fold untouched (retention check).** A legacy-generation
  recording — produced by an engine wired through the retained
  ``skill_hashes`` seam, exactly the pre-cutover host shape — still
  re-emits the OLD event type in the same byte shape, so a resume off such
  a log reconstructs the same state.
* **New recordings fold too**: the generic
  seam rebuilds the same activation map from recorded
  ``ContextContentRecorded`` events.
* **The composer reads the generic activation map only** — the
  ``active_skills`` sugar bridge died with this switch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from tests._skill_fixtures import write_skill

from noeta.core.engine import Engine, emit_context_content_recorded
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, TaskStatePatch
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment
from noeta.testing.fake_llm import FakeLLMProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_engine(log, cs, **seams) -> Engine:
    return Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=StubScriptedPolicy([FinishDecision(answer="done")]),
        **seams,
    )


def _generic_hashes(kind: str, name: str) -> Optional[tuple[str, str]]:
    return ("7", f"gh_{kind}_{name}")


# ---------------------------------------------------------------------------
# 1) Write side — generic seam now emits the GENERIC event (the switch)
# ---------------------------------------------------------------------------


def test_generic_seam_patch_emits_context_content_recorded() -> None:
    """Cutover claim: an engine wired with only the generic seam emits
    ContextContentRecorded (kind=skill, policy=pinned) on skill activation,
    never the old SkillContentRecorded."""
    log, cs, disp = _make_runtime()
    engine = _make_engine(log, cs, content_hashes=_generic_hashes)
    task = engine.create_task(goal="g", policy_name="scripted")
    lease_id = _lease(disp, task.task_id)

    engine.apply_state_patch(
        task,
        patch=TaskStatePatch(activate_skills=["alpha"]),
        lease_id=lease_id,
    )

    events = list(log.read(task.task_id))
    types = [e.type for e in events]
    assert "SkillContentRecorded" not in types
    generic = [e for e in events if e.type == "ContextContentRecorded"]
    assert len(generic) == 1
    p = generic[0].payload
    assert p.kind == "skill"
    assert p.name == "alpha"
    assert p.version == "7"
    assert p.content_hash == "gh_skill_alpha"
    assert p.policy == "pinned"
    # Causal order preserved: provenance before the durable patch.
    assert types.index("ContextContentRecorded") < types.index("TaskStatePatched")
    # Folds into the generic activation map on the in-memory task.
    assert task.state.active_content.get("skill") == ("alpha",)


def test_generic_seam_dedupes_second_activation() -> None:
    log, cs, disp = _make_runtime()
    engine = _make_engine(log, cs, content_hashes=_generic_hashes)
    task = engine.create_task(goal="g", policy_name="scripted")
    lease_id = _lease(disp, task.task_id)

    for _ in range(2):
        engine.apply_state_patch(
            task,
            patch=TaskStatePatch(activate_skills=["alpha"]),
            lease_id=lease_id,
        )

    events = list(log.read(task.task_id))
    assert sum(1 for e in events if e.type == "ContextContentRecorded") == 1


def test_generic_seam_unknown_name_skips_emission() -> None:
    log, cs, disp = _make_runtime()
    engine = _make_engine(log, cs, content_hashes=lambda kind, name: None)
    task = engine.create_task(goal="g", policy_name="scripted")
    lease_id = _lease(disp, task.task_id)
    engine.apply_state_patch(
        task,
        patch=TaskStatePatch(activate_skills=["alpha"]),
        lease_id=lease_id,
    )
    types = [e.type for e in log.read(task.task_id)]
    assert "ContextContentRecorded" not in types
    assert "SkillContentRecorded" not in types
    assert task.state.active_skills == ["alpha"]


def test_legacy_seam_still_emits_old_event() -> None:
    """skill_hashes is retained as a legacy-recording-fold-only seam: an
    engine wired with it still emits the old event in the same byte shape
    (so a resume off an old recording reconstructs the same state)."""
    log, cs, disp = _make_runtime()
    engine = _make_engine(
        log, cs, skill_hashes=lambda name: ("1", "h_" + name)
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    lease_id = _lease(disp, task.task_id)
    engine.apply_state_patch(
        task,
        patch=TaskStatePatch(activate_skills=["alpha"]),
        lease_id=lease_id,
    )
    events = list(log.read(task.task_id))
    old = [e for e in events if e.type == "SkillContentRecorded"]
    assert len(old) == 1
    assert old[0].payload.skill_name == "alpha"
    assert old[0].payload.content_hash == "h_alpha"
    assert not any(e.type == "ContextContentRecorded" for e in events)


def test_legacy_seam_takes_precedence_over_generic() -> None:
    """When both seams are wired, the old seam wins — transition-era
    recordings carry both historical seams, and old-event recordings
    must re-emit in the old shape."""
    log, cs, disp = _make_runtime()
    engine = _make_engine(
        log,
        cs,
        skill_hashes=lambda name: ("1", "specific"),
        content_hashes=_generic_hashes,
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    lease_id = _lease(disp, task.task_id)
    engine.apply_state_patch(
        task,
        patch=TaskStatePatch(activate_skills=["alpha"]),
        lease_id=lease_id,
    )
    events = list(log.read(task.task_id))
    assert [e.type for e in events if e.type == "ContextContentRecorded"] == []
    old = [e for e in events if e.type == "SkillContentRecorded"]
    assert len(old) == 1
    assert old[0].payload.content_hash == "specific"


# ---------------------------------------------------------------------------
# 2) The generic pre-loop helper — kind-neutral, first-only
# ---------------------------------------------------------------------------


def test_emit_context_content_recorded_is_kind_neutral_and_first_only() -> None:
    log, cs, disp = _make_runtime()
    engine = _make_engine(log, cs)
    task = engine.create_task(goal="g", policy_name="scripted")
    lease_id = _lease(disp, task.task_id)

    for _ in range(2):
        emit_context_content_recorded(
            engine,
            task,
            kind="persona",
            name="navigator",
            version="2",
            content_hash="sha-p",
            policy="evolving",
            lease_id=lease_id,
        )
    events = [
        e for e in log.read(task.task_id) if e.type == "ContextContentRecorded"
    ]
    assert len(events) == 1
    assert events[0].payload.kind == "persona"
    assert events[0].payload.policy == "evolving"
    assert task.state.active_content.get("persona") == ("navigator",)


def test_emit_context_content_recorded_skips_blank() -> None:
    log, cs, disp = _make_runtime()
    engine = _make_engine(log, cs)
    task = engine.create_task(goal="g", policy_name="scripted")
    lease_id = _lease(disp, task.task_id)
    emit_context_content_recorded(
        engine, task, kind="skill", name="", version="1",
        content_hash="h", policy="pinned", lease_id=lease_id,
    )
    emit_context_content_recorded(
        engine, task, kind="skill", name="a", version="1",
        content_hash="", policy="pinned", lease_id=lease_id,
    )
    assert [
        e for e in log.read(task.task_id) if e.type == "ContextContentRecorded"
    ] == []


# ---------------------------------------------------------------------------
# 3) SDK pre-loop activate_skills — new recordings use the generic shape
# ---------------------------------------------------------------------------


def test_activate_skills_emits_generic_event(tmp_path: Path) -> None:
    from noeta.execution.skills import activate_skills, load_workspace_skills

    ws = tmp_path / "ws"
    ws.mkdir()
    write_skill(ws, "alpha", description="a")
    registry = load_workspace_skills(ws)

    log, cs, disp = _make_runtime()
    engine = _make_engine(log, cs)
    task = engine.create_task(goal="g", policy_name="scripted")
    lease_id = _lease(disp, task.task_id)

    activate_skills(
        engine, task, skills=["alpha"], lease_id=lease_id,
        skill_registry=registry,
    )

    events = list(log.read(task.task_id))
    types = [e.type for e in events]
    assert "SkillContentRecorded" not in types
    generic = [e for e in events if e.type == "ContextContentRecorded"]
    assert len(generic) == 1
    p = generic[0].payload
    assert (p.kind, p.name, p.policy) == ("skill", "alpha", "pinned")
    assert p.version == "1"
    assert len(p.content_hash) == 64
    # Provenance precedes the durable patch; engine seam does not re-emit.
    assert types.index("ContextContentRecorded") < types.index("TaskStatePatched")


def test_activate_skills_then_engine_seam_no_double_emit(tmp_path: Path) -> None:
    """The four activation entry points converge to exactly one generic event per (task, kind, name)."""
    from noeta.execution.skills import (
        activate_skills,
        build_skill_hashes,
        load_workspace_skills,
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    write_skill(ws, "alpha", description="a")
    registry = load_workspace_skills(ws)
    lookup = build_skill_hashes(registry)
    assert lookup is not None

    log, cs, disp = _make_runtime()
    engine = _make_engine(
        log, cs, content_hashes=lambda kind, name: lookup(name)
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    lease_id = _lease(disp, task.task_id)

    activate_skills(
        engine, task, skills=["alpha"], lease_id=lease_id,
        skill_registry=registry,
    )
    engine.apply_state_patch(
        task,
        patch=TaskStatePatch(activate_skills=["alpha"]),
        lease_id=lease_id,
    )
    events = list(log.read(task.task_id))
    assert sum(1 for e in events if e.type == "ContextContentRecorded") == 1


# ---------------------------------------------------------------------------
# 4) Composer — the active_skills sugar bridge is gone
# ---------------------------------------------------------------------------


def test_composer_reads_generic_map_only(tmp_path: Path) -> None:
    from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
    from noeta.guards.budget import Budget
    from noeta.protocols.task import Task, TaskState

    ws = tmp_path / "ws"
    ws.mkdir()
    write_skill(ws, "alpha", description="a")
    cs = InMemoryContentStore()
    inputs = build_session_inputs(
        workspace_dir=ws,
        system_prompt="p",
        allowed_tools=frozenset({"read_file"}),
        content_store=cs,
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
    )

    def _semi_text(task: Task) -> str:
        view = inputs.composer.compose(task)
        semi = next(s for s in view.segments if s.name == "semi_stable")
        return "\n".join(
            b.text
            for m in semi.content
            for b in m.content
            if isinstance(b, TextBlock)
        )

    # Generic map → renders.
    task_map = Task(
        task_id="t1", state=TaskState(active_content={"skill": ("alpha",)})
    )
    assert "Body of the alpha skill." in _semi_text(task_map)

    # Sugar list alone (no map) → no longer bridged after the cutover.
    task_sugar = Task(task_id="t2", state=TaskState(active_skills=["alpha"]))
    assert "Body of the alpha skill." not in _semi_text(task_sugar)


# ---------------------------------------------------------------------------
# 5) Retention check — old recordings (pre-rebaseline shape) replay untouched;
#    new recordings are strict-green too
# ---------------------------------------------------------------------------


def _skill_call(skill_name: str, call_id: str = "sk") -> LLMResponse:
    from noeta.policies._control_translate import SKILL_TOOL

    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=call_id,
                tool_name=SKILL_TOOL,
                arguments={"skill": skill_name},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _run_to_terminal(engine, disp, task) -> None:
    for _ in range(30):
        lease = disp.lease(worker_id="w")
        if lease is None:
            return
        task = engine.run_one_step(task, lease_id=lease.lease_id)
        if task.status in ("completed", "failed", "suspended"):
            return


def _record_session(
    tmp_path: Path,
    *,
    generation: str,
) -> tuple:
    """Record one skill-invocation session in the requested generation.

    ``generation="legacy"`` wires the engine exactly like a pre-cutover
    host (the retained ``skill_hashes`` seam + pre-loop legacy event via
    the seam-driven engine path) so the resulting stream is byte-shaped
    like a real pre-rebaseline old recording. ``generation="generic"``
    wires the post-cutover host shape (``content_hashes``).
    """
    from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
    from noeta.execution.skills import build_skill_hashes
    from noeta.guards.budget import Budget
    from noeta.runtime.llm import RuntimeLLMClient
    from noeta.runtime.tool import ToolRuntime

    ws = tmp_path / f"ws-{generation}"
    ws.mkdir()
    write_skill(ws, "alpha", description="a")

    cs = InMemoryContentStore()
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)

    def _inputs():
        return build_session_inputs(
            workspace_dir=ws,
            system_prompt="you are helpful",
            allowed_tools=frozenset({"read_file"}),
            content_store=cs,
            model="stub-model",
            compaction=COMPACTION_OFF,
            budget=Budget(),
            skill_invocation_enabled=True,
        )

    responses = [_skill_call("alpha"), _end("done")]
    inputs = _inputs()
    client = RuntimeLLMClient(
        provider=FakeLLMProvider(responses=list(responses)),
        event_log=log,
        content_store=cs,
    )
    if generation == "legacy":
        seams = {"skill_hashes": build_skill_hashes(inputs.skill_registry)}
    else:
        seams = {"content_hashes": inputs.content_hashes}
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=inputs.composer,
        policy=inputs.policy_factory(client),
        tools=inputs.tools,
        tool_runtime=ToolRuntime(event_log=log, content_store=cs),
        hooks=inputs.hooks,
        **seams,
    )
    task = engine.create_task(goal="invoke a skill", policy_name="react")
    disp.enqueue(task.task_id)
    _run_to_terminal(engine, disp, task)
    return task.task_id, log, cs, _inputs, responses


def test_legacy_recording_emits_old_event_shape(tmp_path: Path) -> None:
    """A legacy-generation recording (pre-cutover host shape) emits the old
    ``SkillContentRecorded`` event, never the generic ``ContextContentRecorded``."""
    tid, log, _cs, _make_inputs, _responses = _record_session(
        tmp_path, generation="legacy"
    )
    types = [e.type for e in log.read(tid)]
    assert "SkillContentRecorded" in types, "sample must be the old-generation shape"
    assert "ContextContentRecorded" not in types


def test_generic_recording_emits_generic_event_shape(tmp_path: Path) -> None:
    """A new-generation recording emits ``ContextContentRecorded`` and never
    the old ``SkillContentRecorded``."""
    tid, log, _cs, _make_inputs, _responses = _record_session(
        tmp_path, generation="generic"
    )
    types = [e.type for e in log.read(tid)]
    assert "ContextContentRecorded" in types
    assert "SkillContentRecorded" not in types, "new recordings must not emit the old event"


def test_legacy_and_generic_fold_to_same_activation_state(tmp_path: Path) -> None:
    """Both generations of recording fold to the same activation state (active_skills + active_content)."""
    tid_old, log_old, cs_old, _i1, _r1 = _record_session(
        tmp_path, generation="legacy"
    )
    tid_new, log_new, cs_new, _i2, _r2 = _record_session(
        tmp_path, generation="generic"
    )
    old = fold(log_old, cs_old, tid_old)
    new = fold(log_new, cs_new, tid_new)
    assert old.state.active_skills == new.state.active_skills == ["alpha"]
    assert (
        old.state.active_content
        == new.state.active_content
        == {"skill": ("alpha",)}
    )
