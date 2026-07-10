"""Contract tests for the official preset quartet (noeta.presets).

The SDK-side quartet is copied from the product roster rather than imported
from it, so its identities are independent of noeta.agent.roster constant
changes.
"""
from __future__ import annotations

import pytest

from noeta.agent.spec import Capabilities
from noeta.client.options import Options, SystemPromptPreset, compile_options
from noeta.client.parts import BUILTIN_TOOL_CLASSES
from noeta.presets import (
    MAIN_SYSTEM_PROMPT,
    MEMORY_POLICY_PROMPT,
    OFFICIAL_SUBAGENTS,
    main_options,
    official_specs,
    sandbox_browser_options,
)


# ---------------------------------------------------------------------------
# 1. Contract: quartet membership + main's spawnable + non-empty description
# ---------------------------------------------------------------------------


def test_official_specs_four_keys_exact() -> None:
    specs = official_specs()
    assert set(specs.keys()) == {"main", "explore", "general-purpose", "plan"}


def test_main_spawnable_three_subagents_sorted() -> None:
    specs = official_specs()
    main = specs["main"]
    assert tuple(main.capabilities.spawnable) == (
        "explore",
        "general-purpose",
        "plan",
    )
    # main has all three control-plane switches on + skill_invocation + memory
    # (plan_mode was dropped).
    assert main.capabilities.todo_write is True
    assert main.capabilities.ask_user_question is True
    assert main.capabilities.delegation is True
    assert main.capabilities.skill_invocation is True
    assert main.capabilities.memory is True
    # main opens MCP inheritance for opt-in workers.
    assert main.capabilities.mcp is True


def test_each_subagent_description_non_empty() -> None:
    specs = official_specs()
    for name in ("explore", "general-purpose", "plan"):
        desc = specs[name].metadata.get("description")
        assert desc and desc.strip(), f"{name} has an empty description"


# ---------------------------------------------------------------------------
# 3. Determinism: same input, two calls, structurally equal specs
# ---------------------------------------------------------------------------


def test_official_specs_deterministic() -> None:
    a = official_specs()
    b = official_specs()
    for name in a:
        assert a[name] == b[name]


# ---------------------------------------------------------------------------
# 4. SystemPromptPreset("main") resolves (takes effect after import noeta.presets)
# ---------------------------------------------------------------------------


def test_import_presets_registers_main_preset_prompt() -> None:
    opts = Options(
        system_prompt=SystemPromptPreset(preset="main", append="Extra rule."),
        name="main",
    )
    main, _ = compile_options(opts)
    assert main.instructions == MAIN_SYSTEM_PROMPT + "\n\n" + "Extra rule."


def test_preset_no_append_matches_main_prompt_verbatim() -> None:
    opts = Options(system_prompt=SystemPromptPreset(preset="main"), name="main")
    main, _ = compile_options(opts)
    assert main.instructions == MAIN_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 5. Tool counts: main == full BUILTIN set; explore/plan == 3 read-only (read/grep/glob);
#    general-purpose == explicit whitelist read/write/edit + shell triplet
#    (narrowed, was the full BUILTIN set before)
# ---------------------------------------------------------------------------


def test_main_has_all_builtin_tools() -> None:
    specs = official_specs()
    main = specs["main"]
    names = {t.name for t in main.tools}
    assert names == set(BUILTIN_TOOL_CLASSES)
    # the full builtin set now carries the shell triplet, so
    # main (tools=None catchall) gets all three.
    assert {"shell_run", "shell_poll", "shell_kill"} <= names


def test_webfetch_in_main_and_all_subagents() -> None:
    # CC alignment: webfetch is now in main AND all three subagents — CC's
    # general-purpose has the full toolset, and its Explore/Plan agents have
    # every tool except the write family (so WebFetch is available to them).
    assert "webfetch" in BUILTIN_TOOL_CLASSES
    specs = official_specs()
    for name in ("main", "explore", "plan", "general-purpose"):
        sub_names = {t.name for t in specs[name].tools}
        assert "webfetch" in sub_names, f"{name} should whitelist webfetch"


def test_webfetch_builtin_ref_low_risk() -> None:
    # risk is read straight off the tool class defaults.
    from noeta.client.parts import builtin_tool_ref

    ref = builtin_tool_ref("webfetch")
    assert ref.risk_level == "low"


def test_shell_triplet_registered_in_builtin_catalog() -> None:
    # shell_poll / shell_kill joined BUILTIN_TOOL_CLASSES so
    # they are addressable by name in a preset whitelist (shell_run was already
    # there). risk is read straight off the tool class defaults.
    from noeta.client.parts import builtin_tool_ref

    for name in ("shell_run", "shell_poll", "shell_kill"):
        assert name in BUILTIN_TOOL_CLASSES
    assert builtin_tool_ref("shell_run").risk_level == "high"
    assert builtin_tool_ref("shell_kill").risk_level == "high"
    assert builtin_tool_ref("shell_poll").risk_level == "low"


def test_explore_and_plan_have_shell_but_no_write_family() -> None:
    # CC alignment: Explore/Plan get every built-in tool EXCEPT the write
    # family. shell_run/poll/kill are in the whitelist (CC's Explore/Plan can
    # run Bash); the read-only guarantee is prompt-enforced (shell is restricted
    # to read-only commands), backed by noeta's high-risk shell approval gate.
    specs = official_specs()
    for name in ("explore", "plan"):
        names = {t.name for t in specs[name].tools}
        assert {"shell_run", "shell_poll", "shell_kill"} <= names, name
        # The write family is still physically excluded from both.
        assert names.isdisjoint({"edit", "write", "apply_patch"}), name


def test_builtin_tools_have_nonempty_description() -> None:
    # every shipping built-in tool carries a hand-written, LLM-facing
    # description — the model's single source of tool semantics, rendered into
    # the provider tool schema instead of being restated in the system prompt.
    for name, cls in BUILTIN_TOOL_CLASSES.items():
        desc = getattr(cls, "description", "")
        assert isinstance(desc, str) and desc.strip(), f"{name} is missing a description"


# tool descriptions are aligned to Claude Code's terse short-form —
# a one-line summary followed by a few bullets — NOT the four-section
# (what / when / when-NOT / preconditions) essay, which was dropped as part of the
# Claude-Code catalog alignment. These still live in independent .md resources.
_CC_STYLE_TOOLS = (
    "read",
    "write",
    "edit",
    "apply_patch",
    "grep",
    "glob",
    "shell_run",
    "shell_poll",
    "shell_kill",
    "webfetch",
)
_DROPPED_FOUR_SECTION_HEADINGS = (
    "## What it does",
    "## When to use",
    "## When NOT to use",
    "## Preconditions",
)


@pytest.mark.parametrize("name", _CC_STYLE_TOOLS)
def test_tool_description_is_cc_short_form(name: str) -> None:
    from noeta.tools.descriptions import load_tool_description

    text = load_tool_description(name)
    # Opens with a non-empty one-line summary, not a markdown heading.
    first_line = text.strip().splitlines()[0]
    assert first_line and not first_line.startswith("#"), (
        f"{name}.md should open with a one-line summary, not a heading"
    )
    # The four-section template was dropped.
    for heading in _DROPPED_FOUR_SECTION_HEADINGS:
        assert heading not in text, f"{name}.md still carries dropped section {heading!r}"


def test_shell_triplet_descriptions_load_from_resources() -> None:
    # the three shell tool classes pull their canonical
    # description from descriptions/<name>.md (not an inline Python string), so
    # the class default equals the resource text verbatim.
    from noeta.tools.descriptions import load_tool_description
    from noeta.tools.fs import ShellKillTool, ShellPollTool, ShellRunTool

    assert ShellRunTool.description == load_tool_description("shell_run")
    assert ShellPollTool.description == load_tool_description("shell_poll")
    assert ShellKillTool.description == load_tool_description("shell_kill")
    # Semantics still present in the text (detached handle + job_id).
    assert "run_in_background" in ShellRunTool.description
    assert "job_id" in ShellPollTool.description
    assert "job_id" in ShellKillTool.description


def test_explore_and_plan_whitelist_is_all_but_write_family() -> None:
    # CC alignment: Explore and Plan share the same read-mostly whitelist —
    # every built-in tool EXCEPT the write family (edit/write/apply_patch).
    # That is glob/grep/read + the shell triplet + webfetch.
    expected = {"glob", "grep", "read", "shell_run", "shell_poll", "shell_kill", "webfetch"}
    specs = official_specs()
    for name in ("explore", "plan"):
        names = {t.name for t in specs[name].tools}
        assert names == expected, f"{name} toolset should match the CC scout set"
        assert names.isdisjoint({"edit", "write", "apply_patch"}), name


def test_no_subagent_carries_write_path_globs_metadata() -> None:
    # CC alignment: plan no longer has a restricted write, so its old
    # write_path_globs metadata is gone. No official agent ships it anymore.
    specs = official_specs()
    for name in ("main", "explore", "general-purpose", "plan"):
        assert "write_path_globs" not in specs[name].metadata


def test_general_purpose_whitelist_is_full_builtin_set() -> None:
    # CC alignment: general-purpose mirrors CC's general-purpose agent — the
    # full built-in tool surface (same as main), so it searches with grep/glob
    # and batch-edits with apply_patch instead of falling back to shell.
    specs = official_specs()
    names = {t.name for t in specs["general-purpose"].tools}
    assert names == set(BUILTIN_TOOL_CLASSES)
    # The previously-dropped search/patch/web tools are now present.
    assert {"grep", "glob", "apply_patch", "webfetch"} <= names
    # gp's tool surface now equals main's (both the full built-in catalog).
    main_names = {t.name for t in specs["main"].tools}
    assert names == main_names


def test_general_purpose_output_is_return_value_contract() -> None:
    # gp's system prompt must state that its final text
    # output is the RETURN VALUE handed to the caller — data, not a
    # conversational message written for a human.
    specs = official_specs()
    prompt = specs["general-purpose"].instructions
    low = prompt.lower()
    assert "return value" in low
    assert "data" in low
    assert "not a conversational message" in low or "not a conversational" in low


# ---------------------------------------------------------------------------
# 6. Exact subagent capabilities
# ---------------------------------------------------------------------------


def test_subagent_capabilities_exact() -> None:
    # plan's capabilities changed from
    # {todo_write, skill_invocation} to ONLY {ask_user_question} — the plan IS
    # the deliverable so it no longer needs todo_write, and per D8 plan opens
    # only ask_user_question. explore is unchanged.
    # general-purpose drops todo_write (gp returns a value,
    # it does not narrate progress); only skill_invocation remains. delegation
    # stays off (empty spawnable) — gp is a leaf worker.
    # general-purpose ALSO opens ``mcp`` (it is the real
    # working worker, so it opts into inheriting the parent's enabled MCP tool
    # set). explore / plan stay mcp=False (kept physically MCP-free).
    specs = official_specs()
    assert specs["general-purpose"].capabilities == Capabilities(
        skill_invocation=True,
        mcp=True,
    )
    # Explicit: gp neither narrates (todo_write) nor delegates further down.
    assert specs["general-purpose"].capabilities.todo_write is False
    assert specs["general-purpose"].capabilities.delegation is False
    assert specs["general-purpose"].capabilities.spawnable == ()
    assert specs["general-purpose"].capabilities.mcp is True
    assert specs["plan"].capabilities == Capabilities(ask_user_question=True)
    assert specs["plan"].capabilities.mcp is False
    # explore:skill_invocation True, todo_write False, mcp False (read-only scout)
    assert specs["explore"].capabilities == Capabilities(skill_invocation=True)
    assert specs["explore"].capabilities.mcp is False


# ---------------------------------------------------------------------------
# 7. OFFICIAL_SUBAGENTS surface-shape checks
# ---------------------------------------------------------------------------


def test_plan_prompt_is_readonly_and_returns_plan() -> None:
    # CC alignment: the plan system prompt states it is READ-ONLY (no edit/write
    # tools) and that the plan is returned as the agent's message, not written
    # to disk. It must not lean on todo_write either.
    specs = official_specs()
    prompt = specs["plan"].instructions
    low = prompt.lower()
    assert "plan" in low
    assert "read-only" in low
    assert "do not write it to disk" in low or "return the plan as your message" in low
    assert "todo_write" not in prompt


def test_official_subagents_three_keys() -> None:
    assert set(OFFICIAL_SUBAGENTS.keys()) == {"general-purpose", "explore", "plan"}


# ---------------------------------------------------------------------------
# 8. Memory-policy prompt fragment (memory v2): in the prompt iff the preset
#    opens Capabilities.memory
# ---------------------------------------------------------------------------


def test_memory_policy_fragment_only_in_memory_presets() -> None:
    # The fragment rides the prompt of memory-enabled presets — main and its
    # sandbox-browser variant (which inherits main's capabilities) — and of
    # no memory-free preset.
    specs = official_specs()
    assert specs["main"].capabilities.memory is True
    assert MEMORY_POLICY_PROMPT in specs["main"].instructions
    web_opts = sandbox_browser_options()
    assert web_opts.capabilities.memory is True
    assert MEMORY_POLICY_PROMPT in web_opts.system_prompt
    for name in ("explore", "plan", "general-purpose"):
        assert specs[name].capabilities.memory is False
        assert MEMORY_POLICY_PROMPT not in specs[name].instructions


def test_main_options_reproducible_and_compiles() -> None:
    m_a, kids_a = compile_options(main_options())
    m_b, kids_b = compile_options(main_options())
    assert m_a == m_b
    assert len(kids_a) == len(kids_b) == 3
    for ka, kb in zip(sorted(kids_a, key=lambda s: s.name),
                    sorted(kids_b, key=lambda s: s.name)):
        assert ka == kb
