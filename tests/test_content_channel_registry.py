"""SDK-side content channel: the composer reads a generic activation map.

Covers the SDK half of the generic content channel:

* ``ContentKindSpec`` / ``ContentChannelRegistry`` — one registry item per
  content kind: how to render (``renderer``), how to fingerprint
  (``hashes``), and which drift ``policy`` its recordings carry.
* ``ThreeSegmentComposer`` renders the ``semi_stable`` segment from the
  generic activation map (``TaskState.active_content``) through the
  registry; the skill renderer is subsumed as the ``kind="skill"``
  registry item and the rendered bytes are equal to the legacy
  ``skill_renderer`` path (golden recordings stay green, no re-pin).
* A brand-new (fake) kind walks the whole journey — register, activate
  (fold from the ledger), render, resolve hashes — with **zero runtime
  changes**.
* Red-line guard: compose stays a pure function of folded state — the
  same ledger composed twice yields byte-identical Views.
"""

from __future__ import annotations

import pytest

from noeta.context.composer import (
    RenderedSkills,
    ThreeSegmentComposer,
)
from noeta.context.content_channel import (
    ContentChannelRegistry,
    ContentKindSpec,
)
from noeta.context.skills import (
    SkillDescription,
    SkillRegistry,
    build_skill_renderer,
)
from noeta.core.fold import fold
from noeta.execution.skills import build_skill_composer, skill_content_kind
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.events import (
    ContextContentRecordedPayload,
    TaskCreatedPayload,
    TaskStatePatchedPayload,
)
from noeta.protocols.messages import Message, TextBlock
from noeta.protocols.task import Task
from noeta.protocols.view import View
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_renderer(prefix: str):
    """A minimal kind renderer: one user Message per name, input order."""

    def _render(names: list[str]) -> RenderedSkills:
        return RenderedSkills(
            messages=[
                Message(role="user", content=[TextBlock(text=f"{prefix}:{n}")])
                for n in names
            ],
            selected_skills=list(names),
        )

    return _render


def _skill_registry() -> SkillRegistry:
    return SkillRegistry(
        {
            "alpha": SkillDescription(
                name="alpha", description="da", body="ba", priority=20
            ),
            "beta": SkillDescription(
                name="beta", description="db", body="bb", priority=10
            ),
        }
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


def _ledger_with(*payloads: ContextContentRecordedPayload) -> tuple[
    InMemoryEventLog, InMemoryContentStore
]:
    log, cs = InMemoryEventLog(), InMemoryContentStore()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    for payload in payloads:
        log.emit(task_id="t1", type="ContextContentRecorded", payload=payload)
    return log, cs


def _folded_skill_task(skills: list[str]) -> Task:
    """A task whose skill activation went through the durable patch route
    (sugar list + generic map in lockstep, as fold produces)."""
    log, cs = InMemoryEventLog(), InMemoryContentStore()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="t1",
        type="TaskStatePatched",
        payload=TaskStatePatchedPayload(
            patch=TaskStatePatch(activate_skills=list(skills)).to_dict()
        ),
    )
    return fold(log, cs, "t1")


def _sugar_only_skill_task(skills: list[str]) -> Task:
    """A task built directly (unit-test style): only the sugar list is
    set, the generic map is empty — the legacy bridge must still render."""
    task = Task(task_id="t1")
    task.state.active_skills = list(skills)
    return task


def _assert_views_byte_equal(v1: View, v2: View) -> None:
    assert [s.segment_hash for s in v1.segments] == [
        s.segment_hash for s in v2.segments
    ]
    for s1, s2 in zip(v1.segments, v2.segments):
        assert to_canonical_bytes(s1.content) == to_canonical_bytes(s2.content)
    assert v1.plan_ref == v2.plan_ref


def _semi_texts(view: View) -> list[str]:
    semi = view.segments[1]
    assert semi.name == "semi_stable"
    return [b.text for m in semi.content for b in m.content]


# ---------------------------------------------------------------------------
# Registry unit behaviour
# ---------------------------------------------------------------------------


def test_registry_kinds_in_registration_order() -> None:
    registry = ContentChannelRegistry(
        [
            ContentKindSpec(kind="skill", renderer=_fake_renderer("S")),
            ContentKindSpec(kind="memory", renderer=_fake_renderer("M")),
        ]
    )
    assert registry.kinds() == ("skill", "memory")


def test_registry_duplicate_kind_raises() -> None:
    with pytest.raises(ValueError):
        ContentChannelRegistry(
            [
                ContentKindSpec(kind="skill", renderer=_fake_renderer("A")),
                ContentKindSpec(kind="skill", renderer=_fake_renderer("B")),
            ]
        )


def test_registry_invalid_policy_raises() -> None:
    with pytest.raises(ValueError):
        ContentKindSpec(
            kind="memory", renderer=_fake_renderer("M"), policy="whatever"
        )


def test_registry_blank_kind_raises() -> None:
    with pytest.raises(ValueError):
        ContentKindSpec(kind="", renderer=_fake_renderer("X"))


def test_registry_render_dispatches_to_kind_renderer() -> None:
    registry = ContentChannelRegistry(
        [ContentKindSpec(kind="fact", renderer=_fake_renderer("FACT"))]
    )
    rendered = registry.render("fact", ["x", "y"])
    assert [b.text for m in rendered.messages for b in m.content] == [
        "FACT:x",
        "FACT:y",
    ]


def test_registry_content_hashes_resolves_by_kind_and_name() -> None:
    registry = ContentChannelRegistry(
        [
            ContentKindSpec(
                kind="fact",
                renderer=_fake_renderer("FACT"),
                hashes={"x": ("7", "h-x")}.get,
                policy="evolving",
            ),
            ContentKindSpec(kind="bare", renderer=_fake_renderer("B")),
        ]
    )
    resolve = registry.content_hashes()
    assert resolve("fact", "x") == ("7", "h-x")
    assert resolve("fact", "unknown-name") is None
    assert resolve("bare", "x") is None  # item without a hashes fn
    assert resolve("unknown-kind", "x") is None


# ---------------------------------------------------------------------------
# Skill folded in — registry item renders byte-equal with the legacy renderer
# ---------------------------------------------------------------------------


def test_skill_via_registry_item_byte_equal_with_legacy_renderer() -> None:
    reg = _skill_registry()
    legacy = ThreeSegmentComposer(
        system_prompt="sp",
        tools={},
        content_store=InMemoryContentStore(),
        skill_renderer=build_skill_renderer(reg),
    )
    via_registry = ThreeSegmentComposer(
        system_prompt="sp",
        tools={},
        content_store=InMemoryContentStore(),
        content_renderers=ContentChannelRegistry([skill_content_kind(reg)]),
    )
    for task in (
        _folded_skill_task(["beta", "alpha"]),
        _sugar_only_skill_task(["beta", "alpha"]),
        _folded_skill_task([]),
    ):
        _assert_views_byte_equal(
            legacy.compose(task), via_registry.compose(task)
        )


def test_build_skill_composer_byte_equal_with_legacy_construction() -> None:
    """``build_skill_composer`` now wires the registry path internally —
    its composed bytes must equal a direct legacy ``skill_renderer``
    construction (the product path stays byte-stable, golden untouched)."""
    reg = _skill_registry()
    product = build_skill_composer(
        system_prompt="sys",
        tools={},
        content_store=InMemoryContentStore(),
        skill_registry=reg,
    )
    legacy = ThreeSegmentComposer(
        system_prompt="sys",
        tools={},
        content_store=InMemoryContentStore(),
        skill_renderer=build_skill_renderer(reg),
    )
    task = _folded_skill_task(["alpha", "beta"])
    _assert_views_byte_equal(legacy.compose(task), product.compose(task))


def test_skill_kind_spec_carries_pinned_policy_and_hashes() -> None:
    reg = _skill_registry()
    spec = skill_content_kind(reg)
    assert spec.kind == "skill"
    assert spec.policy == "pinned"
    assert spec.hashes is not None
    version, content_hash = spec.hashes("alpha")
    assert version == "1"
    assert len(content_hash) == 64  # sha256 hex of the skill body
    assert spec.hashes("unknown") is None


def test_composer_rejects_both_skill_renderer_and_content_renderers() -> None:
    reg = _skill_registry()
    with pytest.raises(ValueError):
        ThreeSegmentComposer(
            system_prompt="",
            tools={},
            content_store=InMemoryContentStore(),
            skill_renderer=build_skill_renderer(reg),
            content_renderers=ContentChannelRegistry(
                [skill_content_kind(reg)]
            ),
        )


# ---------------------------------------------------------------------------
# A new kind = one registry item + one render rule (full journey, zero runtime changes)
# ---------------------------------------------------------------------------


def test_fake_kind_full_journey() -> None:
    """Register → activate (fold from the ledger) → render → resolve
    hashes, for a kind the runtime has never heard of."""
    # 1) Register: one registry item + one render rule. Nothing else.
    spec = ContentKindSpec(
        kind="fact",
        renderer=_fake_renderer("FACT"),
        hashes={"x": ("7", "h-x")}.get,
        policy="evolving",
    )
    registry = ContentChannelRegistry([spec])

    # 2) Activate: the generic event folds into the activation map.
    log, cs = _ledger_with(
        _content_event(kind="fact", name="x", version="7", content_hash="h-x")
    )
    task = fold(log, cs, "t1")
    assert task.state.active_content == {"fact": ("x",)}

    # 3) Render: the composer materialises the kind into semi_stable.
    composer = ThreeSegmentComposer(
        system_prompt="",
        tools={},
        content_store=cs,
        content_renderers=registry,
    )
    view = composer.compose(task)
    assert _semi_texts(view) == ["FACT:x"]

    # 4) Hashes: the generic (kind, name) seam resolves through the item.
    assert registry.content_hashes()("fact", "x") == ("7", "h-x")


def test_multi_kind_semi_stable_in_registration_order() -> None:
    reg = _skill_registry()
    registry = ContentChannelRegistry(
        [
            skill_content_kind(reg),
            ContentKindSpec(
                kind="fact", renderer=_fake_renderer("FACT"), policy="evolving"
            ),
        ]
    )
    log, cs = _ledger_with(_content_event(kind="fact", name="x"))
    log.emit(
        task_id="t1",
        type="TaskStatePatched",
        payload=TaskStatePatchedPayload(
            patch=TaskStatePatch(activate_skills=["alpha"]).to_dict()
        ),
    )
    task = fold(log, cs, "t1")
    composer = ThreeSegmentComposer(
        system_prompt="",
        tools={},
        content_store=cs,
        content_renderers=registry,
    )
    texts = _semi_texts(composer.compose(task))
    # Skill body first (registration order), then the fake kind.
    assert texts == ["Activated skill: alpha\n\nda\n\nba", "FACT:x"]


def test_unregistered_kind_in_state_is_ignored() -> None:
    """A kind present in the folded map but absent from the registry is
    silently skipped (an old recording replayed by a host that no longer
    registers the kind must not crash the compose)."""
    registry = ContentChannelRegistry(
        [ContentKindSpec(kind="fact", renderer=_fake_renderer("FACT"))]
    )
    log, cs = _ledger_with(
        _content_event(kind="fact", name="x"),
        _content_event(kind="exotic", name="z"),
    )
    task = fold(log, cs, "t1")
    composer = ThreeSegmentComposer(
        system_prompt="",
        tools={},
        content_store=cs,
        content_renderers=registry,
    )
    assert _semi_texts(composer.compose(task)) == ["FACT:x"]


def test_non_skill_kind_never_leaks_into_selected_skills() -> None:
    """``ContextPlan.selected_skills`` stays skill-only even though the
    fake kind's renderer reports selected names (plan bytes are golden)."""
    from noeta.protocols.canonical import from_canonical_bytes

    registry = ContentChannelRegistry(
        [
            ContentKindSpec(
                kind="fact", renderer=_fake_renderer("FACT"), policy="evolving"
            )
        ]
    )
    log, cs = _ledger_with(_content_event(kind="fact", name="x"))
    task = fold(log, cs, "t1")
    composer = ThreeSegmentComposer(
        system_prompt="",
        tools={},
        content_store=cs,
        content_renderers=registry,
    )
    view = composer.compose(task)
    plan = from_canonical_bytes(cs.get(view.plan_ref))
    selected = (
        plan.selected_skills
        if hasattr(plan, "selected_skills")
        else plan["selected_skills"]
    )
    assert list(selected) == []


# ---------------------------------------------------------------------------
# Red-line guard — compose is a pure function of fold state (same ledger, twice = identical bytes)
# ---------------------------------------------------------------------------


def _registry_for_purity(reg: SkillRegistry) -> ContentChannelRegistry:
    return ContentChannelRegistry(
        [
            skill_content_kind(reg),
            ContentKindSpec(
                kind="fact", renderer=_fake_renderer("FACT"), policy="evolving"
            ),
        ]
    )


def test_compose_pure_same_ledger_twice_byte_identical() -> None:
    """Red line: the composer is a pure read of fold state —
    folding the same ledger twice and composing through two independently
    constructed (identically configured) composers yields byte-identical
    Views. Any compose-time callback to an external source would break
    this and is forbidden."""
    reg = _skill_registry()
    log, cs = _ledger_with(_content_event(kind="fact", name="x"))
    log.emit(
        task_id="t1",
        type="TaskStatePatched",
        payload=TaskStatePatchedPayload(
            patch=TaskStatePatch(activate_skills=["beta"]).to_dict()
        ),
    )

    views: list[View] = []
    for _ in range(2):
        task = fold(log, cs, "t1")  # independent fold of the same ledger
        composer = ThreeSegmentComposer(
            system_prompt="sys",
            tools={},
            content_store=cs,
            content_renderers=_registry_for_purity(reg),
        )
        views.append(composer.compose(task))
    _assert_views_byte_equal(views[0], views[1])

    # Same composer instance re-composing the same state is equally stable.
    task = fold(log, cs, "t1")
    composer = ThreeSegmentComposer(
        system_prompt="sys",
        tools={},
        content_store=cs,
        content_renderers=_registry_for_purity(reg),
    )
    _assert_views_byte_equal(composer.compose(task), composer.compose(task))
