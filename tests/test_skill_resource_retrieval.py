"""A skill's bundled resources are
read on demand with the ordinary ``read`` tool, not a dedicated tool.

The renderer prepends a ``Base directory for this skill: <abs dir>`` line
(Claude Code's contract) so the model can resolve the body's relative
references to an absolute path and ``read`` them. Nothing is inlined and
the renderer reads no resource bytes. This file covers the base-directory
line, the "content never enters the prompt" property, the synthetic
(source_path-less) case, and the ``ContextPlan`` backward-compat restore.
"""

from __future__ import annotations

from pathlib import Path

from noeta.context.skills import SkillIndexer
from noeta.context.skills.indexer import SkillDescription, SkillRegistry
from noeta.protocols.canonical import from_canonical, to_canonical
from noeta.protocols.context_plan import ContextPlan
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.messages import TextBlock

_BASE_MARKER = "Base directory for this skill:"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _registry(
    tmp_path: Path, name: str, body: str, resources: dict[str, str]
) -> SkillRegistry:
    skill_dir = tmp_path / "skills" / name
    _write(skill_dir / "SKILL.md", f"---\nname: {name}\ndescription: d\n---\n{body}\n")
    for rel, content in resources.items():
        _write(skill_dir / rel, content)
    return SkillIndexer(tmp_path / "skills").index()


def _rendered_text(reg: SkillRegistry, name: str) -> str:
    rendered = reg.render([name])
    return "\n".join(
        b.text
        for m in rendered.messages
        for b in m.content
        if isinstance(b, TextBlock)
    )


# ---------------------------------------------------------------------------
# base-directory line — the model's hook to ``read`` bundled references
# ---------------------------------------------------------------------------


def test_base_directory_line_points_at_skill_dir(tmp_path: Path) -> None:
    reg = _registry(
        tmp_path,
        "s",
        "see references/DEEPENING.md",
        {"references/DEEPENING.md": "deep"},
    )
    text = _rendered_text(reg, "s")
    skill_dir = tmp_path / "skills" / "s"
    assert f"{_BASE_MARKER} {skill_dir}" in text


def test_body_is_rendered_verbatim(tmp_path: Path) -> None:
    reg = _registry(tmp_path, "s", "read NOTE.md first", {"NOTE.md": "n"})
    assert "read NOTE.md first" in _rendered_text(reg, "s")


# ---------------------------------------------------------------------------
# resource content is NEVER read or inlined by the renderer
# ---------------------------------------------------------------------------


def test_content_not_inlined(tmp_path: Path) -> None:
    reg = _registry(
        tmp_path, "s", "read DEEPENING.md", {"DEEPENING.md": "DEEP SECRET CONTENT"}
    )
    rendered = reg.render(["s"])
    text = _rendered_text(reg, "s")
    assert "DEEP SECRET CONTENT" not in text
    assert "Resource: DEEPENING.md" not in text  # the old inline header is gone
    # the renderer retrieves nothing — provenance stays empty.
    assert rendered.retrieved_resources == []


def test_render_has_one_message_per_skill(tmp_path: Path) -> None:
    reg = _registry(
        tmp_path,
        "s",
        "read DEEPENING.md and PATTERNS.md",
        {"DEEPENING.md": "d", "PATTERNS.md": "p"},
    )
    rendered = reg.render(["s"])
    assert len(rendered.messages) == 1


def test_content_not_in_semi_stable(tmp_path: Path) -> None:
    from noeta.context.composer import ThreeSegmentComposer
    from noeta.context.skills import build_skill_renderer
    from noeta.protocols.task import Task, TaskState
    from noeta.storage.memory import InMemoryContentStore

    reg = _registry(
        tmp_path, "s", "read DEEPENING.md", {"DEEPENING.md": "DEEP CONTENT"}
    )
    composer = ThreeSegmentComposer(
        system_prompt="p",
        tools={},
        content_store=InMemoryContentStore(),
        skill_renderer=build_skill_renderer(reg),
    )
    task = Task(task_id="t", status="running", state=TaskState(goal="g"))
    TaskStatePatch(activate_skills=["s"]).apply(task.state)
    view = composer.compose(task)
    semi = next(s for s in view.segments if s.name == "semi_stable")
    texts = " ".join(
        b.text for m in semi.content for b in m.content if isinstance(b, TextBlock)
    )
    assert _BASE_MARKER in texts  # base dir surfaced for on-demand read
    assert "DEEP CONTENT" not in texts  # but content not inlined


# ---------------------------------------------------------------------------
# synthetic (source_path-less) description — body only, no base line
# ---------------------------------------------------------------------------


def test_source_path_none_renders_body_only(tmp_path: Path) -> None:
    desc = SkillDescription(
        name="syn",
        description="d",
        body="references DEEPENING.md",
        resources=("DEEPENING.md",),  # source_path is None
    )
    reg = SkillRegistry({"syn": desc})
    rendered = reg.render(["syn"])
    text = "\n".join(
        b.text
        for m in rendered.messages
        for b in m.content
        if isinstance(b, TextBlock)
    )
    assert _BASE_MARKER not in text  # no disk root to surface
    assert "references DEEPENING.md" in text
    assert rendered.retrieved_resources == []
    assert rendered.selected_skills == ["syn"]


# ---------------------------------------------------------------------------
# ContextPlan backward-compat restore (unchanged invariant)
# ---------------------------------------------------------------------------


def test_old_plan_without_retrieved_resources_restores_empty() -> None:
    plan = ContextPlan(
        composer_version="three_segment.v1",
        segment_hashes={
            "stable_prefix": "h",
            "semi_stable": "h",
            "dynamic_suffix": "h",
        },
        selected_skills=["s"],
    )
    body = to_canonical(plan)
    assert isinstance(body, dict)
    del body["retrieved_resources"]  # simulate a pre-D plan body
    restored = from_canonical(body)
    assert isinstance(restored, ContextPlan)
    assert restored.retrieved_resources == []
    assert restored.selected_skills == ["s"]
