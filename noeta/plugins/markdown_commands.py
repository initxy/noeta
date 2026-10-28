"""Slash commands written as markdown (Claude Code's ``commands/*.md``): the
host reads the files and passes each command; typing ``/<name> <args>``
sends its body with ``$ARGUMENTS`` replaced by what was typed."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from noeta.core.extension import Command, CommandContext, Extension

PLACEHOLDER = "$ARGUMENTS"


class MarkdownCommands(Extension):
    """``commands``: one mapping per command — ``name``, ``body`` (the
    markdown after its frontmatter), and optionally ``description`` and
    ``argument_hint``."""

    name = "markdown_commands"

    def __init__(self, commands: Sequence[Mapping[str, Any]] = ()) -> None:
        self._commands: list[Command] = []
        for c in commands:
            body = str(c["body"]).strip()
            # Without the placeholder, what was typed follows the body.
            template = body.replace(PLACEHOLDER, "{args}") if PLACEHOLDER in body \
                else f"{body}\n\n{{args}}"
            self._commands.append(Command(
                name=str(c["name"]), template=template,
                description=str(c.get("description")
                                or (body.splitlines()[0][:80] if body else c["name"])),
                argument_hint=str(c.get("argument_hint") or "")))

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return self._commands
