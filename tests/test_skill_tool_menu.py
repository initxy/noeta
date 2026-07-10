"""Tests for ``skill`` control-tool schema + workspace-skill menu.

Covers:

1. **Flag off → schema absent.** ``skill_invocation_enabled=False`` → composer
   does not expose a ``skill`` tool, regardless of workspace skills.
2. **Empty registry → schema absent.** Flag on but workspace has no skills →
   no ``skill`` tool grown (pure-SDK users zero-perceived).
3. **Non-empty registry — enum + description.** Sorted ``(name, description)``
   pairs from the skill registry drive the ``skill`` property ``enum`` and
   description roster. Entries with no description render as bare names.
4. **Sorting determinism.** Input skill-name ordering on disk does not affect
   the enum order (always sorted by name).
5. **SdkHost integration.** ``spec.capabilities.skill_invocation=True`` flows
   through ``_build_engine`` into the composer schema when the workspace has
   skills; ``False`` keeps it absent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


from tests._skill_fixtures import write_skill

from noeta.agent.registry import AgentRegistry
from noeta.agent.spec import (
    AgentSpec,
    BudgetSpec,
    Capabilities,
    ComponentRef,
)
from noeta.client.host import SdkHost
from noeta.execution.builder import (
    COMPACTION_OFF,
    build_session_inputs,
)
from noeta.guards.budget import Budget
from noeta.policies.control_tools import SKILL_TOOL, skill_tool_schema
from noeta.protocols.messages import Usage
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode


# ---------------------------------------------------------------------------
# Pure-schema tests — skill_tool_schema()
# ---------------------------------------------------------------------------


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def test_skill_schema_empty_menu_shape() -> None:
    """Empty menu still produces the one-parameter shape (single ``skill``)."""
    schema = skill_tool_schema(())
    assert schema["type"] == "function"
    assert schema["function"]["name"] == SKILL_TOOL
    params = schema["function"]["parameters"]
    assert params["required"] == ["skill"]
    prop = params["properties"]["skill"]
    assert prop["type"] == "string"
    # No enum when the menu is empty
    assert "enum" not in prop
    assert prop["description"] == "Name of the skill to activate."


def test_skill_schema_no_args_no_reason() -> None:
    """Per D4: parameters has ONLY ``skill`` — no ``args``, no ``reason``."""
    schema = skill_tool_schema((("alpha", "does things"),))
    props = schema["function"]["parameters"]["properties"]
    assert set(props.keys()) == {"skill"}


def test_skill_schema_nonempty_menu_enum_and_desc() -> None:
    """Menu entries populate the ``enum`` and append roster to description."""
    menu = (
        ("coder", "Writes Python code"),
        ("reviewer", "Finds bugs in code"),
    )
    schema = skill_tool_schema(menu)
    prop = schema["function"]["parameters"]["properties"]["skill"]
    assert prop["enum"] == ["coder", "reviewer"]
    desc = prop["description"]
    assert desc.startswith("Name of the skill to activate.")
    assert "Available: " in desc
    roster = desc.split("Available: ", 1)[1]
    assert "coder — Writes Python code" in roster
    assert "reviewer — Finds bugs in code" in roster


def test_skill_schema_bare_name_when_description_empty() -> None:
    """Skill with empty description renders as bare name (no `` — `` tail)."""
    menu = (
        ("named", "Has description"),
        ("anonymous", ""),
    )
    schema = skill_tool_schema(menu)
    prop = schema["function"]["parameters"]["properties"]["skill"]
    assert prop["enum"] == ["named", "anonymous"]
    roster = prop["description"].split("Available: ", 1)[1]
    assert "named — Has description" in roster
    assert "anonymous" in roster
    assert "anonymous — " not in roster


def test_skill_schema_deterministic_bytes_for_same_input() -> None:
    """Same menu → canonical bytes identical (stable-hash guard)."""
    menu = (("a", "first"), ("b", "second"))
    assert _canonical(skill_tool_schema(menu)) == _canonical(
        skill_tool_schema(menu)
    )


# ---------------------------------------------------------------------------
# build_session_inputs integration — flag on/off, empty/non-empty registry
# ---------------------------------------------------------------------------


def _build_composer_schemas(
    ws: Path, *, skill_invocation_enabled: bool
) -> list[dict[str, Any]]:
    """Call ``build_session_inputs`` and return composer control schemas."""
    content_store = InMemoryContentStore()
    inputs = build_session_inputs(
        workspace_dir=ws,
        system_prompt="you are helpful",
        allowed_tools=frozenset({"read"}),
        content_store=content_store,
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        skill_invocation_enabled=skill_invocation_enabled,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
    )
    return list(inputs.composer._control_action_schemas)


def _find_skill_schema(schemas: list[dict[str, Any]]) -> dict[str, Any] | None:
    for s in schemas:
        if (
            isinstance(s, dict)
            and s.get("function", {}).get("name") == SKILL_TOOL
        ):
            return s
    return None


def test_flag_off_no_skill_schema_even_with_skills(tmp_path: Path) -> None:
    """Flag disabled → skill tool absent from composer, even with skills."""
    ws = tmp_path / "ws"
    ws.mkdir()
    write_skill(ws, "coder", "Writes code")
    schemas = _build_composer_schemas(ws, skill_invocation_enabled=False)
    assert _find_skill_schema(schemas) is None


def test_flag_on_empty_registry_no_skill_schema(tmp_path: Path) -> None:
    """Flag on but no skills on disk → skill tool absent."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # No .noeta/skills at all → registry is empty
    schemas = _build_composer_schemas(ws, skill_invocation_enabled=True)
    assert _find_skill_schema(schemas) is None


def test_flag_on_with_skills_renders_sorted_menu(tmp_path: Path) -> None:
    """Registry non-empty + flag on → enum sorted by name, descriptions present."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # Write in an intentionally non-sorted order on disk
    write_skill(ws, "zeta", "last letter")
    write_skill(ws, "alpha", "first letter")
    write_skill(ws, "beta", "")  # empty description
    schemas = _build_composer_schemas(ws, skill_invocation_enabled=True)
    schema = _find_skill_schema(schemas)
    assert schema is not None
    prop = schema["function"]["parameters"]["properties"]["skill"]
    # Enum must be alphabetically sorted regardless of disk write order
    assert prop["enum"] == ["alpha", "beta", "zeta"]
    desc = prop["description"]
    assert "alpha — first letter" in desc
    assert "zeta — last letter" in desc
    # Bare-name beta, must NOT be followed by " — "
    assert "beta" in desc
    assert "beta — " not in desc


def test_menu_built_from_registry_not_caller(tmp_path: Path) -> None:
    """Builder internally derives the menu from the loaded registry — callers
    never supply a menu arg. (Regression guard: no ``skill_menu`` kwarg on
    ``build_session_inputs`` exists per design.)
    """
    import inspect

    sig = inspect.signature(build_session_inputs)
    assert "skill_menu" not in sig.parameters
    assert "skill_invocation_enabled" in sig.parameters


# ---------------------------------------------------------------------------
# SdkHost integration — spec.capabilities.skill_invocation drives the flag
# ---------------------------------------------------------------------------


def _stub_provider() -> FakeLLMProvider:
    from noeta.protocols.messages import LLMResponse, TextBlock

    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="ok")],
                usage=Usage(uncached=1, output=1),
                raw={"id": "r1"},
            )
        ]
    )


def _make_host(workspace: Path) -> SdkHost:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    return SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=_stub_provider(),
        model="stub-model",
        workspace_dir=workspace,
        registry=AgentRegistry(),
    )


def _spec(skill_invocation: bool) -> AgentSpec:
    return AgentSpec(
        name="main",
        instructions="you are helpful",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=(),
        default_budget=BudgetSpec(),
        capabilities=Capabilities(skill_invocation=skill_invocation),
        metadata={},
    )


def _skill_schema_from_engine(engine: Any) -> dict[str, Any] | None:
    composer = engine._composer
    for s in composer._control_action_schemas:
        if (
            isinstance(s, dict)
            and s.get("function", {}).get("name") == SKILL_TOOL
        ):
            return s
    return None


def test_sdkhost_capability_on_preserves_schema_with_skills(
    tmp_path: Path,
) -> None:
    """spec.capabilities.skill_invocation=True + workspace skills → schema."""
    ws = tmp_path / "ws"
    ws.mkdir()
    write_skill(ws, "coder", "Writes code")
    write_skill(ws, "reviewer", "")

    host = _make_host(ws)
    spec = _spec(skill_invocation=True)
    host.registry.add(spec)
    engine = host._build_engine(
        spec,
        "stub-model",
        delegation_enabled=False,
        allowed_subtask_agents=frozenset(),
        ask_user_question_enabled=False,
        policy_wrapper=None,
    )

    schema = _skill_schema_from_engine(engine)
    assert schema is not None, "skill schema missing when capability is on"
    prop = schema["function"]["parameters"]["properties"]["skill"]
    assert prop["enum"] == ["coder", "reviewer"]
    assert "coder — Writes code" in prop["description"]
    # reviewer with empty description → bare name
    assert "reviewer" in prop["description"]
    assert "reviewer — " not in prop["description"]


def test_sdkhost_capability_off_masks_schema_even_with_skills(
    tmp_path: Path,
) -> None:
    """spec.capabilities.skill_invocation=False → schema absent (no leak)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    write_skill(ws, "coder", "Writes code")

    host = _make_host(ws)
    spec = _spec(skill_invocation=False)
    host.registry.add(spec)
    engine = host._build_engine(
        spec,
        "stub-model",
        delegation_enabled=False,
        allowed_subtask_agents=frozenset(),
        ask_user_question_enabled=False,
        policy_wrapper=None,
    )

    assert _skill_schema_from_engine(engine) is None
