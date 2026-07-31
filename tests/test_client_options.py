"""``compile_options`` — the pure ``Options`` → ``AgentSpec`` compile.

Agent identity is structural equality of the compiled spec, so this module pins
which ``Options`` fields feed identity (prompt, tools, skills, budget, plugins,
child agents) and which are pure wiring that must never perturb it (``cwd``,
``permission_mode``, ``output_schema``, ``thinking``, ``effort``). It also
guards the SDK parts table against declaring a different ``ToolRef`` than
:func:`noeta.presets.official_specs` does for the same built-in name — two
tables disagreeing about one tool is invisible until a fingerprint moves.
"""

from __future__ import annotations

import dataclasses

import pytest

from noeta.agent.spec import BudgetSpec, ComponentRef, ToolRef, agent_activates
from noeta.client import (
    AgentDefinition,
    Options,
    SystemPromptPreset,
    builtin_tool_ref,
    compile_options,
    register_preset_prompt,
)
from noeta.client.options import DEFAULT_PLUGINS
from noeta.client.parts import COMPOSER_REF, POLICY_REF, builtin_tool_classes
from noeta.presets import official_specs
from noeta.tools.decorator import tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "properties": {"x": {"type": "string"}},
    "required": ["x"],
    "additionalProperties": False,
}


@tool(name="my_tool", version="2", risk_level="medium", input_schema=_SCHEMA)
def my_tool(arguments, ctx):  # pragma: no cover — identity-only test
    raise NotImplementedError


def _base_options() -> Options:
    return Options(
        system_prompt="You are a test agent.",
        name="main",
        allowed_tools=("read", my_tool),
        skills=("search", "plan"),
        budget=BudgetSpec(max_iterations=42),
        plugins=DEFAULT_PLUGINS + ("todo_write",),
        metadata={"owner": "tester"},
        model="claude-sonnet-4-5",
    )


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------

def test_purity_equal_inputs_equal_spec() -> None:
    opts = _base_options()
    main_a, kids_a = compile_options(opts)
    main_b, kids_b = compile_options(opts)
    assert main_a == main_b
    assert kids_a == kids_b


# ---------------------------------------------------------------------------
# Substantive fields → spec identity changes
# ---------------------------------------------------------------------------

def test_substantive_system_prompt_changes_identity() -> None:
    base, _ = compile_options(_base_options())
    mutated, _ = compile_options(dataclasses.replace(_base_options(), system_prompt="DIFFERENT"))
    assert base != mutated


def test_substantive_tools_changes_identity() -> None:
    base, _ = compile_options(_base_options())
    mutated, _ = compile_options(dataclasses.replace(_base_options(), allowed_tools=("read", my_tool, "glob")))
    assert base != mutated


def test_substantive_skills_changes_identity() -> None:
    base, _ = compile_options(_base_options())
    mutated, _ = compile_options(dataclasses.replace(_base_options(), skills=("search",)))
    assert base != mutated


def test_substantive_budget_changes_identity() -> None:
    base, _ = compile_options(_base_options())
    mutated, _ = compile_options(
        dataclasses.replace(_base_options(), budget=BudgetSpec(max_iterations=999))
    )
    assert base != mutated


def test_substantive_activation_changes_identity() -> None:
    """Dropping a feature-bundle activation changes the compiled identity."""
    base, _ = compile_options(_base_options())  # todo_write active
    mutated, _ = compile_options(
        dataclasses.replace(_base_options(), plugins=DEFAULT_PLUGINS)
    )
    assert base != mutated


def test_substantive_subagents_name_changes_identity() -> None:
    # Child names live in the `agents` dict key, so swapping the key changes
    # spawnable contents and therefore the parent identity.
    child_a = AgentDefinition(description="sub agent", prompt="sub prompt")
    child_b = AgentDefinition(description="sub agent", prompt="sub prompt")
    base, _ = compile_options(dataclasses.replace(_base_options(), agents={"child_a": child_a}))
    mutated, _ = compile_options(dataclasses.replace(_base_options(), agents={"child_b": child_b}))
    assert base != mutated


# ---------------------------------------------------------------------------
# Mixed tools: DecoratedTool + builtin-name string
# ---------------------------------------------------------------------------

def test_mixed_tools_refs_correct_and_cross_package_consistent() -> None:
    main, _ = compile_options(_base_options())

    # AgentSpec normalises tool order, so compare as a set.
    assert {t.name for t in main.tools} == {"my_tool", "read"}

    my_ref = next(t for t in main.tools if t.name == "my_tool")
    read_ref = next(t for t in main.tools if t.name == "read")

    assert my_ref == my_tool.ref
    assert my_ref == ToolRef(name="my_tool", version="2", risk_level="medium")

    # Both tables must declare the same ref for a shared built-in name; drift
    # here goes unnoticed until an agent fingerprint moves.
    roster_spec = official_specs()["main"]
    roster_read = next(t for t in roster_spec.tools if t.name == "read")
    assert read_ref == roster_read


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_unknown_builtin_name_raises_keyerror() -> None:
    with pytest.raises(KeyError) as exc:
        compile_options(dataclasses.replace(_base_options(), allowed_tools=("not_a_tool",)))
    msg = str(exc.value)
    assert "not_a_tool" in msg
    # Enumeration must include every builtin (spot-check two representatives).
    assert "read" in msg
    assert "shell_run" in msg


def test_illegal_tool_entry_raises_typeerror() -> None:
    with pytest.raises(TypeError) as exc:
        compile_options(dataclasses.replace(_base_options(), allowed_tools=(42,)))  # type: ignore[arg-type]
    assert "int" in str(exc.value)


# ---------------------------------------------------------------------------
# Child agents (flat `agents` dict): compilation, activation, determinism
# ---------------------------------------------------------------------------


def test_child_agents_flat_and_capabilities_set() -> None:
    opts = Options(
        system_prompt="parent",
        name="parent",
        agents={
            "child_a": AgentDefinition(description="child a", prompt="child a prompt"),
            "child_b": AgentDefinition(description="child b", prompt="child b prompt"),
        },
    )
    main, descendants = compile_options(opts)

    assert {s.name for s in descendants} == {"child_a", "child_b"}
    assert agent_activates(main, "delegation") is True
    assert tuple(main.spawnable) == ("child_a", "child_b")

    # Children are flat leaves: they never delegate and never spawn.
    for s in descendants:
        assert agent_activates(s, "delegation") is False
        assert tuple(s.spawnable) == ()


def test_child_agents_name_collides_with_root_raises_valueerror() -> None:
    """A child sharing the main agent's name fails at compile time, not later
    at Client registration."""
    defn = AgentDefinition(description="dup of root", prompt="child prompt")
    with pytest.raises(ValueError, match="main"):
        compile_options(
            Options(name="main", system_prompt="root prompt", agents={"main": defn}))


def test_child_agent_identity_deterministic() -> None:
    parent = Options(
        system_prompt="parent",
        name="parent",
        agents={"child": AgentDefinition(description="child", prompt="child prompt")},
    )
    _, kids_a = compile_options(parent)
    _, kids_b = compile_options(parent)
    child_a = next(s for s in kids_a if s.name == "child")
    child_b = next(s for s in kids_b if s.name == "child")
    assert child_a == child_b


# ---------------------------------------------------------------------------
# Policy / composer ref alignment
# ---------------------------------------------------------------------------

def test_policy_and_composer_match_roster_constants() -> None:
    main, _ = compile_options(_base_options())
    roster_spec = official_specs()["main"]
    assert main.policy == roster_spec.policy
    assert main.composer == roster_spec.composer
    assert main.policy == POLICY_REF
    assert main.composer == COMPOSER_REF


# ---------------------------------------------------------------------------
# builtin_tool_ref surface + builtin_tool_classes() inventory
# ---------------------------------------------------------------------------

def test_builtin_tool_ref_inventory_complete() -> None:
    # The catalogue is the set of addressable built-in names, which is what
    # ``tools=None`` expands to — so membership is a behaviour, not a detail.
    expected_names = {
        # fs read
        "read", "glob", "grep",
        # fs edit
        "edit", "write", "apply_patch",
        # fs shell — the background triplet is catalogued together so
        # ``tools=None`` can whitelist all three at once.
        "shell_run", "shell_poll", "shell_kill",
        # web — not an fs tool, but catalogued so ``tools=None`` includes it.
        "webfetch",
        # web_search is only built when NOETA_WEB_SEARCH_API_KEY is set; the
        # catalogue entry is the addressable name, gated at build time.
        "web_search",
    }
    assert set(builtin_tool_classes()) == expected_names
    for name in expected_names:
        ref = builtin_tool_ref(name)
        assert isinstance(ref, ToolRef)
        assert ref.name == name
        assert ref.version == "1"


def test_skills_become_component_refs_with_default_version() -> None:
    main, _ = compile_options(_base_options())
    assert {s.name for s in main.skills} == {"plan", "search"}
    for s in main.skills:
        assert isinstance(s, ComponentRef)
        assert s.version == "1"  # SDK convention: version-less name => "1"


# ---------------------------------------------------------------------------
# Flat `agents` dict compilation
# ---------------------------------------------------------------------------


def test_agents_dict_produces_child_spec_with_description_metadata() -> None:
    defn = AgentDefinition(
        description="A researcher that finds facts.",
        prompt="You are a researcher.",
    )
    opts = Options(
        system_prompt="You are main.",
        name="main",
        agents={"researcher": defn},
    )
    main, descendants = compile_options(opts)

    assert len(descendants) == 1
    child = descendants[0]
    assert child.name == "researcher"
    assert child.instructions == "You are a researcher."
    assert child.metadata.get("description") == "A researcher that finds facts."
    # Runaway-recursion guard, installed on every child.
    assert child.default_budget.max_subtask_depth == 3
    assert agent_activates(child, "delegation") is False
    assert tuple(child.spawnable) == ()
    assert tuple(main.spawnable) == ("researcher",)
    assert agent_activates(main, "delegation") is True


def test_agents_dict_child_metadata_merges_under_description() -> None:
    # AgentDefinition.metadata merges into the child spec's metadata
    # (description always wins its key), so a preset can ship
    # write_path_globs as a host-binding hint.
    defn = AgentDefinition(
        description="A planner.",
        prompt="You plan.",
        metadata={"write_path_globs": "plans/*.md"},
    )
    opts = Options(
        system_prompt="You are main.",
        name="main",
        agents={"planner": defn},
    )
    _, descendants = compile_options(opts)
    child = descendants[0]
    assert child.metadata.get("description") == "A planner."
    assert child.metadata.get("write_path_globs") == "plans/*.md"


def test_agents_dict_child_metadata_cannot_clobber_description() -> None:
    # description is recipe-owned: an attempt to override it via metadata loses.
    defn = AgentDefinition(
        description="Real description.",
        prompt="p",
        metadata={"description": "sneaky override"},
    )
    _, descendants = compile_options(
        Options(system_prompt="root", name="main", agents={"kid": defn})
    )
    assert descendants[0].metadata.get("description") == "Real description."


def test_agents_dict_child_duplicates_root_name_raises_valueerror() -> None:
    defn = AgentDefinition(description="dup", prompt="p")
    with pytest.raises(ValueError, match="main"):
        compile_options(
            Options(
                system_prompt="root",
                name="main",
                agents={"main": defn},
            )
        )


def test_agents_dict_empty_description_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="description"):
        compile_options(
            Options(
                system_prompt="root",
                name="main",
                agents={"child": AgentDefinition(description="", prompt="p")},
            )
        )
    with pytest.raises(ValueError, match="description"):
        compile_options(
            Options(
                system_prompt="root",
                name="main",
                agents={"child": AgentDefinition(description="   \t  ", prompt="p")},
            )
        )


def test_agents_dict_distinct_children_compile_cleanly() -> None:
    defn_a = AgentDefinition(description="a", prompt="pa")
    defn_b = AgentDefinition(description="b", prompt="pb")
    opts = Options(
        system_prompt="root",
        name="main",
        agents={"a": defn_a, "b": defn_b},
    )
    main, kids = compile_options(opts)
    assert {s.name for s in kids} == {"a", "b"}
    assert tuple(sorted(main.spawnable)) == ("a", "b")


def test_agents_child_model_passes_through() -> None:
    defn = AgentDefinition(
        description="d",
        prompt="p",
        model="claude-opus-7",
    )
    _, kids = compile_options(
        Options(system_prompt="root", name="main", agents={"c": defn})
    )
    assert kids[0].default_model == "claude-opus-7"


def test_agents_child_tools_default_to_full_builtin_set() -> None:
    defn = AgentDefinition(description="d", prompt="p")  # tools=None
    _, kids = compile_options(
        Options(system_prompt="root", name="main", agents={"c": defn})
    )
    assert {t.name for t in kids[0].tools} == set(builtin_tool_classes())


def test_agents_child_tools_explicit_list() -> None:
    defn = AgentDefinition(
        description="d",
        prompt="p",
        tools=("read", "glob"),
    )
    _, kids = compile_options(
        Options(system_prompt="root", name="main", agents={"c": defn})
    )
    assert {t.name for t in kids[0].tools} == {"read", "glob"}


# ---------------------------------------------------------------------------
# allowed_tools / disallowed_tools
# ---------------------------------------------------------------------------


def test_bare_options_defaults_to_all_builtin_tools() -> None:
    """Omitting ``allowed_tools`` mounts the full built-in set."""
    main, _ = compile_options(Options(system_prompt="hi", name="main"))
    assert {t.name for t in main.tools} == set(builtin_tool_classes())


def test_builtin_tool_whitelist_is_pinned() -> None:
    """``docs/tutorials/first-agent.md`` quotes this count in prose (11 names,
    of which 10 mount without extra configuration — ``web_search`` needs an API
    key). Pinning the set forces any change to come back and update the doc."""
    assert set(builtin_tool_classes()) == {
        "apply_patch",
        "edit",
        "glob",
        "grep",
        "read",
        "shell_kill",
        "shell_poll",
        "shell_run",
        "web_search",
        "webfetch",
        "write",
    }
    # Everything else — memory, browser, open_app, run_skill_script — is
    # capability- or host-injection-gated, never part of this whitelist.
    assert len(builtin_tool_classes()) == 11


def test_allowed_tools_explicit_list() -> None:
    main, _ = compile_options(
        Options(
            system_prompt="hi",
            allowed_tools=("read", "glob", my_tool),
        )
    )
    assert {t.name for t in main.tools} == {"read", "glob", "my_tool"}


def test_allowed_tools_empty_tuple_means_no_tools() -> None:
    main, _ = compile_options(
        Options(system_prompt="hi", allowed_tools=()))
    assert main.tools == ()


def test_disallowed_tools_subtracts_from_builtin_set() -> None:
    main, _ = compile_options(
        Options(
            system_prompt="hi",
            disallowed_tools=("shell_run", "edit"),
        )
    )
    names = {t.name for t in main.tools}
    assert "shell_run" not in names
    assert "edit" not in names
    assert len(names) == len(builtin_tool_classes()) - 2


def test_disallowed_tools_missing_names_are_silently_ignored() -> None:
    main, _ = compile_options(
        Options(
            system_prompt="hi",
            disallowed_tools=("does_not_exist", "also_bogus"),
        )
    )
    assert {t.name for t in main.tools} == set(builtin_tool_classes())


def test_disallowed_tools_with_explicit_allowed_tools() -> None:
    main, _ = compile_options(
        Options(
            system_prompt="hi",
            allowed_tools=("read", "glob", "apply_patch", "grep"),
            disallowed_tools=("glob", "grep"),
        )
    )
    assert {t.name for t in main.tools} == {"read", "apply_patch"}


def test_allowed_tools_dedup_preserves_first_occurrence() -> None:
    main, _ = compile_options(
        Options(
            system_prompt="hi",
            allowed_tools=("read", "glob", "read", my_tool, my_tool),
        )
    )
    # Order preserved on first occurrence; AgentSpec __post_init__ re-sorts
    # alphabetically, so we just assert count + name set.
    names = [t.name for t in main.tools]
    assert len(names) == 3
    assert set(names) == {"read", "glob", "my_tool"}


# ---------------------------------------------------------------------------
# permission_mode validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "valid", ["default", "acceptEdits", "bypassPermissions"]
)
def test_permission_mode_three_legal_values_pass(valid: str) -> None:
    main, _ = compile_options(
        Options(system_prompt="hi", permission_mode=valid))
    # The behaviour under test is that compiling does not raise.
    assert main.name == "main"


def test_permission_mode_invalid_value_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="permission_mode"):
        compile_options(
            Options(system_prompt="hi", permission_mode="bogus"))
    with pytest.raises(ValueError, match="permission_mode"):
        compile_options(
            Options(system_prompt="hi", permission_mode=""))


# ---------------------------------------------------------------------------
# max_turns / budget.max_iterations merging
# ---------------------------------------------------------------------------


def test_max_turns_populates_budget_max_iterations() -> None:
    main, _ = compile_options(
        Options(system_prompt="hi", max_turns=100))
    assert main.default_budget.max_iterations == 100
    # The runaway-recursion guard rides along with the derived budget.
    assert main.default_budget.max_subtask_depth == 3


def test_max_turns_combined_with_explicit_budget() -> None:
    main, _ = compile_options(
        Options(
            system_prompt="hi",
            budget=BudgetSpec(max_tool_calls=500),
            max_turns=77,
        )
    )
    assert main.default_budget.max_iterations == 77
    assert main.default_budget.max_tool_calls == 500
    # A caller-supplied budget overrides the default depth guard — stays None.
    assert main.default_budget.max_subtask_depth is None


def test_max_turns_and_budget_max_iterations_both_set_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="max_iterations"):
        compile_options(
            Options(
                system_prompt="hi",
                budget=BudgetSpec(max_iterations=50),
                max_turns=100,
            )
        )


# ---------------------------------------------------------------------------
# SystemPromptPreset resolution
# ---------------------------------------------------------------------------


def test_system_prompt_preset_unregistered_raises_valueerror() -> None:
    # A name nothing registers — ``noeta.presets`` owns the real ones.
    with pytest.raises(ValueError, match="preset"):
        compile_options(Options(system_prompt=SystemPromptPreset(preset="__no_such_preset__")))


def test_register_preset_prompt_resolves_successfully() -> None:
    try:
        register_preset_prompt("__test_main", "You are a coding assistant.")
        main, _ = compile_options(
            Options(system_prompt=SystemPromptPreset(preset="__test_main"))
        )
        assert main.instructions == "You are a coding assistant."
    finally:
        # Clean up to avoid leaking test state into other tests.
        _PRESET_PROMPTS.pop("__test_main", None)  # type: ignore[name-defined]


def test_system_prompt_preset_append_suffix_appended() -> None:
    try:
        register_preset_prompt("__test_append", "BASE PROMPT")
        main, _ = compile_options(
            Options(
                system_prompt=SystemPromptPreset(
                    preset="__test_append",
                    append="Extra rules: be nice.",
                )
            )
        )
        assert main.instructions == "BASE PROMPT\n\nExtra rules: be nice."
    finally:
        _PRESET_PROMPTS.pop("__test_append", None)  # type: ignore[name-defined]


def test_system_prompt_preset_error_lists_registered_names() -> None:
    try:
        register_preset_prompt("__a", "a")
        register_preset_prompt("__b", "b")
        with pytest.raises(ValueError) as exc:
            compile_options(Options(system_prompt=SystemPromptPreset(preset="no-such")))
        msg = str(exc.value)
        assert "__a" in msg
        assert "__b" in msg
        assert "no-such" in msg
    finally:
        _PRESET_PROMPTS.pop("__a", None)  # type: ignore[name-defined]
        _PRESET_PROMPTS.pop("__b", None)  # type: ignore[name-defined]


# Import _PRESET_PROMPTS for the try/finally cleanup blocks above.
from noeta.client.options import _PRESET_PROMPTS  # noqa: E402


# ---------------------------------------------------------------------------
# Purity / identity invariance for the wiring fields
# ---------------------------------------------------------------------------


def test_purity_new_fields_equal_inputs_equal_spec() -> None:
    """Equal ``Options`` compile to structurally equal specs on two
    independent calls."""
    opts = Options(
        system_prompt="hello",
        agents={
            "coder": AgentDefinition(
                description="Writes code.",
                prompt="You write code.",
                tools=("read",),
            )
        },
        allowed_tools=("read", "glob"),
        disallowed_tools=("glob",),
        permission_mode="acceptEdits",
        max_turns=42,
    )
    m_a, k_a = compile_options(opts)
    m_b, k_b = compile_options(opts)
    assert m_a == m_b
    assert k_a == k_b


def test_cwd_does_not_affect_identity() -> None:
    """``cwd`` is pure wiring (like ``provider``) — excluded from identity."""
    opts_a = Options(system_prompt="hi", cwd=None)
    opts_b = Options(system_prompt="hi", cwd="/tmp/project")
    opts_c = Options(system_prompt="hi", cwd="/totally/different")
    m_a, _ = compile_options(opts_a)
    m_b, _ = compile_options(opts_b)
    m_c, _ = compile_options(opts_c)
    assert m_a == m_b == m_c
    # Sanity: the fields themselves do differ on the Options surface.
    assert opts_a.cwd != opts_b.cwd


def test_permission_mode_change_does_not_affect_identity() -> None:
    """``permission_mode`` maps to approval guards, not to spec identity."""
    m_a, _ = compile_options(Options(system_prompt="hi", permission_mode="default"))
    m_b, _ = compile_options(Options(system_prompt="hi", permission_mode="bypassPermissions"))
    assert m_a == m_b


# ---------------------------------------------------------------------------
# AgentDefinition activation (plugins=) compiles into child identity
# ---------------------------------------------------------------------------


def test_agent_definition_activation_compiled_into_child_spec() -> None:
    """An ``AgentDefinition`` activation is part of the child spec's identity."""
    defn_with_caps = AgentDefinition(
        description="d",
        prompt="p",
        plugins=("todo_write", "ask_user_question"),
    )
    defn_plain = AgentDefinition(description="d", prompt="p")
    opts_with = Options(system_prompt="root", name="main", agents={"c": defn_with_caps})
    opts_plain = Options(system_prompt="root", name="main", agents={"c": defn_plain})

    _, kids_with = compile_options(opts_with)
    _, kids_plain = compile_options(opts_plain)

    assert agent_activates(kids_with[0], "todo_write") is True
    assert agent_activates(kids_with[0], "ask_user_question") is True
    assert kids_with[0] != kids_plain[0]


def test_agent_definition_no_activation_defaults_to_empty_capabilities() -> None:
    """A child with no declared ``plugins`` activates nothing."""
    defn = AgentDefinition(description="d", prompt="p")
    assert defn.plugins == ()
    _, kids = compile_options(Options(system_prompt="root", name="main", agents={"c": defn}))
    child = kids[0]
    assert child.plugins == ()
    assert agent_activates(child, "todo_write") is False
    assert agent_activates(child, "ask_user_question") is False
    assert agent_activates(child, "delegation") is False
    # The parent's spawnable must not leak down, and children never union it.
    assert tuple(child.spawnable) == ()


# ---------------------------------------------------------------------------
# skill_invocation activation is part of identity
# ---------------------------------------------------------------------------


def test_options_skill_invocation_passthrough() -> None:
    """``skill_invocation`` reaches the compiled main spec and changes its
    identity."""
    opts_false = Options(
        system_prompt="hi",
        name="main",
    )
    opts_true = Options(
        system_prompt="hi",
        name="main",
        plugins=DEFAULT_PLUGINS + ("skill_invocation",),
    )
    main_false, _ = compile_options(opts_false)
    main_true, _ = compile_options(opts_true)

    assert agent_activates(main_false, "skill_invocation") is False
    assert agent_activates(main_true, "skill_invocation") is True
    assert main_true != main_false


def test_agent_definition_skill_invocation_passthrough() -> None:
    """``skill_invocation`` reaches the compiled child spec and changes its
    identity."""
    defn_true = AgentDefinition(
        description="d",
        prompt="p",
        plugins=("skill_invocation",),
    )
    defn_plain = AgentDefinition(description="d", prompt="p")

    _, kids_true = compile_options(
        Options(system_prompt="root", name="main", agents={"c": defn_true})
    )
    _, kids_plain = compile_options(
        Options(system_prompt="root", name="main", agents={"c": defn_plain})
    )

    assert agent_activates(kids_true[0], "skill_invocation") is True
    assert agent_activates(kids_plain[0], "skill_invocation") is False
    assert kids_true[0] != kids_plain[0]


# -- output_schema / thinking / effort (wiring-only, + validation) ---------


def test_output_schema_thinking_effort_excluded_from_identity() -> None:
    """These three fields are wiring-only and do not change the AgentSpec identity."""
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    opts_a = Options(system_prompt="be terse", name="main")
    opts_b = Options(
        system_prompt="be terse",
        name="main",
        output_schema=schema,
        thinking="adaptive",
        effort="high",
    )
    opts_c = Options(
        system_prompt="be terse",
        name="main",
        thinking="disabled",
        effort="max",
        output_schema={"type": "array"},
    )
    main_a, _ = compile_options(opts_a)
    main_b, _ = compile_options(opts_b)
    main_c, _ = compile_options(opts_c)
    assert main_a == main_b == main_c


def test_thinking_invalid_value_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="thinking"):
        Options(system_prompt="hi", thinking="always")
    with pytest.raises(ValueError, match="thinking"):
        Options(system_prompt="hi", thinking="")
    # Valid values must not raise.
    Options(system_prompt="hi", thinking=None)
    Options(system_prompt="hi", thinking="adaptive")
    Options(system_prompt="hi", thinking="disabled")


def test_effort_invalid_value_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="effort"):
        Options(system_prompt="hi", effort="very-high")
    with pytest.raises(ValueError, match="effort"):
        Options(system_prompt="hi", effort="")
    # Valid values.
    for v in ("low", "medium", "high", "xhigh", "max", None):
        Options(system_prompt="hi", effort=v)


def test_output_schema_non_mapping_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="output_schema"):
        Options(system_prompt="hi", output_schema="not-a-dict")
    with pytest.raises(ValueError, match="output_schema"):
        Options(system_prompt="hi", output_schema=42)
    # Mapping (dict) and None must be fine.
    Options(system_prompt="hi", output_schema=None)
    Options(system_prompt="hi", output_schema={"type": "object"})
