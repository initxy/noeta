"""A public open-source skill must load and run byte-for-byte as published.

The fixtures are verbatim Apache-2.0 skills (see
``fixtures/oss_skills/PROVENANCE.md``), so the frontmatter is whatever their
authors wrote: hyphenated keys, unknown keys, inline list values. Anything the
parser does not understand has to survive as opaque metadata rather than
crash or be normalised, and metadata must not reach the rendered Message —
the ``semi_stable`` segment hash is the cross-host prompt-cache key, so a skill
that ships an extra frontmatter line would otherwise invalidate every cache.
Loading a skill also never executes what it bundles: a script is recorded as a
reachable path, and the whole load → activate → compose flow spawns no
subprocess.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from noeta.builtins.skills.impl import (
    build_skill_composer as build_coding_composer,
    load_workspace_skills,
)
from noeta.builtins.skills.impl import activate_skills
from noeta.builtins.skills.impl import SkillRegistry
from noeta.builtins.skills.impl.indexer import SkillDescription
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.protocols.messages import TextBlock
from noeta.protocols.task import Task, TaskState
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


OSS_SKILLS_DIR = Path(__file__).parent / "fixtures" / "oss_skills"


def _read_plan(cs: InMemoryContentStore, view: object) -> dict[str, object]:
    """Restore the ``ContextPlan`` the composer wrote, narrowing the
    ``ContentRef | None`` ref for ``mypy --strict``."""
    plan_ref = view.plan_ref  # type: ignore[attr-defined]
    assert plan_ref is not None
    plan = json.loads(cs.get(plan_ref).decode("utf-8"))
    assert isinstance(plan, dict)
    return plan


def _semi_stable_text(view: object) -> str:
    """Concatenate the rendered text of the ``semi_stable`` segment."""
    semi = next(s for s in view.segments if s.name == "semi_stable")  # type: ignore[attr-defined]
    return " ".join(
        b.text
        for m in semi.content
        for b in m.content
        if isinstance(b, TextBlock)
    )


# ---------------------------------------------------------------------------
# Real public skills load unchanged
# ---------------------------------------------------------------------------


def _registry() -> SkillRegistry:
    # The fixtures dir IS the skill pack root, and PROVENANCE.md + LICENSE sit
    # at its top level on purpose: they are outside every ``<name>/`` skill
    # root, which is what resource discovery must respect.
    return load_workspace_skills(
        OSS_SKILLS_DIR.parent, override_skills_dir=OSS_SKILLS_DIR
    )


def test_all_oss_fixtures_load() -> None:
    names = set(_registry().names())
    # two verbatim public skills + one authored fixture
    assert {"example-command", "session-report", "refactor-guide"} <= names


def test_example_command_frontmatter_metadata_captured() -> None:
    """Verbatim public skill: ``argument-hint`` (hyphen key) and
    ``allowed-tools: [Read, Glob, Grep, Bash]`` (inline list) are captured as
    opaque, raw-key-sorted metadata strings."""
    desc = _registry().get("example-command")
    assert desc is not None
    assert desc.metadata == (
        ("allowed-tools", "[Read, Glob, Grep, Bash]"),
        ("argument-hint", "<required-arg> [optional-arg]"),
    )
    # Captured as a string, NOT parsed into a YAML list: the loader must not
    # take a position on syntax it does not act on.
    assert dict(desc.metadata)["allowed-tools"] == "[Read, Glob, Grep, Bash]"


def test_refactor_guide_semantic_vs_metadata_split() -> None:
    """Semantic keys drive behavior; everything else is metadata."""
    desc = _registry().get("refactor-guide")
    assert desc is not None
    assert desc.version == "2"
    assert desc.priority == 50
    assert desc.metadata == (
        ("disable-model-invocation", "false"),
        ("license", "Apache-2.0"),
    )


# ---------------------------------------------------------------------------
# Resource discovery
# ---------------------------------------------------------------------------


def test_session_report_resources_discovered() -> None:
    """A skill bundling a script + a template records both as resources,
    sorted, with SKILL.md itself excluded."""
    desc = _registry().get("session-report")
    assert desc is not None
    assert desc.resources == ("analyze-sessions.mjs", "template.html")


def test_refactor_guide_nested_resource_is_posix_relative() -> None:
    desc = _registry().get("refactor-guide")
    assert desc is not None
    assert desc.resources == ("DEEPENING.md", "PATTERNS.md", "scripts/check.sh")


def test_single_file_skill_has_no_resources() -> None:
    desc = _registry().get("example-command")
    assert desc is not None
    assert desc.resources == ()


def test_provenance_and_license_never_leak_into_resources() -> None:
    """PROVENANCE.md + LICENSE live outside every skill root, so resource
    discovery (rooted at each ``<name>/``) must never list them."""
    reg = _registry()
    for name in ("example-command", "session-report", "refactor-guide"):
        desc = reg.get(name)
        assert desc is not None
        assert all("PROVENANCE" not in r for r in desc.resources)
        assert all("LICENSE" not in r for r in desc.resources)
        # no SKILL.md, no absolute paths, no parent escapes
        assert all(r != "SKILL.md" for r in desc.resources)
        assert all(not Path(r).is_absolute() for r in desc.resources)
        assert all(".." not in Path(r).parts for r in desc.resources)


# ---------------------------------------------------------------------------
# Determinism: metadata/resources do not perturb render bytes (prompt prefix)
# ---------------------------------------------------------------------------


def test_metadata_and_resources_do_not_change_render_bytes() -> None:
    """Two descriptions differing only in metadata + resources render
    byte-equal: only name/description/body reach the canonical Message, which
    is what keeps the ``semi_stable`` segment hash stable."""
    plain = SkillDescription(
        name="d", description="same desc", body="same body\n"
    )
    decorated = SkillDescription(
        name="d",
        description="same desc",
        body="same body\n",
        metadata=(("allowed-tools", "[Read]"), ("license", "MIT")),
        resources=("a.md", "scripts/x.sh"),
    )
    # Unequal as objects (metadata/resources participate in eq), identical on
    # the wire — that gap is exactly the property under test.
    assert plain != decorated
    rendered_plain = SkillRegistry({"d": plain}).render(["d"])
    rendered_decorated = SkillRegistry({"d": decorated}).render(["d"])
    assert rendered_plain.messages == rendered_decorated.messages
    assert rendered_plain.selected_skills == rendered_decorated.selected_skills


def test_semi_stable_segment_hash_stable_across_metadata(tmp_path: Path) -> None:
    """Composer-level proof of the same property: activating a skill that
    differs only in metadata/resources yields the same ``semi_stable`` hash."""

    def _hash_for(registry: SkillRegistry) -> str:
        cs = InMemoryContentStore()
        composer = build_coding_composer(
            system_prompt="coding-Agent role",
            tools={},
            content_store=cs,
            skill_registry=registry,
        )
        task = Task(task_id="t", status="running", state=TaskState(goal="g"))
        task.state.active_skills.append("d")
        view = composer.compose(task)
        plan = _read_plan(cs, view)
        segment_hashes = plan["segment_hashes"]
        assert isinstance(segment_hashes, dict)
        semi_stable_hash = segment_hashes["semi_stable"]
        assert isinstance(semi_stable_hash, str)
        return semi_stable_hash

    plain = SkillDescription(name="d", description="x", body="b\n")
    decorated = SkillDescription(
        name="d",
        description="x",
        body="b\n",
        metadata=(("k", "v"),),
        resources=("r.md",),
    )
    assert _hash_for(SkillRegistry({"d": plain})) == _hash_for(
        SkillRegistry({"d": decorated})
    )


# ---------------------------------------------------------------------------
# End-to-end activation of a REAL public skill through the Engine
# ---------------------------------------------------------------------------


def _leased_engine(tmp_path: Path) -> tuple[Engine, Task, str, InMemoryEventLog, InMemoryContentStore]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    registry = load_workspace_skills(
        workspace, override_skills_dir=OSS_SKILLS_DIR
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
    engine = Engine(event_log=log, content_store=cs, composer=composer)
    task = engine.create_task(goal="report", policy_name="react")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="rec-worker")
    assert lease is not None
    return engine, task, lease.lease_id, log, cs


def test_real_public_skill_activates_and_materialises(tmp_path: Path) -> None:
    engine, task, lease_id, log, cs = _leased_engine(tmp_path)

    activate_skills(engine, task, skills=["session-report"], lease_id=lease_id)

    # Activation is durable, so a resume does not lose the skill.
    patched = [e for e in log.read(task.task_id) if e.type == "TaskStatePatched"]
    assert len(patched) == 1
    assert patched[0].payload.patch["activate_skills"] == ["session-report"]

    view = engine._composer.compose(task)  # noqa: SLF001 (test introspection)
    semi_stable = next(s for s in view.segments if s.name == "semi_stable")
    # ONE message per skill: body plus the skill's absolute base directory, so
    # bundled resources are reachable by `read` without inlining any of them.
    assert len(semi_stable.content) == 1
    block = semi_stable.content[0].content[0]
    assert isinstance(block, TextBlock)
    assert "Activated skill: session-report" in block.text
    assert "Session Report" in block.text  # from the real public body
    skill_dir = OSS_SKILLS_DIR / "session-report"
    assert f"Base directory for this skill: {skill_dir}" in block.text
    # No resource listing and no dedicated read tool are injected. The body may
    # name the script itself — that is verbatim SKILL.md, not a listing.
    assert "read_skill_resource" not in block.text
    assert "Bundled resources" not in block.text
    plan = _read_plan(cs, view)
    assert plan["selected_skills"] == ["session-report"]
    # Nothing is force-inlined, so there is no retrieval provenance to record.
    assert plan["retrieved_resources"] == []


def test_real_public_skill_activation_survives_fold(tmp_path: Path) -> None:
    engine, task, lease_id, log, _ = _leased_engine(tmp_path)
    activate_skills(engine, task, skills=["session-report"], lease_id=lease_id)
    rebuilt = fold(log, InMemoryContentStore(), task.task_id)
    assert rebuilt.state.active_skills == ["session-report"]


# ---------------------------------------------------------------------------
# Out-of-scope guard: scripts are recorded, NEVER executed
# ---------------------------------------------------------------------------


def test_bundled_script_is_recorded_but_not_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skill can ship a script (``session-report`` ships
    ``analyze-sessions.mjs``; ``refactor-guide`` ships ``scripts/check.sh``).
    Loading one only offers its path — the whole load → activate → compose
    flow must spawn **no subprocess**, because a skill anyone can publish must
    not gain execution simply by being read. Running a script is the separate,
    guarded ``run_skill_script`` tool."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("must not execute bundled skill scripts")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "call", _boom, raising=False)

    engine, task, lease_id, _, cs = _leased_engine(tmp_path)
    activate_skills(
        engine,
        task,
        skills=["session-report", "refactor-guide"],
        lease_id=lease_id,
    )
    view = engine._composer.compose(task)  # noqa: SLF001
    # The script paths ARE recorded as resources...
    registry = load_workspace_skills(
        tmp_path / "ws", override_skills_dir=OSS_SKILLS_DIR
    )
    sr = registry.get("session-report")
    rg = registry.get("refactor-guide")
    assert sr is not None and "analyze-sessions.mjs" in sr.resources
    assert rg is not None and "scripts/check.sh" in rg.resources
    # ...reachable by path via the skill's base directory, but never inlined
    # and never executed — reaching this line at all means _boom never fired.
    semi_stable = next(s for s in view.segments if s.name == "semi_stable")
    assert len(semi_stable.content) == 2  # one message per skill
    rg_dir = OSS_SKILLS_DIR / "refactor-guide"
    assert f"Base directory for this skill: {rg_dir}" in _semi_stable_text(view)
    plan = _read_plan(cs, view)
    assert plan["retrieved_resources"] == []
