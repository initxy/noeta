"""Slash-command mechanism.

This module is the **mechanism** layer for slash commands.  It defines the
shape of a command (``SlashCommand``), of a resolved command
(``CommandResolution``), and the generic helpers that operate on a caller-supplied
``commands`` catalog and ``local_renderers`` mapping.

It deliberately contains **no** specific command names, no specific agent
names, and no static catalog.  That is the *content* layer, which lives in the
product (``noeta.agent.commands``).  The split mirrors how ``noeta.execution.skills``
provides the skill-loading mechanism while the product registers which skills
are actually active.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

__all__ = [
    "SlashCommand",
    "CommandResolution",
    "first_sentence",
    "get_command",
    "list_commands",
    "render_help",
    "resolve_command",
]


@dataclass(frozen=True, slots=True)
class SlashCommand:
    """A single slash command.

    Attributes:
        name: Command name without the leading slash, e.g. ``"mycmd"``.
        description: One-line description shown in the help listing.
        kind: Either ``"prompt"`` or ``"local"``.
        skill: For prompt commands, the built-in skill name to activate.
        agent: For prompt commands, the registry key of the agent to fork to.
        argument_hint: Optional hint describing the command's arguments.
    """

    name: str
    description: str
    kind: str
    skill: str | None = None
    agent: str | None = None
    argument_hint: str = ""


@dataclass(frozen=True, slots=True)
class CommandResolution:
    """The result of resolving a slash command.

    For prompt commands, ``agent`` / ``skill`` / ``arguments`` are populated and
    ``text`` is ``None``. For local commands, ``text`` is the rendered output and
    ``agent`` / ``skill`` are ``None``.
    """

    command: SlashCommand
    agent: str | None
    skill: str | None
    arguments: str
    text: str | None


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (up to and including a period)."""

    stripped = text.strip()
    if not stripped:
        return ""
    # Collapse to a single line first so a leading newline does not truncate.
    first_line = stripped.splitlines()[0].strip()
    head, sep, _ = first_line.partition(".")
    return head + sep if sep else first_line


def get_command(name: str, *, commands: Mapping[str, SlashCommand]) -> SlashCommand:
    """Return the command named ``name`` from ``commands``.

    Raises:
        KeyError: If ``name`` is not in ``commands``.  The error message lists
            the available command names, sorted, so typos are loud.
    """

    command = commands.get(name)
    if command is None:
        available = ", ".join(sorted(commands))
        raise KeyError(f"unknown command {name!r}; available: {available}")
    return command


def list_commands(commands: Mapping[str, SlashCommand]) -> tuple[SlashCommand, ...]:
    """Return all commands in ``commands`` sorted by name."""

    return tuple(sorted(commands.values(), key=lambda c: c.name))


def render_help(commands: Mapping[str, SlashCommand]) -> str:
    """Render a slash-command help listing of ``commands``.

    Each line is ``"/{name} — {description}"`` and lines are sorted by command
    name.
    """

    lines = [f"/{c.name} — {c.description}" for c in list_commands(commands)]
    return "\n".join(lines)


def resolve_command(
    name: str,
    arguments: str = "",
    *,
    commands: Mapping[str, SlashCommand],
    local_renderers: Mapping[str, Callable[[], str]],
) -> CommandResolution:
    """Resolve a slash command by name against a supplied catalog.

    Args:
        name: Command name without the leading slash.
        arguments: User-supplied arguments, passed through verbatim to prompt
            commands.  Ignored by local commands.
        commands: The catalog of known commands, keyed by name.
        local_renderers: For local commands, maps the command name to a zero-arg
            callable that returns the rendered text.  Local commands without a
            registered renderer yield an empty string.

    Returns:
        A :class:`CommandResolution`.

    Raises:
        KeyError: If ``name`` is not in ``commands``.
    """

    command = get_command(name, commands=commands)

    if command.kind == "local":
        renderer = local_renderers.get(command.name)
        text = renderer() if renderer is not None else ""
        return CommandResolution(
            command=command,
            agent=None,
            skill=None,
            arguments="",
            text=text,
        )

    return CommandResolution(
        command=command,
        agent=command.agent,
        skill=command.skill,
        arguments=arguments,
        text=None,
    )
