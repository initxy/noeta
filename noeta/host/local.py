"""Everything in one process: a store, a worker serving it in the background,
and a client — what the TUI, the ACP adapter and ``noeta -p`` run on. Live
model output reaches subscribers through ``deltas``.

A front end shares its store (``~/.noeta/tasks.db``) with every other noeta
process on the machine, so it runs with ``own_tasks_only=True``: its worker
serves only the task trees it created or resumed (``adopt``), never a task
another front end or a host left in the store."""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from noeta.core.agent import Agent
from noeta.core.channel import Channel
from noeta.core.client import Client, Input
from noeta.core.commands import Typed
from noeta.core.errors import NoetaError
from noeta.core.extension import Command, CommandContext
from noeta.core.provider import Provider
from noeta.core.runner import Runtime
from noeta.core.store import Store
from noeta.core.types import Delta
from noeta.host.app import AppAPI, DeltaSink, agent_commands, agent_run_command


def default_spill_root() -> str:
    """Where a tool's output too long to return inline is saved (one
    directory per task below it) when the host names no other place: the
    model gets a preview and the path, as the tools' descriptions promise."""
    user = getattr(os, "getuid", lambda: "user")()
    return os.path.join(tempfile.gettempdir(), f"noeta-{user}", "tool-results")


class UnknownAgent(NoetaError):
    """A resumed task runs an agent this front end does not have."""


class LocalClient(Client):
    """A ``Client`` whose new tasks the in-process worker serves.
    ``task_config`` is laid under every new task's config (its ``prompts``
    merged key by key)."""

    def __init__(self, store: Store, runtime: Runtime, *,
                 task_config: dict[str, Any] | None = None, **kw: Any) -> None:
        super().__init__(store, **kw)
        self.runtime = runtime
        self.task_config = dict(task_config or {})

    async def create(self, agent: str, input: Input | None = None, *,
                     task_id: str | None = None, config: dict[str, Any] | None = None,
                     **kw: Any) -> str:
        tid = task_id or uuid.uuid4().hex
        merged = {**self.task_config, **(config or {})}
        prompts = {**self.task_config.get("prompts", {}), **(config or {}).get("prompts", {})}
        if prompts:
            merged["prompts"] = prompts
        self.runtime.serve_root(tid)  # before it exists, so the first claim sees it
        created = await super().create(agent, input, task_id=tid, config=merged, **kw)
        if created != tid:  # a dedupe key named an existing task
            self.runtime.serve_root(created)
        return created


class LocalApp:
    def __init__(self, store: Store, agents: list[Agent],
                 provider: Provider | Callable[[str], Provider], *,
                 own_tasks_only: bool = False, task_config: dict[str, Any] | None = None,
                 channels: Sequence[Channel] = (), **runtime_kw: Any) -> None:
        self.store = store
        provider_for = provider if callable(provider) and not hasattr(provider, "complete") \
            else (lambda _m: provider)
        self._sinks: list[DeltaSink] = []
        if own_tasks_only:
            runtime_kw.setdefault("roots", set())
        runtime_kw.setdefault("spill_root", default_spill_root())
        self.runtime = Runtime(store=store, agents={a.name: a for a in agents},
                               provider_for=provider_for,  # type: ignore[arg-type]
                               on_delta=self._fan_out, poll_interval=0.2, **runtime_kw)
        self.client = LocalClient(store, self.runtime, task_config=task_config,
                                  poll_interval=0.2)
        #: Listeners letting outside conversations in (``noeta.core.channel``),
        #: run while the app is started.
        self.channels = list(channels)
        self._stop = asyncio.Event()
        self._serving: asyncio.Task[None] | None = None
        self._listening: asyncio.Task[None] | None = None
        #: What the person should know about how this app was set up (a
        #: project's plugins left unloaded); front ends show them.
        self.notices: list[str] = []
        #: Finds the plugins again and replaces the agents with ones made
        #: from them (``replace_agents``), returning notices for the person;
        #: set by a host that loads plugins from files (the ``noeta``
        #: command), ``None`` otherwise.
        self.reload_plugins: Callable[[], list[str]] | None = None

    def replace_agents(self, agents: list[Agent]) -> None:
        """Run ``agents`` from now on: every task picked up after this (the
        next turn of a waiting one) runs the new ones; a turn in progress
        finishes with the agent it started with."""
        self.runtime.agents.clear()
        self.runtime.agents.update({a.name: a for a in agents})

    def _fan_out(self, task_id: str, delta: Delta) -> None:
        for sink in list(self._sinks):
            sink(task_id, delta)

    def deltas(self, sink: DeltaSink) -> Callable[[], None]:
        self._sinks.append(sink)
        return lambda: self._sinks.remove(sink) if sink in self._sinks else None

    async def agent_names(self) -> list[str]:
        return sorted(self.runtime.agents)

    async def commands(self, agent: str, ctx: CommandContext) -> list[Command]:
        return await agent_commands(self.runtime.agents, agent, ctx)

    async def run_command(self, agent: str, ctx: CommandContext, text: str) -> Typed | None:
        return await agent_run_command(self.runtime.agents, agent, ctx, text)

    async def context(self, task_id: str) -> dict[str, Any] | None:
        from noeta.host.usage import context_breakdown

        preview = await self.runtime.preview(task_id)
        return context_breakdown(preview) if preview is not None else None

    async def adopt(self, task_id: str) -> None:
        """Serve the tree of an existing task (one being resumed). Raises
        :class:`UnknownAgent` when the task's agent is not one this app runs:
        its worker would end the task failed, so the task is left alone (a
        front end can still show its history)."""
        row = await self.store.get(task_id)
        root = await self.store.get(row.root_id) if row is not None else None
        for r in (row, root):
            if r is not None and r.agent not in self.runtime.agents:
                raise UnknownAgent(
                    f"task {r.task_id} runs agent {r.agent!r}, which this front end does not "
                    f"have (it has: {', '.join(sorted(self.runtime.agents)) or 'none'})")
        self.runtime.serve_root(row.root_id if row is not None else task_id)

    async def start(self, *, worker: bool = True) -> None:
        """Run the worker (unless ``worker`` is false: tasks run elsewhere)
        and the channels in the background until :meth:`stop`."""
        from noeta.host.channels import run_channels

        if worker and self._serving is None:
            self._serving = asyncio.create_task(self.runtime.serve(self._stop))
        if self.channels and self._listening is None:
            self._listening = asyncio.create_task(
                run_channels(self.channels, self.client, self._stop))

    async def stop(self) -> None:
        self._stop.set()
        for running in (self._listening, self._serving):
            if running is not None:
                await running
        self._serving = self._listening = None
        await self.store.close()

    async def __aenter__(self) -> LocalApp:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()


def _is_app(app: LocalApp) -> AppAPI:
    return app  # mypy checks LocalApp against AppAPI here
