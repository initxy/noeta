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
another front end or a host left in the store, and it creates its tasks on
:data:`FRONT_END_QUEUE`, which no ``noeta serve`` or ``noeta worker`` takes,
so they run only in the front end that has them open, with its settings."""

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
from noeta.core.loop.state import CONFIG_SIGNAL, MODEL_ROUTE, PLUGINS_OFF, off_names
from noeta.core.provider import Provider
from noeta.core.runner import Runtime
from noeta.core.store import Store
from noeta.core.types import Delta, Text
from noeta.host import plugins as host_plugins
from noeta.host.app import AppAPI, DeltaSink, agent_commands, agent_run_command, archive_task
from noeta.host.plugins import MarketEntry, PluginAdmin, PluginInfo, UiEntry
from noeta.host.rewind import RewindPoint, Rewound, rewind
from noeta.host.summaries import Cache, root_summaries

log = logging.getLogger("noeta.host.local")


def default_spill_root() -> str:
    """Where a tool's output too long to return inline is saved (one
    directory per task below it) when the host names no other place: the
    model gets a preview and the path, as the tools' descriptions promise."""
    user = getattr(os, "getuid", lambda: "user")()
    return os.path.join(tempfile.gettempdir(), f"noeta-{user}", "tool-results")


#: The queue a front end's tasks (and their sub-agents) are created on: a
#: worker serving the whole store takes ``default``, never this one.
FRONT_END_QUEUE = "front-end"
#: What an ``own_tasks_only`` worker takes: its own queue, and ``default`` for
#: the tasks front ends created before they had one.
FRONT_END_QUEUES = (FRONT_END_QUEUE, "default")


async def take_over(store: Store, task_id: str, queue: str = "default") -> bool:
    """Move the front end's task tree ``task_id`` belongs to onto ``queue``,
    so a server's workers run it from now on: what a server does when a
    person acts on such a task through it (their message would otherwise
    wait for a front end to open the task again). A front end that holds a
    task of the tree (a lease not run out) keeps it, and gets what was
    posted; ``False`` then, or when ``task_id`` is unknown."""
    row = await store.get(task_id)
    if row is None:
        return False
    return await store.requeue(row.root_id, FRONT_END_QUEUE, queue)

#: How long live output gathers before it is published to other processes.
DELTA_BATCH_SECONDS = 0.03


class UnknownAgent(NoetaError):
    """A resumed task runs an agent this front end does not have."""


class LocalClient(Client):
    """A ``Client`` whose new tasks the in-process worker serves.
    ``task_config`` is laid under every new task's config (its ``prompts``
    merged key by key); ``queue`` is the one new tasks are created on unless
    a call names one. The plugins :attr:`default_off` names join the
    ``plugins_off`` of every new task that continues no other."""

    def __init__(self, store: Store, runtime: Runtime, *,
                 task_config: dict[str, Any] | None = None, queue: str = "default",
                 **kw: Any) -> None:
        super().__init__(store, **kw)
        self.runtime = runtime
        self.task_config = dict(task_config or {})
        self.queue = queue
        #: ``route_model(model)``: what a task records with a model it is
        #: given (``MODEL_ROUTE``: its provider, its limits), or ``None``;
        #: blocking. Set by a host with several providers.
        self.route_model: Callable[[str], dict[str, Any] | None] | None = None
        #: ``default_off()``: the plugins new tasks start without (the
        #: machine's switches); blocking.
        self.default_off: Callable[[], list[str]] = lambda: []

    async def _route(self, model: Any) -> dict[str, Any] | None:
        if self.route_model is None or not isinstance(model, str) or not model:
            return None
        return await asyncio.to_thread(self.route_model, model)

    async def signal(self, task_id: str, name: str, data: Any = None, *,
                     dedupe_key: str | None = None, wake: bool = True) -> int:
        """A model switch records where the model goes with it."""
        if name == CONFIG_SIGNAL and isinstance(data, dict) and "model" in data \
                and MODEL_ROUTE not in data and self.route_model is not None:
            route = await self._route(data["model"])
            if route is not None:
                data = {**data, MODEL_ROUTE: route}
        return await super().signal(task_id, name, data, dedupe_key=dedupe_key, wake=wake)

    async def create(self, agent: str, input: Input | None = None, *,
                     task_id: str | None = None, config: dict[str, Any] | None = None,
                     **kw: Any) -> str:
        tid = task_id or uuid.uuid4().hex
        merged = {**self.task_config, **(config or {})}
        prompts = {**self.task_config.get("prompts", {}), **(config or {}).get("prompts", {})}
        if prompts:
            merged["prompts"] = prompts
        # A conversation continued (a fork, a rewind) keeps the switches it
        # is given.
        default = [] if kw.get("history_from") else [
            n for n in await asyncio.to_thread(self.default_off) if host_plugins.switchable(n)]
        if default:
            merged[PLUGINS_OFF] = sorted({*off_names(merged), *default})
        if self.route_model is not None and MODEL_ROUTE not in merged:
            found = self.runtime.agent(agent)
            route = await self._route(merged.get("model") or (found.model if found else ""))
            if route is not None:
                merged[MODEL_ROUTE] = route
        if not kw.get("history_from"):
            input = self._initial_prompt(agent, input)
        kw.setdefault("queue", self.queue)
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
            runtime_kw.setdefault("queues", FRONT_END_QUEUES)
        runtime_kw.setdefault("spill_root", default_spill_root())
        runtime_kw.setdefault("agent_aliases", former_agent_names([a.name for a in agents]))
        runtime_kw.setdefault("poll_interval", 0.2)
        self.runtime = Runtime(store=store, agents={a.name: a for a in agents},
                               provider_for=provider_for,  # type: ignore[arg-type]
                               on_delta=self._fan_out, **runtime_kw)
        self.client = LocalClient(store, self.runtime, task_config=task_config,
                                  queue=FRONT_END_QUEUE if own_tasks_only else "default",
                                  poll_interval=runtime_kw["poll_interval"])
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
        #: The machine's plugins as the ``noeta`` command lets front ends
        #: manage them (switches for new tasks, install, remove, the
        #: marketplaces); the default can do none of that.
        self.plugin_admin = PluginAdmin()
        self.client.default_off = lambda: self.plugin_admin.default_off()
        self._summaries: Cache = {}

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
        return await agent_commands(self.runtime.agents, self._named(agent), ctx,
                                    await self._off(ctx))

    async def run_command(self, agent: str, ctx: CommandContext, text: str) -> Typed | None:
        return await agent_run_command(self.runtime.agents, self._named(agent), ctx, text,
                                       await self._off(ctx))

    async def _off(self, ctx: CommandContext) -> list[str]:
        """The plugins ``ctx``'s task runs without (none before it has one)."""
        if not ctx.task_id or await self.store.get(ctx.task_id) is None:
            return []
        return await host_plugins.task_off(self.store, ctx.task_id)

    async def plugins(self, agent: str, cwd: str = "") -> list[PluginInfo]:
        admin = self.plugin_admin
        return await asyncio.to_thread(host_plugins.plugin_infos,
                                       self.runtime.agent(agent), admin, cwd)

    async def set_plugin_default(self, name: str, enabled: bool) -> None:
        if not host_plugins.switchable(name):
            raise ValueError(f"every task runs the plugin {name!r}: it cannot be switched off")
        if self.plugin_admin.set_default is None:
            raise NotImplementedError("this host keeps no plugin switches")
        await asyncio.to_thread(self.plugin_admin.set_default, name, enabled)

    async def ui(self, task_id: str) -> list[UiEntry]:
        return await host_plugins.task_ui(self.runtime, task_id)

    async def plugin_call(self, task_id: str, plugin: str, name: str, args: Any) -> Any:
        return await host_plugins.call_plugin(self.runtime, task_id, plugin, name, args)

    async def install_plugin(self, source: str, marketplace: str | None = None,
                             commit: str | None = None) -> PluginInfo:
        install = self.plugin_admin.install
        if install is None:
            raise NotImplementedError("this host does not install plugins")
        name = await asyncio.to_thread(install, source, marketplace, commit)
        self._reload()
        found = next((p for p in await self.plugins(self.main_agent) if p["name"] == name),
                     None)
        return found or PluginInfo(name=name, version="", description="", source=source,
                                   builtin=False, enabled=False, switchable=False,
                                   broken="installed; it joins no agent", parts=[],
                                   ui_api=None, web=None)

    async def remove_plugin(self, name: str) -> None:
        remove = self.plugin_admin.remove
        if remove is None:
            raise NotImplementedError("this host does not remove plugins")
        await asyncio.to_thread(remove, name)
        self._reload()

    async def market(self) -> list[MarketEntry]:
        market = self.plugin_admin.market
        return await asyncio.to_thread(market) if market is not None else []

    def _reload(self) -> None:
        """Load the plugins again (``/reload-plugins``) after an install."""
        if self.reload_plugins is not None:
            for line in self.reload_plugins():
                log.info("plugins: %s", line)

    def _named(self, agent: str) -> str:
        """``agent`` as named now (a task's log may hold a former name)."""
        found = self.runtime.agent(agent)
        return found.name if found is not None else agent

    async def context(self, task_id: str) -> dict[str, Any] | None:
        from noeta.host.usage import task_context

        return await task_context(self.runtime, task_id)

    async def summaries(self, limit: int = 50, *,
                        archived: bool | None = None) -> list[dict[str, Any]]:
        return await root_summaries(self.client, limit, self._summaries,
                                    inbox=self.store.inbox, archived=archived)

    async def archive(self, task_id: str, archived: bool = True) -> None:
        await archive_task(self.client, task_id, archived)

    async def rewind(self, task_id: str, point: RewindPoint, *, conversation: bool = True,
                     files: bool = True) -> Rewound:
        return await rewind(self.client, task_id, point, conversation=conversation,
                            files=files)

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

    async def run_until(self, stop: asyncio.Event) -> None:
        """:meth:`start` the worker and the channels, run until ``stop`` is
        set (or the worker fails, which is raised), then :meth:`stop`: a
        process that only runs tasks (``noeta worker``)."""
        await self.start()
        assert self._serving is not None
        stopping = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({stopping, self._serving}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            await self.stop()

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
