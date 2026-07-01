"""Content-provenance recording side.

Covers the recording side of the per-task first-only content-hash events that
survived the verify/replay removal:

* **Skill content** — the retained legacy ``skill_hashes`` seam still emits a
  first-only ``SkillContentRecorded`` per (task, skill) right before the
  ``TaskStatePatched(activate_skills=…)``; it folds into ``GovernanceState``
  last-write-wins tables.
* **Code-product E2E** — a live SDK host session records the
  generic ``ContextContentRecorded`` (kind="skill", policy="pinned") before the
  activation patch. The verify-era ``ToolSchemaRecorded`` emission was removed
  with the verify/replay test infrastructure, so a new recording emits none.
"""

from __future__ import annotations

from pathlib import Path

from noeta.execution.skills import load_workspace_skills, skill_content_hash
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
from noeta.tools.fs import FsWriteMode, ShellMode

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
    """Record a skill activation through the retained legacy ``skill_hashes``
    seam: the old ``SkillContentRecorded`` event once per (task, skill) right
    before the activation patch."""
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
# Code-product E2E (PATH C + activate_skills wiring)
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
                    arguments={"path": "x.py", "old": "foo", "new": "bar"},
                ),
                ToolUseBlock(
                    call_id="rt-2",
                    tool_name="edit",
                    arguments={"path": "x.py", "old": "bar", "new": "baz"},
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
    # ``extra_skills=("fix-python-test",)`` → the driver's pre-loop ``activations``
    # (the same workspace-skill activation the runner did at prepare()).
    out = make_driver(host).start(
        goal="rename foo", agent="main", activations=("fix-python-test",)
    )
    assert out.status == "terminal"
    events = list(host.event_log.read(out.task_id))
    # The verify-era ToolSchemaRecorded / SkillContentRecorded are no longer
    # emitted in a new recording — only the generic ContextContentRecorded.
    assert not [e for e in events if e.type == "ToolSchemaRecorded"]
    assert not [e for e in events if e.type == "SkillContentRecorded"]
    skill_events = [e for e in events if e.type == "ContextContentRecorded"]
    # One pinned skill record before activation; the always-on
    # workspace-environment resident records its own evolving entry after.
    assert [
        (e.payload.kind, e.payload.name, e.payload.policy)
        for e in skill_events
    ] == [
        ("skill", "fix-python-test", "pinned"),
        ("environment", "workspace", "evolving"),
    ]
    registry = load_workspace_skills(workspace)
    desc = registry.get("fix-python-test")
    assert desc is not None
    expected_hash = skill_content_hash(desc)
    assert skill_events[0].payload.content_hash == expected_hash
    skill_idx = next(
        i for i, e in enumerate(events) if e.type == "ContextContentRecorded"
    )
    patch_idx = next(
        i for i, e in enumerate(events) if e.type == "TaskStatePatched"
    )
    assert skill_idx < patch_idx
