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
terminal UI and ACP send :data:`FOLLOWED_CONFIG` to the task and every
open task below it when they reopen it (:func:`follow`; child tasks carry
copies), as a Claude Code session resumed in its terminal is interactive
whatever ran it before. An HTTP client sets the keys itself (a config
signal). A headless run that exits leaves its record: until a front end
reopens the task, a worker without the client refuses what the client's
callbacks would judge (``noeta.toolkit.hook_callbacks``).

:data:`BASH_MODE_KEY` says the task's front end runs a person's ``!``
commands (Claude Code's bash mode; only the terminal UI does, and not
attached to a server, where the task's files are): set once, by
the front end that creates the task, so every request of the task is built
alike, and not copied to sub-agents (a fork keeps it with its parent's
prompt)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from noeta.core.loop.state import CONFIG_SIGNAL
from noeta.core.store import TaskRow
from noeta.toolkit.hook_callbacks import CLIENT_HOOKS_KEY

__all__ = ["BASH_MODE_KEY", "FOLLOWED_CONFIG", "INTERACTIVE_KEY", "PROMPT_TOOL_KEY", "can_ask",
           "follow", "interactive"]

INTERACTIVE_KEY = "interactive"
PROMPT_TOOL_KEY = "permission_prompt_tool"
BASH_MODE_KEY = "bash_mode"
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


class _Tree(Protocol):
    async def tasks(self, *, parent_id: str | None = None, status: Any = None,
                    limit: int = 100) -> list[TaskRow]: ...

    async def signal(self, task_id: str, name: str, data: Any = None, *,
                     dedupe_key: str | None = None) -> int: ...


async def follow(client: _Tree, task_id: str) -> None:
    """A person follows ``task_id`` now: :data:`FOLLOWED_CONFIG` to it and
    to every task below it that has not ended (they carry copies of the
    keys)."""
    todo = [task_id]
    while todo:
        tid = todo.pop()
        await client.signal(tid, CONFIG_SIGNAL, dict(FOLLOWED_CONFIG))
        for status in ("ready", "running", "waiting"):
            todo.extend(row.task_id for row in await client.tasks(parent_id=tid, status=status,
                                                                  limit=1000))

