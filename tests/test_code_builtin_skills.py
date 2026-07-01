"""D5 — package-bundled built-in skills (noeta-code).

The six built-in skills ship inside
``noeta/agent/skills_builtin/<name>/SKILL.md`` and are loaded independently of
the per-workspace skill pack so the default runner / replay path is untouched.

The load-bearing invariants pinned here:

* ``load_builtin_skills().names()`` carries **all six** expected names.
* Each ``SkillDescription`` is well-formed: ``desc.name == <dir name>`` and a
  non-empty ``description``.
* **No built-in skill declares ``allowed-tools``** — declaring it would feed
  ``extract_skill_allowed_tools_raw`` and perturb the ``PermissionPolicy``
  fingerprint (architecture red line #2). This test is the regression that
  keeps the red line loud.
* ``merge_skill_registries`` is a real merge: ``overlay`` wins on a name
  clash and the union size is exact.
* An activated built-in skill materialises its body through the composer
  (same renderer the runner uses) — proving the registry is composer-ready.
"""

from __future__ import annotations

from pathlib import Path

from noeta.agent.skills import BUILTIN_SKILLS_DIR, load_builtin_skills
from noeta.execution.skills import build_skill_composer as build_coding_composer
from noeta.execution.skills import merge_skill_registries
from noeta.context.skills import (
    SkillDescription,
    SkillIndexer,
    SkillRegistry,
)
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.messages import TextBlock
from noeta.protocols.task import Task, TaskState
from noeta.storage.memory import InMemoryContentStore


_EXPECTED_BUILTIN_SKILLS = frozenset(
    {"init", "review", "verify", "simplify", "commit", "handoff"}
)


# ---------------------------------------------------------------------------
# load_builtin_skills — shape + per-skill well-formedness
# ---------------------------------------------------------------------------


def test_builtin_skills_dir_points_at_packaged_layout() -> None:
    """The constant resolves to ``noeta/agent/skills_builtin`` and each
    expected skill lives at ``<name>/SKILL.md`` (the SkillIndexer layout)."""
    assert BUILTIN_SKILLS_DIR.name == "skills_builtin"
    assert BUILTIN_SKILLS_DIR.is_dir()
    for name in _EXPECTED_BUILTIN_SKILLS:
        assert (BUILTIN_SKILLS_DIR / name / "SKILL.md").is_file()


def test_load_builtin_skills_carries_all_six_names() -> None:
    registry = load_builtin_skills()
    assert set(registry.names()) == _EXPECTED_BUILTIN_SKILLS


def test_each_builtin_skill_is_well_formed() -> None:
    """``desc.name`` matches the directory name and ``description`` is
    non-empty for every built-in skill."""
    registry = load_builtin_skills()
    for name in _EXPECTED_BUILTIN_SKILLS:
        desc = registry.get(name)
        assert desc is not None, f"missing built-in skill {name!r}"
        assert desc.name == name
        assert desc.description.strip() != ""
        # A SKILL.md with a body — the indexer keeps the post-frontmatter
        # text as the renderable body.
        assert desc.body.strip() != ""


# ---------------------------------------------------------------------------
# Red line #2 — no built-in skill declares allowed-tools
# ---------------------------------------------------------------------------


def test_no_builtin_skill_declares_allowed_tools() -> None:
    """Declaring ``allowed-tools`` would perturb the PermissionPolicy
    fingerprint. ``metadata`` is the ``(key, value)`` tuple of the
    leftover frontmatter keys, so a missing ``allowed-tools`` key proves
    none was declared."""
    registry = load_builtin_skills()
    for name in registry.names():
        desc = registry.get(name)
        assert desc is not None
        keys = {key for key, _ in desc.metadata}
        assert "allowed-tools" not in keys, (
            f"built-in skill {name!r} must not declare allowed-tools"
        )


# ---------------------------------------------------------------------------
# merge_skill_registries — overlay wins + exact union size
# ---------------------------------------------------------------------------


def _registry_with(name: str, description: str) -> SkillRegistry:
    desc = SkillDescription(name=name, description=description, body="b")
    return SkillRegistry({name: desc})


def test_merge_overlay_wins_on_name_clash() -> None:
    base = _registry_with("review", "base review")
    overlay = _registry_with("review", "overlay review")

    merged = merge_skill_registries(base, overlay)

    assert merged.names() == ("review",)
    got = merged.get("review")
    assert got is not None
    assert got.description == "overlay review"


def test_merge_union_size_is_exact() -> None:
    """Disjoint names produce a union whose size is the sum; one shared
    name collapses to a single entry (overlay wins)."""
    base = load_builtin_skills()
    overlay_only = _registry_with("workspace-extra", "an overlay-only skill")
    overlay_clash = _registry_with("review", "overlaid review")
    overlay = merge_skill_registries(overlay_only, overlay_clash)

    merged = merge_skill_registries(base, overlay)

    expected = set(base.names()) | {"workspace-extra"}
    assert set(merged.names()) == expected
    # Union size = 6 built-ins + 1 overlay-only (the clash on "review"
    # does not grow the set).
    assert len(merged.names()) == len(_EXPECTED_BUILTIN_SKILLS) + 1
    # The clashing name took the overlay's body.
    review = merged.get("review")
    assert review is not None
    assert review.description == "overlaid review"


def test_merge_does_not_mutate_inputs() -> None:
    base = load_builtin_skills()
    base_names_before = set(base.names())
    overlay = _registry_with("review", "overlaid")

    merge_skill_registries(base, overlay)

    # Neither input registry is touched.
    assert set(base.names()) == base_names_before
    assert overlay.names() == ("review",)


def test_load_builtin_skills_round_trips_through_indexer() -> None:
    """``load_builtin_skills`` is exactly ``SkillIndexer(BUILTIN_SKILLS_DIR)``
    — re-indexing the same directory yields the same name set."""
    direct = SkillIndexer(BUILTIN_SKILLS_DIR).index()
    assert set(direct.names()) == set(load_builtin_skills().names())


# ---------------------------------------------------------------------------
# Composer can render a built-in skill body (registry is composer-ready)
# ---------------------------------------------------------------------------


def test_activated_builtin_skill_renders_through_composer() -> None:
    registry = load_builtin_skills()
    composer = build_coding_composer(
        system_prompt="coding-Agent role + tools + safety",
        tools={},
        content_store=InMemoryContentStore(),
        skill_registry=registry,
    )
    task = Task(task_id="t-builtin", status="running", state=TaskState(goal="g"))
    TaskStatePatch(activate_skills=["review"]).apply(task.state)

    view = composer.compose(task)

    semi_stable = next(s for s in view.segments if s.name == "semi_stable")
    assert len(semi_stable.content) == 1
    first_block = semi_stable.content[0].content[0]
    assert isinstance(first_block, TextBlock)
    assert "Activated skill: review" in first_block.text
