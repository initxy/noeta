"""Content-provenance recording: what a task durably says about the content it
was given.

A content hash recorded *after* the activation it describes proves nothing, so
the ordering — record first, then patch — is the whole point of these tests,
together with first-only emission (a re-activation must not append a second
record) and the drift ``policy`` each kind carries (``pinned`` for a skill body,
``evolving`` for the workspace environment).

Two paths reach the same guarantee: the Engine's optional ``skill_hashes`` hook
emits ``SkillContentRecorded`` folded into ``GovernanceState``
last-write-wins tables, and a live SDK host session emits the generic
``ContextContentRecorded``.
"""

from __future__ import annotations

from pathlib import Path

from noeta.builtins.skills.impl import load_workspace_skills
from noeta.builtins.skills.impl import skill_content_hash
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import (
    FinishDecision,
    TaskStatePatch,
)
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec


# ---------------------------------------------------------------------------
# Kernel recording helpers
# ---------------------------------------------------------------------------


def _make_runtime() -> tuple[InMemoryEventLog, InMemoryContentStore, InMemoryDispatcher]:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)
    return (log, InMemoryContentStore(), disp)


def _record_skill_activation(
    *, with_provenance: bool, emit_twice: bool = False
) -> tuple[str, InMemoryEventLog, InMemoryContentStore]:
    """Record a skill activation through the Engine's ``skill_hashes`` seam:
    one ``SkillContentRecorded`` per (task, skill), right before the activation
    patch."""
    log, cs, disp = _make_runtime()
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=StubScriptedPolicy([FinishDecision(answer="done")]),
        skill_hashes=(
            (lambda name: ("3", "sha-s1")) if with_provenance else None
        ),
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w-rec")
    assert lease is not None
    for _ in range(2 if emit_twice else 1):
        engine.apply_state_patch(
            task,
            patch=TaskStatePatch(activate_skills=["s1"]),
            lease_id=lease.lease_id,
        )
    engine.run_one_step(task, lease_id=lease.lease_id)
    return task.task_id, log, cs


# ---------------------------------------------------------------------------
# Emission grammar
# ---------------------------------------------------------------------------


def test_skill_content_recorded_first_only_before_patch() -> None:
    task_id, log, _cs = _record_skill_activation(
        with_provenance=True, emit_twice=True
    )
    events = list(log.read(task_id))
    skill_events = [e for e in events if e.type == "SkillContentRecorded"]
    # Emitted twice → still exactly one durable event (fold-backed dedupe).
    assert [
        (e.payload.skill_name, e.payload.version, e.payload.content_hash)
        for e in skill_events
    ] == [("s1", "3", "sha-s1")]
    rec_idx = next(i for i, e in enumerate(events) if e.type == "SkillContentRecorded")
    patch_idx = next(i for i, e in enumerate(events) if e.type == "TaskStatePatched")
    assert rec_idx < patch_idx


# ---------------------------------------------------------------------------
# Fold
# ---------------------------------------------------------------------------


def test_fold_governance_tables() -> None:
    skill_task_id, skill_log, skill_cs = _record_skill_activation(
        with_provenance=True
    )
    skill_task = fold(skill_log, skill_cs, skill_task_id, ignore_snapshots=True)
    assert skill_task.governance.skill_content_hashes == {"s1": "sha-s1"}
    assert skill_task.governance.skill_content_versions == {"s1": "3"}


def test_fold_defaults_empty_without_events() -> None:
    task_id, log, cs = _record_skill_activation(with_provenance=False)
    task = fold(log, cs, task_id, ignore_snapshots=True)
    assert task.governance.tool_schema_hashes == {}
    assert task.governance.tool_schema_versions == {}
    assert task.governance.skill_content_hashes == {}
    assert task.governance.skill_content_versions == {}


# ---------------------------------------------------------------------------
# End-to-end through a live SDK host session
# ---------------------------------------------------------------------------


_SKILL_MD = """\
---
name: fix-python-test
description: minimal-patch loop for a failing pytest
priority: 50
---
1. Run pytest, read the failure, patch minimally, rerun.
"""


def _code_responses() -> list[LLMResponse]:
    return [
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="rt-1",
                    tool_name="edit",
                    arguments={"file_path": "x.py", "old_string": "foo", "new_string": "bar"},
                ),
                ToolUseBlock(
                    call_id="rt-2",
                    tool_name="edit",
                    arguments={"file_path": "x.py", "old_string": "bar", "new_string": "baz"},
                ),
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "resp-1"},
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="done")],
            usage=Usage(uncached=1, output=1),
            raw={"id": "resp-2"},
        ),
    ]


def _code_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "x.py").write_text("foo\n")
    skill_dir = workspace / ".noeta" / "skills" / "fix-python-test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    return workspace


def test_code_session_records_content_provenance(
    tmp_path: Path,
) -> None:
    workspace = _code_workspace(tmp_path)
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=workspace,
        provider=FakeLLMProvider(responses=_code_responses()),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        require_approval_tools=(),
    )
    # ``activations`` are the workspace skills the driver activates pre-loop,
    # before the first turn is composed.
    out = make_driver(host).start(
        goal="rename foo", agent="main", activations=("fix-python-test",)
    )
    assert out.status == "terminal"
    events = list(host.event_log.read(out.task_id))
    # A host session records provenance through the generic
    # ContextContentRecorded only — the kind-specific events stay unused.
    assert not [e for e in events if e.type == "ToolSchemaRecorded"]
    assert not [e for e in events if e.type == "SkillContentRecorded"]
    content_events = [e for e in events if e.type == "ContextContentRecorded"]
    # The always-on workspace-environment resident activates pre-loop through
    # the workspace pack's init hook, so it records BEFORE the post-goal skill
    # activation. Record order is not placement order: both are pre-loop
    # residents and their semi_stable placement is band-ordered
    # (skill < environment) either way.
    assert [
        (e.payload.kind, e.payload.name, e.payload.policy)
        for e in content_events
    ] == [
        ("environment", "workspace", "evolving"),
        ("skill", "fix-python-test", "pinned"),
    ]
    registry = load_workspace_skills(workspace)
    desc = registry.get("fix-python-test")
    assert desc is not None
    expected_hash = skill_content_hash(desc)
    skill_event = next(e for e in content_events if e.payload.kind == "skill")
    assert skill_event.payload.content_hash == expected_hash
    # The skill's provenance record lands before its activation patch: a hash
    # recorded after the fact cannot pin what the turn actually saw.
    skill_idx = next(
        i
        for i, e in enumerate(events)
        if e.type == "ContextContentRecorded" and e.payload.kind == "skill"
    )
    patch_idx = next(
        i for i, e in enumerate(events) if e.type == "TaskStatePatched"
    )
    assert skill_idx < patch_idx
