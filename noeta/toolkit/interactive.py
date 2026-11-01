"""Task config a front end sets about who answers the task: whether a
person follows it live (``interactive``), and whether a program answers its
questions and approvals instead (``permission_prompt_tool``). Claude Code
decides the same per process (print mode, ``--permission-prompt-tool``);
here it is per task, so every worker decides alike.

Absent keys mean an interactive task: the terminal UI, ACP and the HTTP API
set nothing; the headless protocol (``noeta -p``, ``/stream-json``) sets
``interactive: false``, and ``permission_prompt_tool: "stdio"`` when its
client answers ``can_use_tool`` requests.

The keys stay in the task's config, the only place every worker that runs
one of its steps reads, so whoever takes the task over says again who
follows it: a headless run sets its own values on ``--resume``, and the
terminal UI and ACP send :data:`FOLLOWED_CONFIG` when they reopen a task
(as a Claude Code session resumed in its terminal is interactive whatever
ran it before). An HTTP client sets the keys itself (a config signal)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from noeta.toolkit.hook_callbacks import CLIENT_HOOKS_KEY

__all__ = ["FOLLOWED_CONFIG", "INTERACTIVE_KEY", "PROMPT_TOOL_KEY", "can_ask", "interactive"]

INTERACTIVE_KEY = "interactive"
PROMPT_TOOL_KEY = "permission_prompt_tool"
#: The config signal's data a front end a person follows sends when it
#: reopens a task: a headless run's say on who answers, and its client's
#: hook callbacks, taken away.
FOLLOWED_CONFIG: Mapping[str, None] = {INTERACTIVE_KEY: None, PROMPT_TOOL_KEY: None,
                                       CLIENT_HOOKS_KEY: None}


def interactive(config: Mapping[str, Any]) -> bool:
    """Whether a person follows the task live."""
    return config.get(INTERACTIVE_KEY) is not False


def can_ask(config: Mapping[str, Any]) -> bool:
    """Whether a question the task asks can be answered: a person follows
    it, or its client answers prompts."""
    return interactive(config) or config.get(PROMPT_TOOL_KEY) not in (None, "", "none")
