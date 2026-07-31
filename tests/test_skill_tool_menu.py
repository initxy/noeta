"""The ``skill`` tool's schema is the model's only view of what skills exist.

It appears exactly when the capability is activated AND the workspace holds a
skill, so a workspace without skills causes zero schema drift. The enum is
sorted by name regardless of the order files landed on disk, which keeps the
schema bytes stable across machines and preserves the provider's prompt cache.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


from tests._skill_fixtures import write_skill

from tests._session_inputs import default_factory_kwargs
from noeta.agent.registry import AgentRegistry
from noeta.agent.spec import (
    AgentSpec,
    BudgetSpec,
    ComponentRef,
)
from noeta.client.host import SdkHost
from noeta.execution.builder import (
    COMPACTION_OFF,
    build_session_inputs,
)
from noeta.runtime.governance import Budget
from noeta.builtins.skills.impl import SKILL_TOOL, skill_tool_schema
from noeta.protocols.messages import Usage
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode


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
    # An empty enum is invalid in several provider schemas, so it is omitted.
    assert "enum" not in prop
    assert prop["description"] == "Name of the skill to activate."


def test_skill_schema_no_args_no_reason() -> None:
    """The tool takes a name and nothing else: extra parameters would invite
    the model to pass state the activation path has nowhere to put."""
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
    """The schema feeds a hashed prompt prefix, so identical input must give
    byte-identical output."""
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
        **default_factory_kwargs(),
        workspace_dir=ws,
        system_prompt="you are helpful",
        allowed_tools=frozenset({"read"}),
        content_store=content_store,
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        capability_flags={"skill_invocation": skill_invocation_enabled},
        # The fs write/shell knobs ride plugin_config under the plugin's name.
        plugin_config={
            "fs": {"write_mode": FsWriteMode.DRY_RUN, "shell_mode": ShellMode.OFF},
        },
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
    schemas = _build_composer_schemas(ws, skill_invocation_enabled=True)
    assert _find_skill_schema(schemas) is None


def test_flag_on_with_skills_renders_sorted_menu(tmp_path: Path) -> None:
    """Registry non-empty + flag on → enum sorted by name, descriptions present."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # Written out of order on purpose so the sort has something to do.
    write_skill(ws, "zeta", "last letter")
    write_skill(ws, "alpha", "first letter")
    write_skill(ws, "beta", "")  # empty description
    schemas = _build_composer_schemas(ws, skill_invocation_enabled=True)
    schema = _find_skill_schema(schemas)
    assert schema is not None
    prop = schema["function"]["parameters"]["properties"]["skill"]
    assert prop["enum"] == ["alpha", "beta", "zeta"]
    desc = prop["description"]
    assert "alpha — first letter" in desc
    assert "zeta — last letter" in desc
    assert "beta" in desc
    assert "beta — " not in desc


def test_menu_built_from_registry_not_caller(tmp_path: Path) -> None:
    """The menu derives from the loaded registry, never from a caller
    argument, and the flag rides the generic ``capability_flags`` bag — the
    builder's signature must stay free of skill-specific vocabulary or the
    kernel starts knowing what a skill is."""
    import inspect

    sig = inspect.signature(build_session_inputs)
    assert "skill_menu" not in sig.parameters
    assert "skill_invocation_enabled" not in sig.parameters
    assert "capability_flags" in sig.parameters


# ---------------------------------------------------------------------------
# SdkHost — the spec's "skill_invocation" activation drives the flag
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
        plugins=("skill_invocation",) if skill_invocation else (),
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
    """The activation on the AgentSpec reaches the composer schema through
    ``_build_engine``, so a spec is enough to configure the menu."""
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
    assert "reviewer" in prop["description"]
    assert "reviewer — " not in prop["description"]


def test_sdkhost_capability_off_masks_schema_even_with_skills(
    tmp_path: Path,
) -> None:
    """Without the activation the schema stays absent even though skills are
    on disk — the workspace's contents must never override the spec."""
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
