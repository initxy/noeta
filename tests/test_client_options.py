"""Tests for ``noeta.client.options``.

Covers:

1. Purity: equal ``Options`` → structurally equal specs across two compile calls.
2. Substantive field mutations → the main spec is no longer ``==`` the base.
3. Non-identity wiring (``cwd``, ``permission_mode``) → spec unchanged.
4. Mixed tools: ``@tool``-decorated callable + builtin-name string produce
   the expected ``ToolRef`` s, and the builtin ``read`` ref is
   byte-equal to the one produced by ``noeta.agent.roster.specs.agent_spec_for``
   (cross-package sanity — SDK parts table must never drift from the
   code-roster parts table).
5. Error paths: unknown builtin name → ``KeyError``; non-DecoratedTool/str
   entry → ``TypeError``.
6. Child agents (flat ``agents`` dict): compile produces a flat
   descendant list, parent capabilities carry ``delegation=True`` and the
   child's name in ``spawnable``; child fingerprint is deterministic.
   Deep recursion and cousin-name-collision semantics no longer apply
   (see per-test inline comments).
7. Policy/composer ref alignment: compiled specs carry exactly
   ``ComponentRef("react","1")`` / ``ComponentRef("three_segment","v3")`` —
   the same values the code-roster uses.
"""

from __future__ import annotations

import dataclasses

import pytest

from noeta.agent.spec import BudgetSpec, Capabilities, ComponentRef, ToolRef
from noeta.client import (
    AgentDefinition,
    Options,
    SystemPromptPreset,
    builtin_tool_ref,
    compile_options,
    register_preset_prompt,
)
from noeta.client.parts import BUILTIN_TOOL_CLASSES, COMPOSER_REF, POLICY_REF
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
        capabilities=Capabilities(todo_write=True),
        metadata={"owner": "tester"},
        model="claude-sonnet-4-5",
    )


# ---------------------------------------------------------------------------
# 1. Purity
# ---------------------------------------------------------------------------

def test_purity_equal_inputs_equal_spec() -> None:
    opts = _base_options()
    main_a, kids_a = compile_options(opts)
    main_b, kids_b = compile_options(opts)
    assert main_a == main_b
    assert kids_a == kids_b


# ---------------------------------------------------------------------------
# 2. Substantive fields → spec identity changes
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


def test_substantive_capabilities_changes_identity() -> None:
    base, _ = compile_options(_base_options())
    mutated, _ = compile_options(
        dataclasses.replace(_base_options(), capabilities=Capabilities(todo_write=False))
    )
    assert base != mutated


def test_substantive_subagents_name_changes_identity() -> None:
    # child names live in the `agents` dict key. Swapping the key
    # changes spawnable contents and therefore the parent identity.
    child_a = AgentDefinition(description="sub agent", prompt="sub prompt")
    child_b = AgentDefinition(description="sub agent", prompt="sub prompt")
    base, _ = compile_options(dataclasses.replace(_base_options(), agents={"child_a": child_a}))
    mutated, _ = compile_options(dataclasses.replace(_base_options(), agents={"child_b": child_b}))
    assert base != mutated


# ---------------------------------------------------------------------------
# 3. Non-identity wiring (cwd, permission_mode) → spec unchanged (below in §13)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 4. Mixed tools: DecoratedTool + builtin string → correct refs, cross-package
# ---------------------------------------------------------------------------

def test_mixed_tools_refs_correct_and_cross_package_consistent() -> None:
    main, _ = compile_options(_base_options())

    # Two distinct ToolRefs, sorted alphabetically by AgentSpec.
    assert {t.name for t in main.tools} == {"my_tool", "read"}

    my_ref = next(t for t in main.tools if t.name == "my_tool")
    read_ref = next(t for t in main.tools if t.name == "read")

    # DecoratedTool's ref is carried through verbatim.
    assert my_ref == my_tool.ref
    assert my_ref == ToolRef(name="my_tool", version="2", risk_level="medium")

    # read's ref must byte-match the ref official_specs()["main"] declares for the
    # same name. This is the cross-package drift guard for slice 4a.
    roster_spec = official_specs()["main"]
    roster_read = next(t for t in roster_spec.tools if t.name == "read")
    assert read_ref == roster_read


# ---------------------------------------------------------------------------
# 5. Error paths
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
# 6. Child agents (flat `agents` dict): compilation + caps + determinism
# ---------------------------------------------------------------------------
# NOTE: the library-SDK refactor abandons recursive subagent nesting. Three-level trees
# (parent→child→grandchild) and cousin-name-collision semantics are no
# longer expressible — they have been dropped (see inline comments).
#
# Deleted tests (no longer expressible in the flat-dict model):
#   - test_subagents_flattened_and_capabilities_set (grandchild nesting)
#   - test_subagents_duplicate_name_raises_valueerror (dict keys are unique)
#   - test_deep_descendant_matching_root_name_raises_valueerror (no nesting)


def test_child_agents_flat_and_capabilities_set() -> None:
    """Flat `agents` dict: two children → parent delegation=True,
    spawnable = sorted child names; children are flat leaves."""
    opts = Options(
        system_prompt="parent",
        name="parent",
        agents={
            "child_a": AgentDefinition(description="child a", prompt="child a prompt"),
            "child_b": AgentDefinition(description="child b", prompt="child b prompt"),
        },
    )
    main, descendants = compile_options(opts)

    # Two flat descendants, no recursion.
    assert {s.name for s in descendants} == {"child_a", "child_b"}

    # Parent: delegation=True, spawnable = sorted union of child names.
    assert main.capabilities.delegation is True
    assert tuple(main.capabilities.spawnable) == ("child_a", "child_b")

    # Children: no delegation, no spawnable (flat leaves).
    for s in descendants:
        assert s.capabilities.delegation is False
        assert tuple(s.capabilities.spawnable) == ()


def test_child_agents_name_collides_with_root_raises_valueerror() -> None:
    """An `agents` entry sharing the main agent's own name raises ValueError
    at compile time (not later at Client registration)."""
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


def test_explicit_capabilities_preserves_flags_and_unions_spawnable() -> None:
    # Caller sets delegation=False explicitly with a child present → we
    # respect delegation=False (per _capabilities_for additive contract) but
    # still union spawnable so the child name is listed.
    explicit_caps = Capabilities(
        todo_write=True,
        ask_user_question=True,
        delegation=False,
        spawnable=("other_agent",),
    )
    opts = Options(
        system_prompt="parent",
        name="parent",
        capabilities=explicit_caps,
        agents={"kid_x": AgentDefinition(description="kid x", prompt="child prompt")},
    )
    main, _ = compile_options(opts)
    # Explicit flags preserved verbatim.
    assert main.capabilities.todo_write is True
    assert main.capabilities.ask_user_question is True
    assert main.capabilities.delegation is False
    # spawnable is union of explicit + inline.
    assert tuple(main.capabilities.spawnable) == ("kid_x", "other_agent")


# ---------------------------------------------------------------------------
# 7. Policy / composer ref alignment with roster
# ---------------------------------------------------------------------------

def test_policy_and_composer_match_roster_constants() -> None:
    main, _ = compile_options(_base_options())
    roster_spec = official_specs()["main"]
    assert main.policy == roster_spec.policy
    assert main.composer == roster_spec.composer
    # Direct assertion against our canonical parts refs too.
    assert main.policy == POLICY_REF
    assert main.composer == COMPOSER_REF


# ---------------------------------------------------------------------------
# Extra: builtin_tool_ref surface + BUILTIN_TOOL_CLASSES inventory
# ---------------------------------------------------------------------------

def test_builtin_tool_ref_inventory_complete() -> None:
    # The SDK parts table must cover every tool the code-roster declares.
    expected_names = {
        # fs read (list_dir retired, read_file renamed read)
        "read", "glob", "grep",
        # fs edit
        "edit", "write", "apply_patch",
        # fs shell (shell_poll / shell_kill joined the catalog
        # so the background triplet can enter a preset whitelist via tools=None)
        "shell_run", "shell_poll", "shell_kill",
        # web (webfetch is a built-in but not an fs tool;
        # it joins the catalog so main's tools=None full set includes it)
        "webfetch",
        # web_search joins the catalog the same way (so main's tools=None full
        # set includes it). It is only built when NOETA_WEB_SEARCH_API_KEY is set;
        # the catalog entry is the addressable name, gated at build time.
        "web_search",
    }
    assert set(BUILTIN_TOOL_CLASSES) == expected_names
    # Each name produces a ToolRef with version="1".
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
# 8. flat `agents` dict compilation
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
    # description goes into metadata.
    assert child.metadata.get("description") == "A researcher that finds facts."
    # Children get the standard budget guard (max_subtask_depth=3).
    assert child.default_budget.max_subtask_depth == 3
    # Children get Capabilities() — delegation False, no spawnable.
    assert child.capabilities.delegation is False
    assert tuple(child.capabilities.spawnable) == ()
    # Parent spawnable includes the flat dict name.
    assert tuple(main.capabilities.spawnable) == ("researcher",)
    assert main.capabilities.delegation is True


def test_agents_dict_child_metadata_merges_under_description() -> None:
    # AgentDefinition.metadata merges into the child spec's
    # metadata (description always wins its key), so the plan preset can ship
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
    """Two flat-dict entries with distinct keys compile cleanly and spawnable
    is sorted."""
    defn_a = AgentDefinition(description="a", prompt="pa")
    defn_b = AgentDefinition(description="b", prompt="pb")
    opts = Options(
        system_prompt="root",
        name="main",
        agents={"a": defn_a, "b": defn_b},
    )
    main, kids = compile_options(opts)
    assert {s.name for s in kids} == {"a", "b"}
    assert tuple(sorted(main.capabilities.spawnable)) == ("a", "b")


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
    # None → full set of 13 built-ins.
    assert {t.name for t in kids[0].tools} == set(BUILTIN_TOOL_CLASSES)


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
# 9. allowed_tools / disallowed_tools
# ---------------------------------------------------------------------------


def test_bare_options_defaults_to_all_builtin_tools() -> None:
    """Bare Options(system_prompt=…) defaults to full set of
    built-in tools (no `allowed_tools` override)."""
    main, _ = compile_options(Options(system_prompt="hi", name="main"))
    assert {t.name for t in main.tools} == set(BUILTIN_TOOL_CLASSES)


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
    # None → all builtins; subtract two → (len-2) remain.
    main, _ = compile_options(
        Options(
            system_prompt="hi",
            disallowed_tools=("shell_run", "edit"),
        )
    )
    names = {t.name for t in main.tools}
    assert "shell_run" not in names
    assert "edit" not in names
    assert len(names) == len(BUILTIN_TOOL_CLASSES) - 2


def test_disallowed_tools_missing_names_are_silently_ignored() -> None:
    # None → all builtins; subtract two non-existent names → same set.
    main, _ = compile_options(
        Options(
            system_prompt="hi",
            disallowed_tools=("does_not_exist", "also_bogus"),
        )
    )
    assert {t.name for t in main.tools} == set(BUILTIN_TOOL_CLASSES)


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
# 10. permission_mode validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "valid", ["default", "acceptEdits", "bypassPermissions"]
)
def test_permission_mode_three_legal_values_pass(valid: str) -> None:
    main, _ = compile_options(
        Options(system_prompt="hi", permission_mode=valid))
    # Compile succeeds (no runtime wiring yet — just no ValueError).
    assert main.name == "main"


def test_permission_mode_invalid_value_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="permission_mode"):
        compile_options(
            Options(system_prompt="hi", permission_mode="bogus"))
    with pytest.raises(ValueError, match="permission_mode"):
        compile_options(
            Options(system_prompt="hi", permission_mode=""))


# ---------------------------------------------------------------------------
# 11. max_turns / budget.max_iterations merging
# ---------------------------------------------------------------------------


def test_max_turns_populates_budget_max_iterations() -> None:
    main, _ = compile_options(
        Options(system_prompt="hi", max_turns=100))
    assert main.default_budget.max_iterations == 100
    # runaway-recursion guard is still installed.
    assert main.default_budget.max_subtask_depth == 3


def test_max_turns_combined_with_explicit_budget() -> None:
    main, _ = compile_options(
        Options(
            system_prompt="hi",
            budget=BudgetSpec(max_tool_calls=500),
            max_turns=77,
        )
    )
    # max_iterations comes from max_turns; the rest from explicit budget.
    assert main.default_budget.max_iterations == 77
    assert main.default_budget.max_tool_calls == 500
    # Caller supplied budget overrides our default depth guard — keep as None.
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
# 12. SystemPromptPreset resolution
# ---------------------------------------------------------------------------


def test_system_prompt_preset_unregistered_raises_valueerror() -> None:
    # Use a name that can never be registered to verify the unregistered-preset
    # ValueError path. ("main" is now registered by noeta.presets, so it no
    # longer works as an unregistered example.)
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
# 13. Purity / fingerprint invariants with new fields
# ---------------------------------------------------------------------------


def test_purity_new_fields_equal_inputs_equal_spec() -> None:
    """Equal Options (using new fields) compile to structurally
    equal specs on two independent calls."""
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
    """permission_mode is not part of AgentSpec identity (it maps to guards,
    which this issue does not yet wire — identity does not change)."""
    m_a, _ = compile_options(Options(system_prompt="hi", permission_mode="default"))
    m_b, _ = compile_options(Options(system_prompt="hi", permission_mode="bypassPermissions"))
    assert m_a == m_b


# ---------------------------------------------------------------------------
# 14. AgentDefinition.capabilities (advanced field)
# ---------------------------------------------------------------------------


def test_agent_definition_capabilities_compiled_into_child_spec() -> None:
    """An AgentDefinition with capabilities compiles to a child spec whose
    identity differs from a child spec built with default Capabilities() —
    i.e. capabilities are compiled into the child spec's identity."""
    defn_with_caps = AgentDefinition(
        description="d",
        prompt="p",
        capabilities=Capabilities(todo_write=True, ask_user_question=True),
    )
    defn_plain = AgentDefinition(description="d", prompt="p")
    opts_with = Options(system_prompt="root", name="main", agents={"c": defn_with_caps})
    opts_plain = Options(system_prompt="root", name="main", agents={"c": defn_plain})

    _, kids_with = compile_options(opts_with)
    _, kids_plain = compile_options(opts_plain)

    assert kids_with[0].capabilities.todo_write is True
    assert kids_with[0].capabilities.ask_user_question is True
    # With vs without capabilities: identities must differ.
    assert kids_with[0] != kids_plain[0]


def test_agent_definition_capabilities_none_defaults_to_empty_capabilities() -> None:
    """With capabilities unset (default None), the child spec's capabilities
    are the all-default Capabilities(); spawnable stays empty (children are
    flat leaves and do not union spawnable like the parent does)."""
    defn = AgentDefinition(description="d", prompt="p")
    assert defn.capabilities is None  # surface default really is None
    _, kids = compile_options(Options(system_prompt="root", name="main", agents={"c": defn}))
    caps = kids[0].capabilities
    # All flags False.
    assert caps.todo_write is False
    assert caps.ask_user_question is False
    assert caps.delegation is False
    # spawnable empty (not the parent's spawnable, and no union across children).
    assert tuple(caps.spawnable) == ()


# ---------------------------------------------------------------------------
# 15. skill_invocation capability is part of identity
# ---------------------------------------------------------------------------


def test_options_skill_invocation_passthrough() -> None:
    """When Options.capabilities sets skill_invocation=True, the flag passes
    through to the compiled main spec and its identity differs from the
    False version."""
    opts_false = Options(
        system_prompt="hi",
        name="main",
        capabilities=Capabilities(skill_invocation=False),
    )
    opts_true = Options(
        system_prompt="hi",
        name="main",
        capabilities=Capabilities(skill_invocation=True),
    )
    main_false, _ = compile_options(opts_false)
    main_true, _ = compile_options(opts_true)

    assert main_false.capabilities.skill_invocation is False
    assert main_true.capabilities.skill_invocation is True
    assert main_true != main_false


def test_agent_definition_skill_invocation_passthrough() -> None:
    """When AgentDefinition.capabilities sets skill_invocation=True, the flag
    passes through to the compiled child spec and its identity differs from
    a child spec built with default Capabilities()."""
    defn_true = AgentDefinition(
        description="d",
        prompt="p",
        capabilities=Capabilities(skill_invocation=True),
    )
    defn_plain = AgentDefinition(description="d", prompt="p")

    _, kids_true = compile_options(
        Options(system_prompt="root", name="main", agents={"c": defn_true})
    )
    _, kids_plain = compile_options(
        Options(system_prompt="root", name="main", agents={"c": defn_plain})
    )

    assert kids_true[0].capabilities.skill_invocation is True
    assert kids_plain[0].capabilities.skill_invocation is False
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
