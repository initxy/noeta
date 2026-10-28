"""Slash commands from an agent's extensions, for front ends.

A front end lists them (:func:`list_commands`) next to its own and, when the
user types ``/<name> <args>`` that is not one of its own, turns it into the
message to send (:func:`expand_command`). An extension that fails to list or
run is logged and left out; its failure never stops the front end.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from noeta.core.agent import Agent
from noeta.core.extension import Command, CommandContext, Extension

__all__ = [
    "command_message",
    "expand_command",
    "expand_with",
    "is_command_message",
    "list_commands",
    "parse_command",
    "parse_command_message",
]

log = logging.getLogger("noeta.commands")


def parse_command(text: str) -> tuple[str, str] | None:
    """``"/name args"`` as ``(name, args)``; ``None`` for other text."""
    if not text.startswith("/") or text.startswith("//"):
        return None
    name, _, args = text[1:].partition(" ")
    name = name.strip()
    return (name, args.strip()) if name else None


#: The tags a typed slash command reaches the model in (Claude Code's).
_COMMAND_OPENERS = ("<command-message>", "<command-name>", "<local-command-caveat>")
_COMMAND_MESSAGE_RE = re.compile(
    r"\A<command-message>([^<]*)</command-message>\n<command-name>/([^<]*)</command-name>"
    r"\n<command-args>([\s\S]*?)</command-args>")


def command_message(name: str, args: str) -> str:
    """How a command that sends a prompt (a skill) shows in the person's
    turn: Claude Code's ``<command-message>`` / ``<command-name>`` /
    ``<command-args>`` tags."""
    return (f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>\n"
            f"<command-args>{args}</command-args>")


def parse_command_message(text: str) -> tuple[str, str] | None:
    """``(name, args)`` of a :func:`command_message`; ``None`` for other text."""
    m = _COMMAND_MESSAGE_RE.match(text)
    return (m.group(2), m.group(3)) if m else None


def is_command_message(text: str) -> bool:
    """Whether ``text`` is a typed slash command as Claude Code's tags carry
    it (a prompt command or a local one), not something the person wrote."""
    return text.lstrip().startswith(_COMMAND_OPENERS)


async def _owned(agent: Agent, ctx: CommandContext) -> list[tuple[Extension, Command]]:
    return await _offered(agent.all_extensions(), ctx)


async def _offered(extensions: Sequence[Extension], ctx: CommandContext
                   ) -> list[tuple[Extension, Command]]:
    out: list[tuple[Extension, Command]] = []
    seen: set[str] = set()
    for ext in extensions:
        try:
            commands = await ext.commands(ctx)
        except Exception:
            log.exception("extension %s: listing commands failed", ext.name)
            continue
        for c in commands:
            if c.name not in seen:  # the first extension offering a name keeps it
                seen.add(c.name)
                out.append((ext, c))
    return out


async def list_commands(agent: Agent, ctx: CommandContext) -> list[Command]:
    """Every command ``agent``'s extensions offer, sorted by name."""
    return sorted((c for _e, c in await _owned(agent, ctx)), key=lambda c: c.name)


async def expand_command(agent: Agent, ctx: CommandContext, text: str) -> str | None:
    """The message ``text`` (``/name args``) stands for, or ``None`` when no
    extension of ``agent`` offers ``name``. Raises what ``run_command``
    raises."""
    return await expand_with(agent.all_extensions(), ctx, text)


async def expand_with(extensions: Sequence[Extension], ctx: CommandContext,
                      text: str) -> str | None:
    """:func:`expand_command` over a task's extensions (``TaskView``): what a
    plugin that replays a typed command later (a scheduled prompt) uses."""
    parsed = parse_command(text)
    if parsed is None:
        return None
    name, args = parsed
    for ext, c in await _offered(extensions, ctx):
        if c.name == name:
            if c.template:
                return c.template.replace("{args}", args).strip()
            return await ext.run_command(ctx, name, args)
    return None
