"""Slash commands from an agent's extensions, for front ends.

A front end lists them (:func:`list_commands`) next to its own and, when the
user types ``/<name> <args>`` that is not one of its own, turns it into the
message to send (:func:`expand_command`), or with :func:`run_typed` also
learns that a local command's text is to be shown, not sent. An extension
that fails to list or run is logged and left out; its failure never stops
the front end.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from noeta.core.agent import Agent
from noeta.core.extension import Command, CommandContext, Extension

__all__ = ["Typed", "expand_command", "expand_with", "list_commands", "parse_command",
           "run_typed"]


@dataclass(frozen=True)
class Typed:
    """What a typed command stands for: the message to send, or with
    ``local`` the text to show the person (nothing is sent)."""

    text: str
    local: bool = False

log = logging.getLogger("noeta.commands")


def parse_command(text: str) -> tuple[str, str] | None:
    """``"/name args"`` as ``(name, args)``; ``None`` for other text."""
    if not text.startswith("/") or text.startswith("//"):
        return None
    name, _, args = text[1:].partition(" ")
    name = name.strip()
    return (name, args.strip()) if name else None


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
    extension of ``agent`` offers ``name``; empty for a local command (its
    output is for :func:`run_typed` callers). Raises what ``run_command``
    raises."""
    return await expand_with(agent.all_extensions(), ctx, text)


async def expand_with(extensions: Sequence[Extension], ctx: CommandContext,
                      text: str) -> str | None:
    """:func:`expand_command` over a task's extensions (``TaskView``): what a
    plugin that replays a typed command later (a scheduled prompt) uses."""
    typed = await run_typed(extensions, ctx, text)
    if typed is None:
        return None
    return "" if typed.local else typed.text


async def run_typed(extensions: Sequence[Extension], ctx: CommandContext,
                    text: str) -> Typed | None:
    """The command ``text`` names, run: the message it stands for, or a
    local command's output. ``None`` when no extension offers it."""
    parsed = parse_command(text)
    if parsed is None:
        return None
    name, args = parsed
    for ext, c in await _offered(extensions, ctx):
        if c.name == name:
            if c.template:
                return Typed(c.template.replace("{args}", args).strip(), c.local)
            return Typed(await ext.run_command(ctx, name, args), c.local)
    return None
