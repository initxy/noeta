"""What every front end (the TUI, ACP, stream-json) runs on: the client
plus what only the side running the agents can answer — the agents' names,
their slash commands, and what fills a task's next request. ``LocalApp``
answers it in process, ``RemoteApp`` over HTTP from a ``noeta serve``
server, so a front end is the same code on one machine or against many."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from noeta.core import models
from noeta.core.agent import Agent
from noeta.core.client import ClientAPI
from noeta.core.commands import Typed, list_commands, run_typed
from noeta.core.extension import Command, CommandContext
from noeta.core.loop.state import CONFIG_SIGNAL
from noeta.core.types import Delta
from noeta.toolkit.signals import ARCHIVE_SIGNAL

DeltaSink = Callable[[str, Delta], None]

log = logging.getLogger("noeta.host.app")


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

    async def summaries(self, limit: int = 50, *,
                        archived: bool | None = None) -> list[dict[str, Any]]:
        """The root tasks to resume from, newest first
        (``noeta.host.summaries.root_summaries``): only the archived ones
        (``archived=True``), only the others (``False``) or all."""
        ...

    async def archive(self, task_id: str, archived: bool = True) -> None:
        """Archive the task (the session lists leave it out), or with
        ``archived=False`` bring it back (:func:`archive_task`)."""
        ...


@dataclass
class FrontEndStart:
    """What the ``noeta`` command hands a front end it opens: one found by
    name in the entry-point group ``noeta.frontends`` (``tui``), called
    ``run(app, start)`` with the :class:`AppAPI` it built, returning when
    the person quits."""

    #: The agent new tasks run.
    agent: str
    #: The directory the person started in (on this machine).
    cwd: str
    #: The model new tasks run, as the person reads it.
    model: str
    #: What each new task's config starts with (a server's own choices
    #: when empty).
    task_config: dict[str, Any] = field(default_factory=dict)
    #: A task to open; ``""``: let the person pick one; ``None``: a new one.
    resume: str | None = None
    #: Without ``resume``: open the newest task started in ``cwd`` (``-c``).
    continue_last: bool = False
    #: The permission mode new tasks start in (``None``: the plugin's default).
    permission_mode: str | None = None
    #: ``app`` is a server's: tasks run in its directories, not ``cwd``.
    on_server: bool = False
    #: The noeta home directory (``~/.noeta``: its settings and ``NOETA.md``).
    home: str = ""
    #: ``(name, value)`` rows of the settings in effect, for showing.
    settings: Callable[[], list[tuple[str, str]]] = lambda: []
    #: The installed noeta's version.
    version: str = ""
    #: Set when the project at ``cwd`` is not trusted: trusts it and loads
    #: what only a trusted project loads (the front end asks first).
    trust: Callable[[], None] | None = None


async def change_config(client: ClientAPI, task_id: str, changes: Mapping[str, Any]) -> None:
    """Change a task's config from its next model call on: ``model``,
    ``effort`` and the kernel's other changeable keys; ``None`` removes one."""
    await client.signal(task_id, CONFIG_SIGNAL, dict(changes))


async def archive_task(client: ClientAPI, task_id: str, archived: bool = True) -> None:
    """Post the ``archive`` signal: an ended task takes it too, since the
    session list reads it from the inbox (so it wakes no idle task)."""
    await client.signal(task_id, ARCHIVE_SIGNAL, {"archived": archived}, wake=False)


async def agent_commands(agents: Mapping[str, Agent], agent: str,
                         ctx: CommandContext) -> list[Command]:
    found = agents.get(agent)
    return await list_commands(found, ctx) if found is not None else []


async def agent_run_command(agents: Mapping[str, Agent], agent: str, ctx: CommandContext,
                            text: str) -> Typed | None:
    found = agents.get(agent)
    return await run_typed(found.all_extensions(), ctx, text) if found is not None else None


async def model_choices(app: Any, *, catalog: bool = True) -> list[tuple[str, str]]:
    """``(model, provider name)`` for the models a front end offers: what
    the app's providers serve (``LocalApp.list_models``), else (``catalog``)
    the catalog's, with no provider name."""
    lister = getattr(app, "list_models", None)
    found: list[tuple[str, str]] = []
    if lister is not None:
        try:
            found = await asyncio.to_thread(lister)
        except Exception:
            log.warning("listing the providers' models failed", exc_info=True)
    if found or not catalog:
        return found
    return [(m, "") for m in models.catalog_models()]


async def prepare_model(app: Any, model: str) -> str | None:
    """Settle ``model`` before a switch to it (``LocalApp.prepare_model``):
    what the person should know about it, or ``None``."""
    prepare = getattr(app, "prepare_model", None)
    if prepare is None:
        return models.unknown_model_notice(model)
    result: str | None = await asyncio.to_thread(prepare, model)
    return result
