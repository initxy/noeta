"""Slash commands written as markdown (Claude Code's ``commands/*.md``): the
host reads the files and passes each command; typing ``/<name> <args>``
sends its body with the argument placeholders filled as Claude Code fills
them (:func:`noeta.toolkit.arguments.substitute_arguments`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from noeta.core.extension import Command, CommandContext, Extension
from noeta.toolkit.arguments import substitute_arguments


class MarkdownCommands(Extension):
    """``commands``: one mapping per command — ``name``, ``body`` (the
    markdown after its frontmatter), and optionally ``description`` and
    ``argument_hint``."""

    name = "markdown_commands"

    def __init__(self, commands: Sequence[Mapping[str, Any]] = ()) -> None:
        self._commands: list[Command] = []
        self._bodies: dict[str, str] = {}
        for c in commands:
            body = str(c["body"]).strip()
            self._bodies[str(c["name"])] = body
            self._commands.append(Command(
                name=str(c["name"]),
                description=str(c.get("description")
                                or (body.splitlines()[0][:80] if body else c["name"])),
                argument_hint=str(c.get("argument_hint") or "")))

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return self._commands

    async def run_command(self, ctx: CommandContext, name: str, args: str) -> str:
        return substitute_arguments(self._bodies[name], args).strip()
