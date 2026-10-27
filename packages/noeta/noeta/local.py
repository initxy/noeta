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
from typing import Any

from noeta.agent import Agent
from noeta.client import Client, Input
from noeta.providers.base import Provider
from noeta.runner import Runtime
from noeta.store.base import Store
from noeta.types import Delta

DeltaSink = Callable[[str, Delta], None]


class LocalClient(Client):
    """A ``Client`` whose new tasks the in-process worker serves."""

    def __init__(self, store: Store, runtime: Runtime, **kw: Any) -> None:
        super().__init__(store, **kw)
        self.runtime = runtime

    async def create(self, agent: str, input: Input | None = None, *,
                     task_id: str | None = None, **kw: Any) -> str:
        tid = task_id or uuid.uuid4().hex
        self.runtime.serve_root(tid)  # before it exists, so the first claim sees it
        created = await super().create(agent, input, task_id=tid, **kw)
        if created != tid:  # a dedupe key named an existing task
            self.runtime.serve_root(created)
        return created


class LocalApp:
    def __init__(self, store: Store, agents: list[Agent],
                 provider: Provider | Callable[[str], Provider], *,
                 own_tasks_only: bool = False, **runtime_kw: Any) -> None:
        self.store = store
        provider_for = provider if callable(provider) and not hasattr(provider, "complete") \
            else (lambda _m: provider)
        self._sinks: list[DeltaSink] = []
        if own_tasks_only:
            runtime_kw.setdefault("roots", set())
        self.runtime = Runtime(store=store, agents={a.name: a for a in agents},
                               provider_for=provider_for,  # type: ignore[arg-type]
                               on_delta=self._fan_out, poll_interval=0.2, **runtime_kw)
        self.client = LocalClient(store, self.runtime, poll_interval=0.2)
        self._stop = asyncio.Event()
        self._serving: asyncio.Task[None] | None = None

    def _fan_out(self, task_id: str, delta: Delta) -> None:
        for sink in list(self._sinks):
            sink(task_id, delta)

    def deltas(self, sink: DeltaSink) -> Callable[[], None]:
        self._sinks.append(sink)
        return lambda: self._sinks.remove(sink) if sink in self._sinks else None

    async def adopt(self, task_id: str) -> None:
        """Serve the tree of an existing task (one being resumed)."""
        row = await self.store.get(task_id)
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
