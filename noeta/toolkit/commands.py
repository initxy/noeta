"""Claude Code's tags for a typed slash command in the person's turn."""

from __future__ import annotations

import re

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
