"""Content-channel rendering + message-stream origin pass-through.

The verify-era per-entry View source attribution (``ViewSegment.entry_sources``
/ ``RenderedSkills.entry_names``) was retired with verify/replay. What survives
and is exercised here:

* Content-channel entries (``skill:`` / ``memory:``) render into the
  ``semi_stable`` segment; a skill renders its body plus an absolute
  base-directory line into ONE message, without force-inlining resources.
* Message-stream entries pass through D4's ``origin`` (neither the thinking
  re-attach nor the tail-prune transform path loses origin).
"""

from __future__ import annotations

from pathlib import Path

from noeta.context.composer import RenderedSkills, ThreeSegmentComposer
from noeta.context.content_channel import ContentChannelRegistry, ContentKindSpec
from noeta.context.skills import SkillIndexer
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.messages import (
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.task import Task
from noeta.protocols.view import View, ViewSegment
from noeta.storage.memory import InMemoryContentStore


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _renderer(kind_tag: str):
    """Renderer: one user message per name."""

    def _render(names: list[str]) -> RenderedSkills:
        return RenderedSkills(
            messages=[
                Message(
                    role="user", content=[TextBlock(text=f"{kind_tag}:{n}")]
                )
                for n in names
            ],
            selected_skills=list(names),
        )

    return _render


def _composer(
    cs: InMemoryContentStore,
    registry: ContentChannelRegistry,
    **kwargs,
) -> ThreeSegmentComposer:
    return ThreeSegmentComposer(
        system_prompt="label test agent",
        tools={},
        content_store=cs,
        content_renderers=registry,
        **kwargs,
    )


def _segment(view: View, name: str) -> ViewSegment:
    return next(s for s in view.segments if s.name == name)


# ---------------------------------------------------------------------------
# Content-channel entries — skill body + base directory in one message
# ---------------------------------------------------------------------------


def test_skill_registry_renders_body_and_base_directory(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "skills" / "deep"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: deep\ndescription: d\n---\nsee NOTES.md for more\n",
        encoding="utf-8",
    )
    (skill_dir / "NOTES.md").write_text("notes body", encoding="utf-8")
    registry = SkillIndexer(tmp_path / "skills").index()

    rendered = registry.render(["deep"])

    # body + the skill's absolute base-directory line fold into
    # ONE message (no force-inlined resource).
    assert len(rendered.messages) == 1
    text = rendered.messages[0].content[0].text
    assert f"Base directory for this skill: {skill_dir}" in text  # read by path
    assert "notes body" not in text  # resource content not inlined

    cs = InMemoryContentStore()
    composer = ThreeSegmentComposer(
        system_prompt="p",
        tools={},
        content_store=cs,
        skill_renderer=registry.render,
    )
    task = Task(task_id="t1")
    TaskStatePatch(activate_skills=["deep"]).apply(task.state)
    view = composer.compose(task)
    semi = _segment(view, "semi_stable")
    assert len(semi.content) == 1


# ---------------------------------------------------------------------------
# Message-stream channel — passes through origin
# ---------------------------------------------------------------------------


def test_dynamic_suffix_passes_through_message_origin() -> None:
    cs = InMemoryContentStore()
    registry = ContentChannelRegistry(
        [ContentKindSpec(kind="skill", renderer=_renderer("S"))]
    )
    task = Task(task_id="t1")
    task.runtime.messages = [
        Message(role="user", content=[TextBlock(text="hi")]),
        Message(
            role="user",
            content=[TextBlock(text="recalled")],
            origin="memory",
        ),
        Message(role="assistant", content=[TextBlock(text="ok")]),
    ]

    view = _composer(cs, registry).compose(task)

    dyn = _segment(view, "dynamic_suffix")
    assert dyn.content[1].origin == "memory"


def test_origin_survives_thinking_reattach() -> None:
    cs = InMemoryContentStore()
    registry = ContentChannelRegistry(
        [ContentKindSpec(kind="skill", renderer=_renderer("S"))]
    )
    task = Task(task_id="t1")
    assistant = Message(
        role="assistant",
        content=[
            ToolUseBlock(call_id="c1", tool_name="t", arguments={}),
        ],
        origin="system",
    )
    task.runtime.messages = [assistant]
    task.context.thinking_by_call_id = {
        "c1": [ThinkingBlock(text="think", signature="sig")]
    }

    view = _composer(cs, registry).compose(task)

    dyn = _segment(view, "dynamic_suffix")
    assert isinstance(dyn.content[0].content[0], ThinkingBlock)  # re-attach took effect
    assert dyn.content[0].origin == "system"  # re-attach keeps origin


def test_origin_survives_tail_prune() -> None:
    cs = InMemoryContentStore()
    registry = ContentChannelRegistry(
        [ContentKindSpec(kind="skill", renderer=_renderer("S"))]
    )
    task = Task(task_id="t1")
    task.runtime.messages = [
        Message(
            role="user",
            content=[TextBlock(text="old recall")],
            origin="memory",
        ),
        Message(
            role="tool",
            content=[
                ToolResultBlock(call_id="c1", output="x" * 400, success=True)
            ],
        ),
        Message(role="user", content=[TextBlock(text="recent " * 50)]),
    ]

    view = _composer(cs, registry, tail_token_budget=10).compose(task)

    dyn = _segment(view, "dynamic_suffix")
    # Pruning took effect: the old tool output is replaced by the lean
    # placeholder marker, no longer an empty string.
    assert dyn.content[1].content[0].output == "[tool output cleared]"
    # origin pass-through is unaffected by pruning
    assert dyn.content[0].origin == "memory"
