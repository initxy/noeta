"""Everything in one process: a store, a worker serving it in the background,
and a client — what the TUI, the ACP adapter and ``noeta -p`` run on. Live
model output reaches subscribers through ``deltas``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from noeta.agent import Agent
from noeta.client import Client
from noeta.providers.base import Provider
from noeta.runner import Runtime
from noeta.store.base import Store
from noeta.types import Delta

DeltaSink = Callable[[str, Delta], None]


class LocalApp:
    def __init__(self, store: Store, agents: list[Agent],
                 provider: Provider | Callable[[str], Provider], **runtime_kw: Any) -> None:
        self.store = store
        self.client = Client(store, poll_interval=0.2)
        provider_for = provider if callable(provider) and not hasattr(provider, "complete") \
            else (lambda _m: provider)
        self._sinks: list[DeltaSink] = []
        self.runtime = Runtime(store=store, agents={a.name: a for a in agents},
                               provider_for=provider_for,  # type: ignore[arg-type]
                               on_delta=self._fan_out, poll_interval=0.2, **runtime_kw)
        self._stop = asyncio.Event()
        self._serving: asyncio.Task[None] | None = None

    def _fan_out(self, task_id: str, delta: Delta) -> None:
        for sink in list(self._sinks):
            sink(task_id, delta)

    def deltas(self, sink: DeltaSink) -> Callable[[], None]:
        self._sinks.append(sink)
        return lambda: self._sinks.remove(sink) if sink in self._sinks else None

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
