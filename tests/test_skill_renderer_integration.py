"""Composer × SkillRegistry integration tests (issue 21).

Pins the rev3 acceptance criteria:
* **G3** Composer reads the fold-owned activation state only (the
  generic map ``TaskState.active_content`` since the issue-07
  generation switch; the patch sugar keeps it in lockstep with
  ``active_skills``); resolution / sorting happens inside
  :meth:`SkillRegistry.render`.
* **G4 (rev3 NB2)** A 5 KB skill body appears verbatim in the
  transient View, while the persisted ``ContextPlanComposed`` payload
  and ContentStore-backed ``ContextPlan`` body never contain the body
  literal.
* **G5** Determinism / drift: byte-equal output across compose calls;
  changing the Registry rotates ``semi_stable`` hash but not the
  other two segments; changing ``active_skills`` rotates only
  ``semi_stable``.
* **P2** ``view.plan_ref`` body deserialises with
  ``selected_skills`` set to the post-filter, post-sort name list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.context.composer import ThreeSegmentComposer
from noeta.context.skills.indexer import (
    SkillDescription,
    SkillRegistry,
    build_skill_renderer,
)
from noeta.protocols.canonical import from_canonical_bytes, to_canonical_bytes
from noeta.protocols.context_plan import ContextPlan
from noeta.protocols.events import ContextPlanComposedPayload
from noeta.protocols.messages import Message, TextBlock
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.task import Task, TaskState
from noeta.protocols.tool import Tool, ToolContext, ToolResult
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.risk_level = "low"
        self.input_schema = {"type": "object", "additionalProperties": True}

    def invoke(
        self, arguments: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:  # pragma: no cover
        raise NotImplementedError


def _composer_with(
    registry: SkillRegistry,
    *,
    store: InMemoryContentStore | None = None,
) -> tuple[ThreeSegmentComposer, InMemoryContentStore]:
    store = store or InMemoryContentStore()
    composer = ThreeSegmentComposer(
        system_prompt="you are a helpful agent",
        tools={"echo": _FakeTool("echo")},
        content_store=store,
        skill_renderer=build_skill_renderer(registry),
    )
    return composer, store


def _task(*, active_skills: list[str], user_text: str = "hi") -> Task:
    task = Task(task_id="t-1", state=TaskState())
    # Patch-sugar activation keeps the sugar list and the generic
    # activation map in lockstep (the composer reads the map only since
    # the issue-07 generation switch).
    TaskStatePatch(activate_skills=list(active_skills)).apply(task.state)
    task.runtime.messages.append(
        Message(role="user", content=[TextBlock(text=user_text)])
    )
    return task


def _registry(*descs: SkillDescription) -> SkillRegistry:
    return SkillRegistry({d.name: d for d in descs})


def _desc(name: str, *, description: str = "d", body: str = "b", priority: int = 100) -> SkillDescription:
    return SkillDescription(
        name=name,
        description=description,
        body=body,
        priority=priority,
    )


# ---------------------------------------------------------------------------
# G3 + Composer wiring
# ---------------------------------------------------------------------------


def test_active_skills_render_into_semi_stable_segment() -> None:
    registry = _registry(_desc("alpha", body="ALPHA-BODY"))
    composer, _ = _composer_with(registry)
    task = _task(active_skills=["alpha"])

    view = composer.compose(task)

    semi = view.segments[1]
    assert semi.name == "semi_stable"
    assert len(semi.content) == 1
    msg = semi.content[0]
    assert isinstance(msg, Message)
    assert msg.role == "user"
    block = msg.content[0]
    assert isinstance(block, TextBlock)
    assert "ALPHA-BODY" in block.text
    assert block.text.startswith("Activated skill: alpha\n\n")


def test_unknown_active_skill_dropped_from_semi_stable() -> None:
    registry = _registry(_desc("known"))
    composer, _ = _composer_with(registry)
    task = _task(active_skills=["known", "ghost"])

    view = composer.compose(task)

    # only `known` renders
    assert len(view.segments[1].content) == 1
    block = view.segments[1].content[0].content[0]
    assert isinstance(block, TextBlock)
    assert "Activated skill: known" in block.text


# ---------------------------------------------------------------------------
# G5 — determinism / drift
# ---------------------------------------------------------------------------


def test_same_registry_and_active_produces_byte_equal_view() -> None:
    registry = _registry(_desc("a"), _desc("b"))
    composer, _ = _composer_with(registry)
    task = _task(active_skills=["a", "b"])

    v1 = composer.compose(task)
    v2 = composer.compose(task)

    assert [s.segment_hash for s in v1.segments] == [s.segment_hash for s in v2.segments]
    assert v1.plan_ref == v2.plan_ref


def test_changing_active_skills_rotates_only_semi_stable_hash() -> None:
    registry = _registry(_desc("a"), _desc("b"))
    composer, _ = _composer_with(registry)
    task1 = _task(active_skills=["a"])
    task2 = _task(active_skills=["a", "b"])

    v1 = composer.compose(task1)
    v2 = composer.compose(task2)

    assert v1.segments[0].segment_hash == v2.segments[0].segment_hash
    assert v1.segments[1].segment_hash != v2.segments[1].segment_hash
    assert v1.segments[2].segment_hash == v2.segments[2].segment_hash


def test_changing_registry_body_rotates_semi_stable_hash() -> None:
    reg1 = _registry(_desc("a", body="v1"))
    reg2 = _registry(_desc("a", body="v2"))
    composer1, _ = _composer_with(reg1)
    composer2, _ = _composer_with(reg2)
    task = _task(active_skills=["a"])

    v1 = composer1.compose(task)
    v2 = composer2.compose(task)

    assert v1.segments[0].segment_hash == v2.segments[0].segment_hash
    assert v1.segments[1].segment_hash != v2.segments[1].segment_hash
    assert v1.segments[2].segment_hash == v2.segments[2].segment_hash


def test_removing_skill_rotates_semi_stable_hash() -> None:
    reg1 = _registry(_desc("a"), _desc("b"))
    reg2 = _registry(_desc("a"))  # b removed
    composer1, _ = _composer_with(reg1)
    composer2, _ = _composer_with(reg2)
    task = _task(active_skills=["a", "b"])

    v1 = composer1.compose(task)
    v2 = composer2.compose(task)

    assert v1.segments[1].segment_hash != v2.segments[1].segment_hash


# ---------------------------------------------------------------------------
# P2 — ContextPlan provenance
# ---------------------------------------------------------------------------


def test_plan_selected_skills_is_render_order_not_raw_active() -> None:
    """``selected_skills`` is the post-filter, post-sort name list,
    in render order — independent of the order Policy emitted in
    ``activate_skills``."""
    registry = _registry(
        _desc("alpha", priority=20),
        _desc("beta", priority=10),
    )
    composer, store = _composer_with(registry)
    task = _task(active_skills=["alpha", "ghost", "beta"])

    view = composer.compose(task)
    body = store.get(view.plan_ref)
    plan = from_canonical_bytes(body)
    assert isinstance(plan, ContextPlan)
    assert plan.selected_skills == ["beta", "alpha"]


def test_plan_selected_skills_excludes_unknown_active_skill() -> None:
    registry = _registry(_desc("known"))
    composer, store = _composer_with(registry)
    task = _task(active_skills=["known", "ghost"])

    view = composer.compose(task)
    plan = from_canonical_bytes(store.get(view.plan_ref))
    assert isinstance(plan, ContextPlan)
    assert plan.selected_skills == ["known"]


# ---------------------------------------------------------------------------
# G4 — persisted vs runtime body location (rev3 NB2)
# ---------------------------------------------------------------------------


def test_large_body_in_view_but_not_in_persisted_records() -> None:
    """rev3 G4 + NB2: a 5 KB skill body must
    1) appear verbatim in ``view.segments[1]`` (transient runtime),
    2) NOT appear in the canonical bytes of the actual
       ``ContextPlanComposed`` envelope read back from EventLog,
    3) NOT appear in the canonical bytes of the ``ContextPlan`` body
       stored in ContentStore (read via ``view.plan_ref``).
    Test method:
    fold-like ``event_log.read(task_id)`` + ``content_store.get(plan_ref)``.
    """
    big = "X" * 5000
    registry = _registry(_desc("big", description="huge skill", body=big))
    composer, store = _composer_with(registry)
    task = _task(active_skills=["big"])

    view = composer.compose(task)

    # 1) View carries the body literal (transient runtime)
    semi_block = view.segments[1].content[0].content[0]
    assert isinstance(semi_block, TextBlock)
    assert big in semi_block.text

    # 2) Persisted EventLog envelope for ContextPlanComposed: emit and
    #    read back to mirror Engine's append-then-fold pattern.
    event_log = InMemoryEventLog()
    event_log.system_emit(
        task_id=task.task_id,
        type="ContextPlanComposed",
        payload=ContextPlanComposedPayload(plan_ref=view.plan_ref),
        actor="composer",
        origin="engine",
        trace_id="trace-test",
    )
    envelopes = event_log.read(task.task_id)
    assert len(envelopes) == 1
    envelope_bytes = to_canonical_bytes(envelopes[0])
    assert big.encode("utf-8") not in envelope_bytes
    # The payload itself is just the ContentRef — small, body-free.
    payload_bytes = to_canonical_bytes(envelopes[0].payload)
    assert big.encode("utf-8") not in payload_bytes
    assert len(payload_bytes) < 4096

    # 3) ContextPlan body (via plan_ref) does NOT carry the body literal
    plan_bytes = store.get(view.plan_ref)
    assert big.encode("utf-8") not in plan_bytes
    plan = from_canonical_bytes(plan_bytes)
    assert isinstance(plan, ContextPlan)
    assert plan.selected_skills == ["big"]


# ---------------------------------------------------------------------------
# Default renderer fallback (no skills wired)
# ---------------------------------------------------------------------------


def test_default_renderer_with_active_skills_writes_empty_selected_skills() -> None:
    """``_default_skill_renderer`` returns
    ``RenderedSkills(messages=[], selected_skills=[])``: the Composer
    writes that empty list to ``ContextPlan.selected_skills`` even
    when ``task.state.active_skills`` is non-empty — the renderer is
    the canonical source of truth for what was rendered."""
    store = InMemoryContentStore()
    composer = ThreeSegmentComposer(
        system_prompt="",
        tools={},
        content_store=store,
    )
    task = _task(active_skills=["a", "b"])
    view = composer.compose(task)
    plan = from_canonical_bytes(store.get(view.plan_ref))
    assert isinstance(plan, ContextPlan)
    assert plan.selected_skills == []
    assert view.segments[1].content == []
