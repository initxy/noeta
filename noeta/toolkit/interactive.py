"""Task config a front end sets about who answers the task: whether a
person follows it live (``interactive``), and whether a program answers its
questions and approvals instead (``permission_prompt_tool``). Claude Code
decides the same per process (print mode, ``--permission-prompt-tool``);
here it is per task, so every worker decides alike.

Absent keys mean an interactive task: the terminal UI, ACP and the HTTP API
set nothing; the headless protocol (``noeta -p``, ``/stream-json``) sets
``interactive: false``, and ``permission_prompt_tool: "stdio"`` when its
client answers ``can_use_tool`` requests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["INTERACTIVE_KEY", "PROMPT_TOOL_KEY", "can_ask", "interactive"]

INTERACTIVE_KEY = "interactive"
PROMPT_TOOL_KEY = "permission_prompt_tool"


def interactive(config: Mapping[str, Any]) -> bool:
    """Whether a person follows the task live."""
    return config.get(INTERACTIVE_KEY) is not False


def can_ask(config: Mapping[str, Any]) -> bool:
    """Whether a question the task asks can be answered: a person follows
    it, or its client answers prompts."""
    return interactive(config) or config.get(PROMPT_TOOL_KEY) not in (None, "", "none")
