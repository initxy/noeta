"""Everything in one process: a store, a worker serving it in the background,
and a client — what the TUI, the ACP adapter and ``noeta -p`` run on. Live
model output reaches subscribers through ``deltas``.

A worker publishes its live output through the store when the store can
carry it to other processes (Postgres: ``publish_deltas``), and ``deltas``
hears what workers elsewhere publish (``on_delta``), so a front end attached
to one server streams the text of a task another machine runs. On a store
without that (sqlite) live output stays in its process; the recorded events
still reach everyone.

A front end shares its store (``~/.noeta/tasks.db``) with every other noeta
process on the machine, so it runs with ``own_tasks_only=True``: its worker
serves only the task trees it created or resumed (``adopt``), never a task
another front end or a host left in the store."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from collections.abc import Callable, Sequence
from typing import Any

import msgspec

from noeta.core.agent import Agent
from noeta.core.channel import Channel
from noeta.core.client import Client, Input
from noeta.core.commands import Typed
from noeta.core.errors import NoetaError
from noeta.core.extension import Command, CommandContext
from noeta.core.loop.state import CONFIG_SIGNAL, MODEL_ROUTE
from noeta.core.provider import Provider
from noeta.core.runner import Runtime
from noeta.core.store import Store
from noeta.core.types import Delta, Text
from noeta.host.app import AppAPI, DeltaSink, agent_commands, agent_run_command

log = logging.getLogger("noeta.host.local")


def default_spill_root() -> str:
    """Where a tool's output too long to return inline is saved (one
    directory per task below it) when the host names no other place: the
    model gets a preview and the path, as the tools' descriptions promise."""
    user = getattr(os, "getuid", lambda: "user")()
    return os.path.join(tempfile.gettempdir(), f"noeta-{user}", "tool-results")


#: How long live output gathers before it is published to other processes.
DELTA_BATCH_SECONDS = 0.03


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
        #: ``route_model(model)``: what a task records with a model it is
        #: given (``MODEL_ROUTE``: its provider, its limits), or ``None``;
        #: blocking. Set by a host with several providers.
        self.route_model: Callable[[str], dict[str, Any] | None] | None = None

    async def _route(self, model: Any) -> dict[str, Any] | None:
        if self.route_model is None or not isinstance(model, str) or not model:
            return None
        return await asyncio.to_thread(self.route_model, model)

    async def signal(self, task_id: str, name: str, data: Any = None, *,
                     dedupe_key: str | None = None) -> int:
        """A model switch records where the model goes with it."""
        if name == CONFIG_SIGNAL and isinstance(data, dict) and "model" in data \
                and MODEL_ROUTE not in data and self.route_model is not None:
            route = await self._route(data["model"])
            if route is not None:
                data = {**data, MODEL_ROUTE: route}
        return await super().signal(task_id, name, data, dedupe_key=dedupe_key)

    async def create(self, agent: str, input: Input | None = None, *,
                     task_id: str | None = None, config: dict[str, Any] | None = None,
                     **kw: Any) -> str:
        tid = task_id or uuid.uuid4().hex
        merged = {**self.task_config, **(config or {})}
        prompts = {**self.task_config.get("prompts", {}), **(config or {}).get("prompts", {})}
        if prompts:
            merged["prompts"] = prompts
        if self.route_model is not None and MODEL_ROUTE not in merged:
            found = self.runtime.agent(agent)
            route = await self._route(merged.get("model") or (found.model if found else ""))
            if route is not None:
                merged[MODEL_ROUTE] = route
        if not kw.get("history_from"):
            input = self._initial_prompt(agent, input)
        self.runtime.serve_root(tid)  # before it exists, so the first claim sees it
        created = await super().create(agent, input, task_id=tid, config=merged, **kw)
        if created != tid:  # a dedupe key named an existing task
            self.runtime.serve_root(created)
        return created

    def _initial_prompt(self, agent: str, input: Input | None) -> Input | None:
        """An agent file's ``initialPrompt`` (Claude Code's): the first
        message of a new top-level conversation with that agent, ahead of
        what the person sent with it. Sub-agents never read it."""
        found = self.runtime.agent(agent)
        initial = found.extra.get("initialPrompt") if found is not None else None
        if not isinstance(initial, str) or not initial.strip():
            return input
        if input is None:
            return initial
        if isinstance(input, str):
            return f"{initial}\n\n{input}" if input else initial
        if input.role == "user" and input.origin == "human":
            return msgspec.structs.replace(input, content=[Text(text=initial), *input.content])
        return input


def former_agent_names(names: Sequence[str]) -> dict[str, str]:
    """``<agent>`` -> ``<plugin>:<agent>`` for each Claude Code plugin agent
    whose bare name no other agent has: the name tasks made before plugin
    agents were named so recorded (``Runtime.agent_aliases``)."""
    owners: dict[str, list[str]] = {}
    for name in names:
        parts = name.split(":")
        if len(parts) == 2 and all(parts):
            owners.setdefault(parts[1], []).append(name)
    return {bare: found[0] for bare, found in owners.items()
            if len(found) == 1 and bare not in names}


class LocalApp:
    def __init__(self, store: Store, agents: list[Agent],
                 provider: Provider | Callable[[str], Provider], *,
                 own_tasks_only: bool = False, task_config: dict[str, Any] | None = None,
                 channels: Sequence[Channel] = (), **runtime_kw: Any) -> None:
        self.store = store
        provider_for = provider if callable(provider) and not hasattr(provider, "complete") \
            else (lambda _m: provider)
        self._sinks: list[DeltaSink] = []
        self._outgoing: list[tuple[str, Delta]] = []
        self._publishing: asyncio.Task[None] | None = None
        self._hearing: Callable[[], None] | None = None
        if own_tasks_only:
            runtime_kw.setdefault("roots", set())
        runtime_kw.setdefault("spill_root", default_spill_root())
        runtime_kw.setdefault("agent_aliases", former_agent_names([a.name for a in agents]))
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
        #: Removes ended tasks' old saved tool outputs (``spill_sweep``).
        self._sweeping: asyncio.Task[None] | None = None
        #: What the person should know about how this app was set up (a
        #: project's plugins left unloaded); front ends show them.
        self.notices: list[str] = []
        #: The agent a front end starts new tasks with unless told another
        #: (the settings' ``agent``).
        self.main_agent = "main"
        #: Finds the plugins again and replaces the agents with ones made
        #: from them (``replace_agents``), returning notices for the person;
        #: set by a host that loads plugins from files (the ``noeta``
        #: command), ``None`` otherwise.
        self.reload_plugins: Callable[[], list[str]] | None = None
        #: ``(model, provider name)`` for the models every configured
        #: provider offers; ``None`` when only the catalog is known. May
        #: read the providers' listings: call it off the event loop.
        self.list_models: Callable[[], list[tuple[str, str]]] | None = None
        #: Settle a model before a switch to it (its provider, its limits;
        #: the client records them, ``LocalClient.route_model``),
        #: returning what the person should know (an unknown model) or
        #: ``None``; ``None`` when only the catalog is known. Off the loop.
        self.prepare_model: Callable[[str], str | None] | None = None

    def replace_agents(self, agents: list[Agent]) -> None:
        """Run ``agents`` from now on: every task picked up after this (the
        next turn of a waiting one) runs the new ones; a turn in progress
        finishes with the agent it started with."""
        self.runtime.agents.clear()
        self.runtime.agents.update({a.name: a for a in agents})
        self.runtime.agent_aliases.clear()
        self.runtime.agent_aliases.update(former_agent_names([a.name for a in agents]))

    def _fan_out(self, task_id: str, delta: Delta) -> None:
        self._deliver(task_id, delta)
        if getattr(self.store, "publish_deltas", None) is not None:
            self._outgoing.append((task_id, delta))
            if self._publishing is None or self._publishing.done():
                self._publishing = asyncio.get_running_loop().create_task(self._publish())

    def _deliver(self, task_id: str, delta: Delta) -> None:
        for sink in list(self._sinks):
            sink(task_id, delta)

    async def _publish(self) -> None:
        """Send the output gathered over a short while in one go."""
        while self._outgoing:
            await asyncio.sleep(DELTA_BATCH_SECONDS)
            batch, self._outgoing = self._outgoing, []
            try:
                await self.store.publish_deltas(batch)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 — live output is best effort
                log.debug("publishing live output failed: %s", exc)

    def deltas(self, sink: DeltaSink) -> Callable[[], None]:
        self._sinks.append(sink)
        hear = getattr(self.store, "on_delta", None)
        if self._hearing is None and hear is not None:
            self._hearing = hear(self._deliver)
        return lambda: self._sinks.remove(sink) if sink in self._sinks else None

    async def agent_names(self) -> list[str]:
        return sorted(self.runtime.agents)

    async def commands(self, agent: str, ctx: CommandContext) -> list[Command]:
        return await agent_commands(self.runtime.agents, self._named(agent), ctx)

    async def run_command(self, agent: str, ctx: CommandContext, text: str) -> Typed | None:
        return await agent_run_command(self.runtime.agents, self._named(agent), ctx, text)

    def _named(self, agent: str) -> str:
        """``agent`` as named now (a task's log may hold a former name)."""
        found = self.runtime.agent(agent)
        return found.name if found is not None else agent

    async def context(self, task_id: str) -> dict[str, Any] | None:
        from noeta.host.usage import task_context

        return await task_context(self.runtime, task_id)

    async def adopt(self, task_id: str) -> None:
        """Serve the tree of an existing task (one being resumed). Raises
        :class:`UnknownAgent` when the task's agent is not one this app runs:
        its worker would end the task failed, so the task is left alone (a
        front end can still show its history)."""
        row = await self.store.get(task_id)
        root = await self.store.get(row.root_id) if row is not None else None
        for r in (row, root):
            if r is not None and self.runtime.agent(r.agent) is None:
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
            if self.runtime.spill_root and self._sweeping is None:
                from noeta.host.spill_sweep import keep_spills_swept

                self._sweeping = asyncio.create_task(
                    keep_spills_swept(self.store, self.runtime.spill_root, self._stop))
        if self.channels and self._listening is None:
            self._listening = asyncio.create_task(
                run_channels(self.channels, self.client, self._stop))

    async def stop(self) -> None:
        self._stop.set()
        for running in (self._listening, self._serving, self._publishing, self._sweeping):
            if running is not None:
                await running
        self._serving = self._listening = self._publishing = self._sweeping = None
        if self._hearing is not None:
            self._hearing()
            self._hearing = None
        await self.store.close()

    async def __aenter__(self) -> LocalApp:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()


def _is_app(app: LocalApp) -> AppAPI:
    return app  # mypy checks LocalApp against AppAPI here
