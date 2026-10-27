"""The worker side: claim tasks and run them."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from noeta.agent import Agent
from noeta.errors import NoetaError
from noeta.execenv.base import ExecEnv
from noeta.providers.base import Provider
from noeta.store.base import Claim, Store
from noeta.types import Delta

if TYPE_CHECKING:
    from noeta.loop.state import TaskState

log = logging.getLogger("noeta.runner")

ProviderFor = Callable[[str], Provider]
EnvFor = Callable[["TaskState"], ExecEnv]
OnDelta = Callable[[str, Delta], None]


def _default_env(state: TaskState) -> ExecEnv:
    from noeta.execenv.local import LocalExecEnv

    env: ExecEnv = LocalExecEnv(str(state.config.get("cwd") or os.getcwd()))
    return env


@dataclass
class Runtime:
    """Everything a runner needs. One ``Runtime`` runs many tasks at once."""

    store: Store
    agents: dict[str, Agent]
    provider_for: ProviderFor
    env_for: EnvFor = _default_env
    worker: str = field(default_factory=lambda: f"w-{uuid.uuid4().hex[:8]}")
    queues: Sequence[str] = ("default",)
    lease_seconds: float = 30.0
    poll_interval: float = 0.5
    concurrency: int = 8
    #: Directory for tool outputs too large to return inline.
    spill_root: str | None = None
    on_delta: OnDelta | None = None
    sleep: Callable[[float], Any] = asyncio.sleep

    async def run_once(self) -> bool:
        """Claim one task and run it until it waits or ends. ``False`` when
        nothing was claimable."""
        from noeta.runner.task import TaskRun

        claim = await self.store.claim(self.worker, self.queues, self.lease_seconds)
        if claim is None:
            return False
        await TaskRun(self, claim).run()
        return True

    async def run_until_idle(self) -> None:
        while await self.run_once():
            pass

    async def _claim(self) -> Claim | None:
        try:
            return await self.store.claim(self.worker, self.queues, self.lease_seconds)
        except (NoetaError, OSError) as exc:
            # A store outage must not end the worker; try again next tick.
            log.warning("claim failed: %s", exc)
            return None

    async def serve(self, stop: asyncio.Event) -> None:
        """Run tasks as they become claimable until ``stop`` is set."""
        from noeta.runner.task import TaskRun

        loop = asyncio.get_running_loop()
        wake = asyncio.Event()

        def nudge(_task: str, _kind: str) -> None:
            loop.call_soon_threadsafe(wake.set)

        unsubscribe = self.store.on_change(nudge)
        running: set[asyncio.Task[None]] = set()
        try:
            while not stop.is_set():
                while len(running) < self.concurrency:
                    claim = await self._claim()
                    if claim is None:
                        break
                    t = asyncio.create_task(TaskRun(self, claim).run())
                    running.add(t)
                    t.add_done_callback(running.discard)
                    t.add_done_callback(lambda _d: wake.set())
                wake.clear()
                waiters = [asyncio.create_task(wake.wait()), asyncio.create_task(stop.wait())]
                await asyncio.wait(waiters, timeout=self.poll_interval,
                                   return_when=asyncio.FIRST_COMPLETED)
                for w in waiters:
                    w.cancel()
        finally:
            unsubscribe()
            for t in running:
                t.cancel()
            await asyncio.gather(*running, return_exceptions=True)
