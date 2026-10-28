"""What every front end (the TUI, ACP, stream-json) runs on: the client
plus what only the side running the agents can answer — the agents' names,
their slash commands, and what fills a task's next request. ``LocalApp``
answers it in process, ``RemoteApp`` over HTTP from a ``noeta serve``
server, so a front end is the same code on one machine or against many."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from noeta.core.agent import Agent
from noeta.core.client import ClientAPI
from noeta.core.commands import Typed, list_commands, run_typed
from noeta.core.extension import Command, CommandContext
from noeta.core.types import Delta

DeltaSink = Callable[[str, Delta], None]


class AppAPI(Protocol):
    @property
    def client(self) -> ClientAPI: ...

    def deltas(self, sink: DeltaSink) -> Callable[[], None]:
        """Call ``sink(task_id, delta)`` with live model output; returns the
        unsubscribe function."""
        ...

    async def adopt(self, task_id: str) -> None:
        """Make sure the tree of an existing task is served (a resume)."""
        ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def agent_names(self) -> list[str]: ...

    async def commands(self, agent: str, ctx: CommandContext) -> list[Command]:
        """The slash commands ``agent``'s plugins offer (none for an agent
        this side does not run)."""
        ...

    async def run_command(self, agent: str, ctx: CommandContext, text: str) -> Typed | None:
        """``text`` (``/name args``) run as ``agent``'s command; ``None``
        when no plugin of it offers that name."""
        ...

    async def context(self, task_id: str) -> dict[str, Any] | None:
        """What fills the task's next request
        (``noeta.host.usage.context_breakdown``); ``None`` when this side
        does not run the task's agent."""
        ...


async def agent_commands(agents: Mapping[str, Agent], agent: str,
                         ctx: CommandContext) -> list[Command]:
    found = agents.get(agent)
    return await list_commands(found, ctx) if found is not None else []


async def agent_run_command(agents: Mapping[str, Agent], agent: str, ctx: CommandContext,
                            text: str) -> Typed | None:
    found = agents.get(agent)
    return await run_typed(found.all_extensions(), ctx, text) if found is not None else None
