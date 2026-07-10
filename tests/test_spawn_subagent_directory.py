"""Tests for spawn_subagent agent-directory schema enhancement.

Covers:

1. **No-roster shape**: ``spawn_subagent_tool_schema()`` with no args / empty
   directory matches a hand-written reference exactly — the function-level
   ``description`` is present and the ``agent`` property carries
   NO ``enum`` (there is no roster to advertise).
2. **Non-empty directory**: ``enum`` present on the ``agent`` property (order
   matches input); description includes ``Available:`` with ``name — desc``
   for entries with a description, bare ``name`` for those without.
3. **SdkHost integration**: Compiled child specs whose
   ``metadata["description"]`` is non-empty cause the parent's
   ``spawn_subagent`` schema (exposed by the composer) to carry the enum +
   the description text. When all descendants have no description, the
   schema matches the no-roster reference shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


from noeta.agent.registry import AgentRegistry
from noeta.agent.spec import (
    AgentSpec,
    Capabilities,
    ComponentRef,
)
from noeta.client import AgentDefinition, Options, compile_options
from noeta.client.host import SdkHost
from noeta.policies.descriptions import load_control_tool_description
from noeta.policies._control_translate import (
    SPAWN_SUBAGENT_TOOL,
    spawn_subagent_tool_schema,
)
from noeta.protocols.messages import Usage
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider


# ---------------------------------------------------------------------------
# 1. No-roster shape — empty dir ⇔ reference schema (with function description)
# ---------------------------------------------------------------------------

_REFERENCE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SPAWN_SUBAGENT_TOOL,
        "description": load_control_tool_description("spawn_subagent"),
        "parameters": {
            "type": "object",
            "properties": {
                "spawns": {
                    "type": "array",
                    "minItems": 1,
                    "description": (
                        "The sub-agents to spawn. ONE entry delegates and "
                        "waits for that single result. SEVERAL entries fan "
                        "out and run CONCURRENTLY; their results return "
                        "together, in entry order. Always batch independent "
                        "goals into one call — spawning one entry per turn "
                        "is strictly sequential, never parallel."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent": {
                                "type": "string",
                                "description": "Named sub-agent to delegate to.",
                            },
                            "goal": {
                                "type": "string",
                                "description": (
                                    "The focused goal for this sub-agent."
                                ),
                            },
                        },
                        "required": ["agent", "goal"],
                    },
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "Run the sub-agent in the background instead of "
                        "waiting for it. With background=true you immediately "
                        "get a 'started' acknowledgement and keep working; the "
                        "sub-agent runs concurrently and its result is "
                        "delivered to you automatically when it finishes — you "
                        "never poll or wait. Use it for independent, "
                        "longer-running work (research, a broad scan) you want "
                        "off the critical path. Omit it (the default) to "
                        "delegate and wait for the result inline. Only valid "
                        "with exactly ONE spawns entry (a fan-out batch is "
                        "always foreground)."
                    ),
                },
            },
            "required": ["spawns"],
        },
    },
}


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _agent_prop_of(schema: dict[str, Any]) -> dict[str, Any]:
    """The roster-annotated ``agent`` property — nested per spawn entry under
    ``spawns.items`` in the batch-form schema."""
    parameters = schema["function"]["parameters"]
    return parameters["properties"]["spawns"]["items"]["properties"]["agent"]


def test_empty_dir_matches_reference_no_arg() -> None:
    """No-arg call must match the hand-written reference schema exactly."""
    got = spawn_subagent_tool_schema()
    assert _canonical(got) == _canonical(_REFERENCE_SCHEMA), (
        f"schema drift:\n  got={_canonical(got)}\n  exp={_canonical(_REFERENCE_SCHEMA)}"
    )


def test_empty_dir_matches_reference_explicit_empty() -> None:
    """Explicit empty-tuple arg must also match the reference schema exactly."""
    got = spawn_subagent_tool_schema(())
    assert _canonical(got) == _canonical(_REFERENCE_SCHEMA)


# ---------------------------------------------------------------------------
# 2. Non-empty directory — enum + description rendering
# ---------------------------------------------------------------------------


def test_nonempty_dir_enum_and_description_full() -> None:
    """Every entry has a description → enum present, all "name — desc" in desc."""
    directory = (
        ("coder", "Writes Python code"),
        ("reviewer", "Finds bugs in code"),
    )
    schema = spawn_subagent_tool_schema(directory)
    agent_prop = _agent_prop_of(schema)
    # enum order == input order (sorted by caller before passing in)
    assert agent_prop["enum"] == ["coder", "reviewer"]
    desc = agent_prop["description"]
    assert desc.startswith("Named sub-agent to delegate to.")
    assert "Available: " in desc
    roster = desc.split("Available: ", 1)[1]
    assert "coder — Writes Python code" in roster
    assert "reviewer — Finds bugs in code" in roster
    # separator between entries
    assert "; " in roster


def test_nonempty_dir_mixed_descriptions_bare_name_entries() -> None:
    """Entries with empty description should render as bare name (no "— ")."""
    directory = (
        ("named", "Has a description"),
        ("anonymous", ""),
        ("tagged", "Also has one"),
    )
    schema = spawn_subagent_tool_schema(directory)
    agent_prop = _agent_prop_of(schema)
    assert agent_prop["enum"] == ["named", "anonymous", "tagged"]
    desc = agent_prop["description"]
    roster = desc.split("Available: ", 1)[1]
    assert "named — Has a description" in roster
    assert "anonymous" in roster
    # anonymous must NOT be followed by " — "
    assert "anonymous — " not in roster
    assert "tagged — Also has one" in roster


def test_nonempty_dir_all_empty_descriptions_still_renders_enum() -> None:
    """If call site passes the directory, even all-empty-desc entries get enum.

    (The SdkHost call site deliberately withholds the directory in this case;
    this test documents the low-level function's behaviour independently.)
    """
    directory = (("a", ""), ("b", ""))
    schema = spawn_subagent_tool_schema(directory)
    agent_prop = _agent_prop_of(schema)
    assert agent_prop.get("enum") == ["a", "b"]
    desc = agent_prop["description"]
    roster = desc.split("Available: ", 1)[1]
    # Bare names joined by "; "
    assert roster == "a; b"


# ---------------------------------------------------------------------------
# 3. SdkHost integration — metadata description flows to the composer schema
# ---------------------------------------------------------------------------


def _stub_provider() -> FakeLLMProvider:
    """Provider that returns a single end_turn so _build_engine runs without
    needing a full driver loop (we only inspect the composer, not drive turns).
    """
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


def _make_host(
    registry: AgentRegistry, tmp_path: Path
) -> SdkHost:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    ws = tmp_path / "ws"
    ws.mkdir()
    return SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=_stub_provider(),
        model="stub-model",
        workspace_dir=ws,
        registry=registry,
    )


def _spawn_schema_from_engine_composer(engine: Any) -> dict[str, Any] | None:
    """Extract the spawn_subagent schema from an engine's composer (if any)."""
    # We need to ask the composer to produce a View and scan its provider_tool_schemas.
    # Simpler: reach into the composer's control_action_schemas list.
    composer = engine._composer
    for s in composer._control_action_schemas:
        if isinstance(s, dict) and s.get("function", {}).get("name") == SPAWN_SUBAGENT_TOOL:
            return s
    return None


def test_sdkhost_description_flows_to_schema(tmp_path: Path) -> None:
    """Compile Options with a named child agent whose description is set; the
    parent engine's spawn_subagent schema must carry the enum + description.
    """
    opts = Options(
        system_prompt="You are the parent agent.",
        name="main",
        allowed_tools=("read",),
        agents={
            "helper": AgentDefinition(
                description="search code",
                prompt="you search",
                tools=("read",),
            ),
        },
    )
    main_spec, child_specs = compile_options(opts)
    # Sanity: description metadata is on the child
    helper_spec = next(s for s in child_specs if s.name == "helper")
    assert helper_spec.metadata.get("description") == "search code"

    registry = AgentRegistry()
    registry.add(main_spec)
    registry.add(helper_spec)

    host = _make_host(registry, tmp_path)
    engine = host._build_engine(
        main_spec,
        "stub-model",
        delegation_enabled=True,
        allowed_subtask_agents=frozenset({"helper"}),
        ask_user_question_enabled=False,
        policy_wrapper=None,
    )

    schema = _spawn_schema_from_engine_composer(engine)
    assert schema is not None, "spawn_subagent schema missing from composer"
    agent_prop = _agent_prop_of(schema)
    assert agent_prop.get("enum") == ["helper"]
    desc = agent_prop["description"]
    assert "Available: " in desc
    assert "helper — search code" in desc


def test_sdkhost_no_descriptions_keeps_reference_shape(tmp_path: Path) -> None:
    """When NO child has a non-empty metadata description, the directory is
    intentionally withheld → schema matches the no-roster reference shape.

    We build this by hand (compile_options rejects empty descriptions for the
    agents= dict path, which is correct user-facing behaviour; internally a
    spec can carry metadata without description).
    """
    parent = AgentSpec(
        name="main",
        instructions="parent",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=(),
        capabilities=Capabilities(delegation=True, spawnable=("child_a", "child_b")),
        metadata={},
    )
    child_a = AgentSpec(
        name="child_a",
        instructions="a",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=(),
        capabilities=Capabilities(),
        metadata={},  # no description key
    )
    child_b = AgentSpec(
        name="child_b",
        instructions="b",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=(),
        capabilities=Capabilities(),
        metadata={"description": ""},  # explicit empty
    )
    registry = AgentRegistry()
    registry.add(parent)
    registry.add(child_a)
    registry.add(child_b)

    host = _make_host(registry, tmp_path)
    engine = host._build_engine(
        parent,
        "stub-model",
        delegation_enabled=True,
        allowed_subtask_agents=frozenset({"child_a", "child_b"}),
        ask_user_question_enabled=False,
        policy_wrapper=None,
    )

    schema = _spawn_schema_from_engine_composer(engine)
    assert schema is not None
    # All descriptions absent or empty → matches the no-roster reference shape
    assert _canonical(schema) == _canonical(_REFERENCE_SCHEMA)


def test_sdkhost_some_descriptions_mixed(tmp_path: Path) -> None:
    """One child has a description, another doesn't → directory rendered;
    the bare-name child shows up without " — ", the described one does.
    """
    parent = AgentSpec(
        name="main",
        instructions="parent",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=(),
        capabilities=Capabilities(
            delegation=True, spawnable=("alpha", "beta")
        ),
        metadata={},
    )
    alpha = AgentSpec(
        name="alpha",
        instructions="a",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=(),
        metadata={"description": "does alpha things"},
    )
    beta = AgentSpec(
        name="beta",
        instructions="b",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=(),
        metadata={},  # no description
    )
    registry = AgentRegistry()
    registry.add(parent)
    registry.add(alpha)
    registry.add(beta)

    host = _make_host(registry, tmp_path)
    engine = host._build_engine(
        parent,
        "stub-model",
        delegation_enabled=True,
        allowed_subtask_agents=frozenset({"alpha", "beta"}),
        ask_user_question_enabled=False,
        policy_wrapper=None,
    )

    schema = _spawn_schema_from_engine_composer(engine)
    assert schema is not None
    agent_prop = _agent_prop_of(schema)
    # allowed_subtask_agents frozenset → sorted iteration in SdkHost
    assert agent_prop["enum"] == ["alpha", "beta"]
    desc = agent_prop["description"]
    assert "alpha — does alpha things" in desc
    # beta is bare (no description)
    roster = desc.split("Available: ", 1)[1]
    # "beta" appears without a following " — "
    assert "beta" in roster
    assert "beta — " not in roster


def test_sdkhost_delegation_disabled_no_directory_leak(tmp_path: Path) -> None:
    """delegation_enabled=False → no spawn_subagent schema at all, regardless
    of directory availability. (Regression guard against a future refactor.)
    """
    parent = AgentSpec(
        name="main",
        instructions="parent",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=(),
        capabilities=Capabilities(),  # delegation False
        metadata={},
    )
    child = AgentSpec(
        name="helper",
        instructions="h",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=(),
        metadata={"description": "does things"},
    )
    registry = AgentRegistry()
    registry.add(parent)
    registry.add(child)

    host = _make_host(registry, tmp_path)
    engine = host._build_engine(
        parent,
        "stub-model",
        delegation_enabled=False,
        allowed_subtask_agents=frozenset({"helper"}),
        ask_user_question_enabled=False,
        policy_wrapper=None,
    )

    schema = _spawn_schema_from_engine_composer(engine)
    assert schema is None, (
        "spawn_subagent schema must be absent when delegation disabled"
    )


def test_sdkhost_unknown_agent_in_roster_is_skipped(tmp_path: Path) -> None:
    """allowed_subtask_agents contains a name not in the registry → silently
    skipped; the directory still renders correctly for the rest.
    """
    parent = AgentSpec(
        name="main",
        instructions="parent",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=(),
        capabilities=Capabilities(delegation=True, spawnable=("ok",)),
        metadata={},
    )
    ok_spec = AgentSpec(
        name="ok",
        instructions="ok",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=(),
        metadata={"description": "I exist"},
    )
    registry = AgentRegistry()
    registry.add(parent)
    registry.add(ok_spec)

    host = _make_host(registry, tmp_path)
    engine = host._build_engine(
        parent,
        "stub-model",
        delegation_enabled=True,
        # "ghost" is declared but absent from registry → resolve() raises
        allowed_subtask_agents=frozenset({"ok", "ghost"}),
        ask_user_question_enabled=False,
        policy_wrapper=None,
    )

    schema = _spawn_schema_from_engine_composer(engine)
    assert schema is not None
    agent_prop = _agent_prop_of(schema)
    # ghost excluded (not in registry), ok included
    assert agent_prop["enum"] == ["ok"]
    assert "ghost" not in agent_prop["description"]
    assert "ok — I exist" in agent_prop["description"]
