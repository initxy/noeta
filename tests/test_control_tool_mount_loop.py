"""Mechanism tests for the control-tool mount loop (control-tool-surface S1).

Covers the loop MECHANISM only — the byte-order contract and per-tool behaviour
are pinned elsewhere (``test_control_tool_schema_goldens.py`` + the ask / todo /
spawn / skill / workflow e2e suites). Here we prove:

* the two orders genuinely diverge — a mount with inverted schema/routing bands
  lands in a different position in each order;
* a factory that self-gates to ``None`` is skipped (mounting IS enablement);
* a mount with no ``translate`` (``structured_output``) contributes a schema but
  no routing spec;
* a duplicate mount name raises loudly, naming BOTH colliding entries;
* the ``skill`` translate closure validates against its CAPTURED menu, so the
  neutral ``ControlTranslateContext`` no longer carries any menu state (D2).
"""

from __future__ import annotations

import dataclasses

import pytest

from noeta.execution.builder import _run_control_tool_mounts
from noeta.execution.control_tool import (
    ControlToolBuildContext,
    ControlToolEntry,
    ControlToolMount,
)
from noeta.policies.control_semantics import (
    ControlTranslateContext,
    make_skill_translate,
)
from noeta.protocols.decisions import Decision, StatePatchDecision
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    ToolResultBlock,
    ToolUseBlock,
)


def _empty_build_ctx() -> ControlToolBuildContext:
    """A build context with every control tool inapplicable — the fake factories
    below ignore it; the real factories would all self-gate to ``None``."""
    return ControlToolBuildContext(
        todo_write_enabled=False,
        ask_user_question_enabled=False,
        delegation_enabled=False,
        skill_invocation_enabled=False,
        workflow_enabled=False,
        subtask_agent_directory=(),
        skill_menu=(),
        skill_menu_names=frozenset(),
        structured_output_schema=None,
        exports={},
    )


def _noop_translate(_ctx: ControlTranslateContext) -> Decision | None:
    return None


def test_dual_priority_orders_diverge() -> None:
    """A mount whose schema band is low but routing band is high (and its
    inverse) lands in OPPOSITE positions in the two orders — proving the loop
    sorts the schema list and the routing specs on independent keys."""

    def mount_a(_ctx: ControlToolBuildContext) -> ControlToolMount | None:
        return ControlToolMount(
            name="a", schema={"tag": "a"}, translate=_noop_translate,
            routing_priority=500, schema_priority=100,
        )

    def mount_b(_ctx: ControlToolBuildContext) -> ControlToolMount | None:
        return ControlToolMount(
            name="b", schema={"tag": "b"}, translate=_noop_translate,
            routing_priority=100, schema_priority=500,
        )

    entries = (
        ControlToolEntry("a", 1, mount_a),
        ControlToolEntry("b", 2, mount_b),
    )
    schemas, specs = _run_control_tool_mounts(entries, _empty_build_ctx())
    assert schemas is not None
    # schema order: a (100) before b (500)
    assert [s["tag"] for s in schemas] == ["a", "b"]
    # routing order: b (100) before a (500) — the genuine inverse
    assert [sp.name for sp in specs] == ["b", "a"]


def test_self_gate_none_is_skipped() -> None:
    def yes(_ctx: ControlToolBuildContext) -> ControlToolMount | None:
        return ControlToolMount(
            name="yes", schema={"tag": "yes"}, translate=_noop_translate,
            routing_priority=100, schema_priority=100,
        )

    def no(_ctx: ControlToolBuildContext) -> ControlToolMount | None:
        return None

    entries = (
        ControlToolEntry("no", 1, no),
        ControlToolEntry("yes", 2, yes),
    )
    schemas, specs = _run_control_tool_mounts(entries, _empty_build_ctx())
    assert schemas is not None
    assert [s["tag"] for s in schemas] == ["yes"]
    assert [sp.name for sp in specs] == ["yes"]


def test_no_translate_mount_contributes_schema_but_no_routing_spec() -> None:
    """The ``structured_output`` shape: ``translate=None`` → a schema in the
    list, but excluded from the routing specs (StructuredOutputPolicy intercepts
    it, so it is never dispatched)."""

    def mount(_ctx: ControlToolBuildContext) -> ControlToolMount | None:
        return ControlToolMount(
            name="structured_output", schema={"tag": "so"}, translate=None,
            routing_priority=0, schema_priority=600,
        )

    entries = (ControlToolEntry("structured_output", 1, mount),)
    schemas, specs = _run_control_tool_mounts(entries, _empty_build_ctx())
    assert schemas is not None
    assert [s["tag"] for s in schemas] == ["so"]
    assert specs == ()


def test_no_mounts_yields_none_and_empty_specs() -> None:
    def no(_ctx: ControlToolBuildContext) -> ControlToolMount | None:
        return None

    entries = (ControlToolEntry("x", 1, no),)
    schemas, specs = _run_control_tool_mounts(entries, _empty_build_ctx())
    # An empty schema list folds to None (the composer's "no control schemas").
    assert schemas is None
    assert specs == ()


def test_duplicate_mount_name_raises_naming_both_sides() -> None:
    def first(_ctx: ControlToolBuildContext) -> ControlToolMount | None:
        return ControlToolMount(
            name="dup", schema={}, translate=_noop_translate,
            routing_priority=100, schema_priority=100,
        )

    def second(_ctx: ControlToolBuildContext) -> ControlToolMount | None:
        return ControlToolMount(
            name="dup", schema={}, translate=_noop_translate,
            routing_priority=200, schema_priority=200,
        )

    entries = (
        ControlToolEntry("first", 1, first),
        ControlToolEntry("second", 2, second),
    )
    with pytest.raises(ValueError) as exc:
        _run_control_tool_mounts(entries, _empty_build_ctx())
    msg = str(exc.value)
    assert "dup" in msg
    # Both colliding entries are named (mirrors the pack-loop collision style).
    assert "first" in msg and "second" in msg


def _skill_response(name: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(call_id="sk", tool_name="skill", arguments={"skill": name})
        ],
    )


def _skill_ctx(name: str) -> ControlTranslateContext:
    resp = _skill_response(name)
    return ControlTranslateContext(
        response=resp,
        assistant_message=Message(role="assistant", content=list(resp.content)),
        assistant_thinking=(),
        content_store=None,
    )


def test_skill_translate_closure_captures_its_menu() -> None:
    """The skill closure validates against the menu it captured, WITHOUT the
    neutral context carrying any menu field — the D2 point of the migration."""
    # The neutral context sheds its feature-named field entirely.
    field_names = {f.name for f in dataclasses.fields(ControlTranslateContext)}
    assert "skill_menu_names" not in field_names

    translate = make_skill_translate(frozenset({"alpha", "beta"}))

    # A known name activates against the captured menu.
    ok = translate(_skill_ctx("alpha"))
    assert isinstance(ok, StatePatchDecision)
    assert ok.patch is not None
    assert ok.patch.activate_skills == ["alpha"]

    # An unknown name errors against the captured menu (no context menu state).
    bad = translate(_skill_ctx("zeta"))
    assert isinstance(bad, StatePatchDecision)
    assert bad.patch is None
    block = bad.messages_after[0].content[0]
    assert isinstance(block, ToolResultBlock)
    assert "unknown skill 'zeta'" in block.output
    assert "alpha, beta" in block.output
