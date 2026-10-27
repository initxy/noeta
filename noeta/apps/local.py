"""Everything in one process: a store, a worker serving it in the background,
and a client — what the TUI, the ACP adapter and ``noeta -p`` run on. Live
model output reaches subscribers through ``deltas``.

A front end shares its store (``~/.noeta/tasks.db``) with every other noeta
process on the machine, so it runs with ``own_tasks_only=True``: its worker
serves only the task trees it created or resumed (``adopt``), never a task
another front end or a host left in the store."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from noeta.core.agent import Agent
from noeta.core.client import Client, Input
from noeta.core.errors import NoetaError
from noeta.core.provider import Provider
from noeta.core.runner import Runtime
from noeta.core.store.base import Store
from noeta.core.types import Delta

if TYPE_CHECKING:
    from noeta.apps.config import Settings

DeltaSink = Callable[[str, Delta], None]


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
                 **runtime_kw: Any) -> None:
        self.store = store
        provider_for = provider if callable(provider) and not hasattr(provider, "complete") \
            else (lambda _m: provider)
        self._sinks: list[DeltaSink] = []
        if own_tasks_only:
            runtime_kw.setdefault("roots", set())
        self.runtime = Runtime(store=store, agents={a.name: a for a in agents},
                               provider_for=provider_for,  # type: ignore[arg-type]
                               on_delta=self._fan_out, poll_interval=0.2, **runtime_kw)
        self.client = LocalClient(store, self.runtime, task_config=task_config,
                                  poll_interval=0.2)
        self._stop = asyncio.Event()
        self._serving: asyncio.Task[None] | None = None

    def _fan_out(self, task_id: str, delta: Delta) -> None:
        for sink in list(self._sinks):
            sink(task_id, delta)

    def deltas(self, sink: DeltaSink) -> Callable[[], None]:
        self._sinks.append(sink)
        return lambda: self._sinks.remove(sink) if sink in self._sinks else None

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

    async def start(self) -> None:
        if self._serving is None:
            self._serving = asyncio.create_task(self.runtime.serve(self._stop))

    async def stop(self) -> None:
        self._stop.set()
        if self._serving is not None:
            await self._serving
            self._serving = None
        await self.store.close()

    async def __aenter__(self) -> LocalApp:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()


def from_settings(settings: Settings, *, agents_kw: dict[str, Any] | None = None,
                  own_tasks_only: bool = False, **runtime_kw: Any) -> LocalApp:
    """The default agents on the configured store, model and provider, with
    the user's memory (``~/.noeta/memory``) and prompt overrides
    (``~/.noeta/prompts``). Front ends (``-p``, ``acp``, the TUI) pass
    ``own_tasks_only`` so their worker never picks up another process's task
    from the shared store; ``worker`` and ``serve`` serve every task."""
    from noeta.apps.config import HOME, open_store
    from noeta.presets import default_agents

    kw: dict[str, Any] = {"memory_root": str(HOME / "memory"), **(agents_kw or {})}
    runtime_kw.setdefault("prompt_dirs", [str(HOME / "prompts")])
    return LocalApp(open_store(settings.store_url), default_agents(settings.model, **kw),
                    settings.make_provider(), own_tasks_only=own_tasks_only,
                    task_config={"prompts": settings.prompts} if settings.prompts else None,
                    **runtime_kw)
