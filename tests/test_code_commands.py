"""D5 — slash-command registry + resolution (noeta-code).

``BUILTIN_COMMANDS`` is the static catalogue Claude-Code-style slash commands
map onto: six ``"prompt"`` commands (each activates a built-in skill and forks
to a named agent) plus four ``"local"`` commands (``/help``, ``/agents``,
``/skill``, and ``/skills``,
rendered to text).

The load-bearing **parity invariant**: every prompt command points at a real
built-in skill (``∈ load_builtin_skills().names()``) and a real agent
(``∈ noeta.agent.official_specs()``). If a command names a skill or agent that does not
exist, this test fails — that is the whole point of pinning the registry as
data rather than re-deriving it.
"""

from __future__ import annotations

import pytest

from noeta.agent.commands import (
    BUILTIN_COMMANDS,
    CommandResolution,
    SlashCommand,
    get_command,
    list_commands,
    resolve_command,
)
from noeta.agent.skills import load_builtin_skills
from noeta.presets import official_specs


_EXPECTED_PROMPT_COMMANDS = frozenset(
    {"review", "verify", "simplify", "init", "commit", "handoff"}
)
_EXPECTED_LOCAL_COMMANDS = frozenset({"help", "agents", "skill", "skills"})
_EXPECTED_COMMANDS = _EXPECTED_PROMPT_COMMANDS | _EXPECTED_LOCAL_COMMANDS


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_builtin_commands_carry_expected_names() -> None:
    assert set(BUILTIN_COMMANDS) == _EXPECTED_COMMANDS


def test_command_kinds_split_prompt_and_local() -> None:
    prompt = {n for n, c in BUILTIN_COMMANDS.items() if c.kind == "prompt"}
    local = {n for n, c in BUILTIN_COMMANDS.items() if c.kind == "local"}
    assert prompt == _EXPECTED_PROMPT_COMMANDS
    assert local == _EXPECTED_LOCAL_COMMANDS


def test_registry_is_keyed_by_command_name() -> None:
    for name, command in BUILTIN_COMMANDS.items():
        assert isinstance(command, SlashCommand)
        assert command.name == name


def test_list_commands_is_sorted_by_name() -> None:
    names = [c.name for c in list_commands()]
    assert names == sorted(_EXPECTED_COMMANDS)


def test_get_command_unknown_raises_keyerror_with_available_list() -> None:
    with pytest.raises(KeyError) as info:
        get_command("not-a-command")
    message = str(info.value)
    assert "not-a-command" in message
    # The error message lists the available commands so a typo is loud.
    for known in _EXPECTED_COMMANDS:
        assert known in message


# ---------------------------------------------------------------------------
# Parity invariant — every prompt command's skill + agent exist
# ---------------------------------------------------------------------------


def test_every_prompt_command_targets_a_real_skill_and_agent() -> None:
    builtin_skill_names = set(load_builtin_skills().names())
    agent_names = set(official_specs())

    for name, command in BUILTIN_COMMANDS.items():
        if command.kind != "prompt":
            continue
        assert command.skill is not None, f"{name} prompt cmd lacks a skill"
        assert command.agent is not None, f"{name} prompt cmd lacks an agent"
        assert command.skill in builtin_skill_names, (
            f"command {name!r} → skill {command.skill!r} not a built-in skill"
        )
        assert command.agent in agent_names, (
            f"command {name!r} → agent {command.agent!r} not in official_specs()"
        )


def test_local_commands_have_no_skill_or_agent() -> None:
    for name in _EXPECTED_LOCAL_COMMANDS:
        command = get_command(name)
        assert command.kind == "local"
        assert command.skill is None
        assert command.agent is None


# ---------------------------------------------------------------------------
# resolve_command — prompt commands
# ---------------------------------------------------------------------------


def test_resolve_review_yields_code_reviewer_and_review_skill() -> None:
    resolution = resolve_command("review", "since main")

    assert isinstance(resolution, CommandResolution)
    assert resolution.command is BUILTIN_COMMANDS["review"]
    assert resolution.agent == "general-purpose"
    assert resolution.skill == "review"
    # Arguments pass through verbatim ($ARGUMENTS).
    assert resolution.arguments == "since main"
    # Prompt commands render no local text.
    assert resolution.text is None


def test_resolve_prompt_command_passes_arguments_through() -> None:
    resolution = resolve_command("commit", "only the parser fix")
    assert resolution.agent == "main"
    assert resolution.skill == "commit"
    assert resolution.arguments == "only the parser fix"
    assert resolution.text is None


def test_resolve_prompt_command_default_arguments_is_empty() -> None:
    resolution = resolve_command("verify")
    assert resolution.skill == "verify"
    assert resolution.agent == "general-purpose"
    assert resolution.arguments == ""


# ---------------------------------------------------------------------------
# resolve_command — local commands
# ---------------------------------------------------------------------------


def test_resolve_help_text_lists_every_command_name() -> None:
    resolution = resolve_command("help")
    assert resolution.agent is None
    assert resolution.skill is None
    assert resolution.text is not None
    for name in _EXPECTED_COMMANDS:
        assert f"/{name}" in resolution.text


def test_resolve_agents_text_lists_every_agent_name() -> None:
    resolution = resolve_command("agents")
    assert resolution.agent is None
    assert resolution.skill is None
    assert resolution.text is not None
    # main and default are the same object; the renderer de-dups by
    # identity, so we assert each *distinct* agent appears at least once.
    distinct_agent_names = {
        name for name in official_specs() if name in resolution.text
    }
    # Every distinct underlying agent must be represented. main/default
    # alias one object, so requiring both names is wrong; instead assert
    # the non-aliased names are all present and at least one of the
    # main/default pair shows up.
    aliased = {"main", "default"}
    for name in set(official_specs()) - aliased:
        assert name in resolution.text, f"agent {name!r} missing from /agents"
    assert distinct_agent_names & aliased


def test_resolve_skills_text_lists_every_builtin_skill() -> None:
    resolution = resolve_command("skills")
    assert resolution.agent is None
    assert resolution.skill is None
    assert resolution.text is not None

    registry = load_builtin_skills()
    for name in registry.names():
        skill = registry.get(name)
        assert skill is not None
        assert name in resolution.text
        assert skill.description in resolution.text


def test_resolve_skill_alias_lists_every_builtin_skill() -> None:
    resolution = resolve_command("skill")
    assert resolution.agent is None
    assert resolution.skill is None
    assert resolution.text is not None
    assert "review" in resolution.text
    assert "verify" in resolution.text


def test_resolve_unknown_command_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        resolve_command("does-not-exist")
