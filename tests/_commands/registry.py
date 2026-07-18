"""Slash-command registry for noeta-agent.

This module is **pure data**.  It defines the static catalogue of built-in
slash commands and thin wrappers that bind the generic mechanism helpers in
:mod:`noeta.execution.commands` to the product's specific catalog.

A command is either a ``"prompt"`` command (it activates a built-in skill and
forks to a specific agent) or a ``"local"`` command (it renders text directly,
e.g. ``/help``, ``/agents``, and ``/skills``).

Architecture note: the *mechanism* (``SlashCommand`` shape,
resolution logic, help rendering) lives in :mod:`noeta.execution.commands`
alongside the skills mechanism.  Only the *content* — which commands exist,
what they do — stays here in the product.  This mirrors how
:mod:`noeta.execution.skills` provides the generic skill loader while the
product declares which skills are active.
"""

from __future__ import annotations

from noeta.execution.commands import SlashCommand
from noeta.execution.commands import get_command as _get_command_generic
from noeta.execution.commands import list_commands as _list_commands_generic

__all__ = [
    "SlashCommand",
    "BUILTIN_COMMANDS",
    "get_command",
    "list_commands",
]


def _prompt(
    name: str,
    description: str,
    *,
    skill: str,
    agent: str,
    argument_hint: str = "",
) -> SlashCommand:
    return SlashCommand(
        name=name,
        description=description,
        kind="prompt",
        skill=skill,
        agent=agent,
        argument_hint=argument_hint,
    )


def _local(name: str, description: str) -> SlashCommand:
    return SlashCommand(name=name, description=description, kind="local")


_COMMANDS: tuple[SlashCommand, ...] = (
    _prompt(
        "review",
        "Review the current git diff for correctness and repo-standard "
        "compliance.",
        skill="review",
        # D3: dedicated code-reviewer agent was removed; fork to
        # general-purpose — the review skill shapes behaviour, not the agent.
        agent="general-purpose",
        argument_hint="[focus or paths]",
    ),
    _prompt(
        "verify",
        "Verify a change actually does what it claims by running and "
        "observing it.",
        skill="verify",
        # D3: dedicated test-runner agent was removed; fork to
        # general-purpose — the verify skill shapes behaviour.
        agent="general-purpose",
        argument_hint="[what to verify]",
    ),
    _prompt(
        "simplify",
        "Clean up the change for reuse, simplification, and efficiency.",
        skill="simplify",
        agent="main",
        argument_hint="[focus or paths]",
    ),
    _prompt(
        "init",
        "Generate or refresh CONTEXT.md and project documentation.",
        skill="init",
        # D3: dedicated docs-writer agent was removed; fork to
        # general-purpose — the init skill shapes behaviour.
        agent="general-purpose",
        argument_hint="[focus]",
    ),
    _prompt(
        "commit",
        "Draft a git commit for the current change.",
        skill="commit",
        agent="main",
        argument_hint="[guidance]",
    ),
    _prompt(
        "handoff",
        "Compress the current session into a handoff document.",
        skill="handoff",
        agent="main",
        argument_hint="[focus]",
    ),
    _local("help", "List all available slash commands."),
    _local("agents", "List all available agents."),
    _local("skill", "List all available skills."),
    _local("skills", "List all available skills."),
)

BUILTIN_COMMANDS: dict[str, SlashCommand] = {c.name: c for c in _COMMANDS}


def get_command(name: str) -> SlashCommand:
    """Return the built-in command named ``name``.

    Raises:
        KeyError: If ``name`` is not a known command.  The error message lists
            the available command names.
    """

    return _get_command_generic(name, commands=BUILTIN_COMMANDS)


def list_commands() -> tuple[SlashCommand, ...]:
    """Return all built-in commands sorted by name."""

    return _list_commands_generic(BUILTIN_COMMANDS)
