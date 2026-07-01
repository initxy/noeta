"""SkillRegistry resolve / render tests (issue 21).

Indexer-level disk scanning lives in ``test_skill_indexer.py``; here we
construct the Registry directly from synthetic :class:`SkillDescription`
instances so each behaviour is in isolation.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from noeta.context.composer import RenderedSkills
from noeta.context.skills.indexer import (
    SkillDescription,
    SkillRegistry,
    build_skill_renderer,
)
from noeta.protocols.messages import Message, TextBlock


def _desc(
    name: str,
    *,
    description: str = "d",
    body: str = "b",
    priority: int = 100,
    version: str = "1",
    source_path: Path | None = None,
) -> SkillDescription:
    return SkillDescription(
        name=name,
        description=description,
        body=body,
        version=version,
        priority=priority,
        source_path=source_path,
    )


def _registry(*descs: SkillDescription) -> SkillRegistry:
    return SkillRegistry({d.name: d for d in descs})


# ---------------------------------------------------------------------------
# get / names
# ---------------------------------------------------------------------------


def test_get_returns_description_by_name() -> None:
    registry = _registry(_desc("a"))
    assert registry.get("a") is not None
    assert registry.get("missing") is None


def test_names_returns_registered_keys() -> None:
    registry = _registry(_desc("a"), _desc("b"))
    assert set(registry.names()) == {"a", "b"}


def test_registry_defensive_copy() -> None:
    """Mutating the dict passed to ``SkillRegistry`` must not affect
    the Registry — confirms the dict-copy at __init__ is honest."""
    source = {"a": _desc("a")}
    registry = SkillRegistry(source)
    source.pop("a")
    assert registry.get("a") is not None


# ---------------------------------------------------------------------------
# resolve — drop unknown, dedupe, sort
# ---------------------------------------------------------------------------


def test_resolve_drops_unknown_active_skill(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = _registry(_desc("known"))
    caplog.set_level(logging.INFO, logger="noeta.context.skills.indexer")
    resolved = registry.resolve(["known", "ghost"])
    assert [d.name for d in resolved] == ["known"]
    assert any("ghost" in r.getMessage() for r in caplog.records)


def test_resolve_deduplicates_active_list() -> None:
    registry = _registry(_desc("a"))
    resolved = registry.resolve(["a", "a", "a"])
    assert [d.name for d in resolved] == ["a"]


def test_resolve_orders_by_priority_then_name() -> None:
    registry = _registry(
        _desc("c", priority=50),
        _desc("b", priority=10),
        _desc("a", priority=10),
    )
    resolved = registry.resolve(["c", "b", "a"])
    # priority 10 winners (a, b) before 50 (c); within priority by name
    assert [d.name for d in resolved] == ["a", "b", "c"]


def test_resolve_input_order_does_not_affect_render_order() -> None:
    """Q3: Composer must be invariant to Policy reshuffles of
    ``active_skills``; render order is purely (priority, name)."""
    registry = _registry(
        _desc("alpha", priority=20),
        _desc("beta", priority=10),
    )
    first = registry.resolve(["alpha", "beta"])
    second = registry.resolve(["beta", "alpha"])
    assert [d.name for d in first] == [d.name for d in second] == ["beta", "alpha"]


def test_resolve_empty_input_yields_empty_tuple() -> None:
    registry = _registry(_desc("a"))
    assert registry.resolve([]) == ()


# ---------------------------------------------------------------------------
# render — RenderedSkills shape, role=user, "Activated skill" prefix
# ---------------------------------------------------------------------------


def test_render_returns_rendered_skills_dataclass() -> None:
    registry = _registry(_desc("a"))
    result = registry.render(["a"])
    assert isinstance(result, RenderedSkills)
    assert isinstance(result.messages, list)
    assert isinstance(result.selected_skills, list)


def test_render_emits_user_role_messages_only() -> None:
    """P1: ``role='system'`` is forbidden inside ``LLMRequest.messages``
    (``messages.py:109``). All skill renders must use ``role='user'``.
    """
    registry = _registry(_desc("a"), _desc("b"))
    result = registry.render(["a", "b"])
    assert all(msg.role == "user" for msg in result.messages)


def test_render_message_text_uses_activated_skill_prefix() -> None:
    registry = _registry(_desc("greet", description="say hi", body="HELLO"))
    result = registry.render(["greet"])
    assert len(result.messages) == 1
    block = result.messages[0].content[0]
    assert isinstance(block, TextBlock)
    assert block.text == "Activated skill: greet\n\nsay hi\n\nHELLO"


def test_render_selected_skills_is_resolved_names_in_render_order() -> None:
    """P2: ``selected_skills`` is the post-filter, post-sort name list,
    not the raw active list. Composer writes this verbatim to
    ``ContextPlan.selected_skills``."""
    registry = _registry(
        _desc("alpha", priority=20),
        _desc("beta", priority=10),
    )
    result = registry.render(["alpha", "ghost", "beta"])
    assert result.selected_skills == ["beta", "alpha"]


def test_render_empty_active_returns_empty_rendered_skills() -> None:
    registry = _registry(_desc("a"))
    result = registry.render([])
    assert result.messages == []
    assert result.selected_skills == []


def test_render_unknown_only_returns_empty_rendered_skills() -> None:
    registry = _registry(_desc("a"))
    result = registry.render(["ghost"])
    assert result.messages == []
    assert result.selected_skills == []


# ---------------------------------------------------------------------------
# build_skill_renderer adapter
# ---------------------------------------------------------------------------


def test_build_skill_renderer_returns_registry_render() -> None:
    """Adapter must call straight through; Composer wires the result
    as ``skill_renderer`` so the seam type matches."""
    registry = _registry(_desc("a"))
    renderer = build_skill_renderer(registry)
    direct = registry.render(["a"])
    adapted = renderer(["a"])
    assert direct == adapted


# ---------------------------------------------------------------------------
# NB1 — SkillDescription default equality includes source_path,
#       but render output is byte-equal across source_path differences
# ---------------------------------------------------------------------------


def test_descriptions_with_different_source_paths_not_equal() -> None:
    """rev2 NB1: ``source_path`` participates in default dataclass
    equality so debug tooling can distinguish same-content files from
    different on-disk locations."""
    a = _desc("k", source_path=Path("/a/SKILL.md"))
    b = _desc("k", source_path=Path("/b/SKILL.md"))
    assert a != b


def test_render_embeds_source_path_base_directory() -> None:
    """(reverses NB1): ``render`` now emits the skill's parent
    directory as the ``Base directory for this skill:`` line so the model
    can ``read`` bundled references by path. Two checkouts at different
    paths therefore render DIFFERENTLY — each carries its own base dir —
    so resume is tied to the same skill paths (single-machine)."""
    reg_a = _registry(_desc("k", source_path=Path("/checkout-a/k/SKILL.md")))
    reg_b = _registry(_desc("k", source_path=Path("/checkout-b/k/SKILL.md")))
    text_a = reg_a.render(["k"]).messages[0].content[0].text
    text_b = reg_b.render(["k"]).messages[0].content[0].text
    assert "Base directory for this skill: /checkout-a/k" in text_a
    assert "Base directory for this skill: /checkout-b/k" in text_b
    assert reg_a.render(["k"]) != reg_b.render(["k"])
