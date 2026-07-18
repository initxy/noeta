"""Built-in slash commands for noeta-agent.

Public API:
    SlashCommand, BUILTIN_COMMANDS, get_command, list_commands — the registry.
    CommandResolution, resolve_command — resolving a command into a payload.
"""

from __future__ import annotations

from .registry import (
    BUILTIN_COMMANDS,
    SlashCommand,
    get_command,
    list_commands,
)
from .resolve import CommandResolution, resolve_command

__all__ = [
    "SlashCommand",
    "BUILTIN_COMMANDS",
    "get_command",
    "list_commands",
    "CommandResolution",
    "resolve_command",
]
