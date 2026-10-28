"""The worker side: claim tasks and run them."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from noeta.core.agent import Agent
from noeta.core.execenv import ExecEnv
from noeta.core.provider import Provider
from noeta.core.store import Claim, Store
from noeta.core.types import Delta, ModelRequest

if TYPE_CHECKING:
    from noeta.core.loop.state import TaskState

log = logging.getLogger("noeta.core.runner")

ProviderFor = Callable[[str], Provider]
EnvFor = Callable[["TaskState"], ExecEnv]
OnDelta = Callable[[str, Delta], None]


def _default_env(state: TaskState) -> ExecEnv:
    from noeta.core import registry

    return registry.execenv("local", str(state.config.get("cwd") or os.getcwd()))


def catalog_output_ceiling(model: str) -> int:
    """``model``'s output ceiling in the model catalog
    (:mod:`noeta.core.models`; an unknown model gets the unknown-model cap)."""
    from noeta.core import models

    spec = models.find_spec(model)
    return spec.max_output_tokens if spec is not None else models.UNKNOWN_MODEL_MAX_OUTPUT_TOKENS


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
    #: Directory for tool outputs too large to return inline; ``None`` cuts
    #: them instead (built-in hosts always set one).
    spill_root: str | None = None
    #: Host directories of prompt override files (``noeta.core.prompts``),
    #: e.g. ``~/.noeta/prompts``; the task's ``<cwd>/.noeta/prompts`` is
    #: read too when :attr:`workspace_trusted` allows it.
    prompt_dirs: Sequence[str] = ()
    #: ``workspace_trusted(cwd)``: may a task's workspace change what the
    #: model is told (its ``.noeta/prompts``)? ``None`` trusts every workspace.
    workspace_trusted: Callable[[str], bool] | None = None
    on_delta: OnDelta | None = None
    #: ``output_ceiling(model)``: the ``max_tokens`` a request sends when its
    #: agent sets none (always sent: a gateway once defaulted to 1,000 when
    #: it was left out). A host whose models the catalog does not know
    #: passes its own.
    output_ceiling: Callable[[str], int] = catalog_output_ceiling
    sleep: Callable[[float], Any] = asyncio.sleep
    #: Serve only the task trees whose root ids are in this set (a front end
    #: adds each task it creates or resumes); ``None`` serves every task.
    roots: set[str] | None = None
    _wake: asyncio.Event | None = field(default=None, init=False, repr=False)
    #: Holders per held tree root (:meth:`hold`).
    _holds: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _renewer: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    @property
    def hold_seconds(self) -> float:
        """How long a hold outlives its last renewal: a dead worker's trees
        go back to the others after this."""
        return 2 * self.lease_seconds

    async def hold(self, root_id: str) -> Callable[[], Awaitable[None]]:
        """Keep the tree of ``root_id`` with this worker (no other worker
        claims its tasks) until the returned release is awaited; holds are
        counted, renewed while this runtime lives, and run out once it is
        gone. For what lives only in this process, e.g. a background job."""
        count = self._holds.get(root_id, 0)
        self._holds[root_id] = count + 1
        if count == 0:
            await self.store.hold(root_id, self.worker, self.hold_seconds)
        if self._renewer is None or self._renewer.done():
            self._renewer = asyncio.create_task(self._renew_holds())
        released = False

        async def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            if root_id not in self._holds:
                return  # dropped on the way out (_drop_holds), store maybe closed
            left = self._holds[root_id] - 1
            if left > 0:
                self._holds[root_id] = left
                return
            self._holds.pop(root_id, None)
            try:
                await self.store.unhold(root_id, self.worker)
            except Exception as exc:  # noqa: BLE001 — the hold runs out by itself
                log.warning("tree %s: unhold failed: %s", root_id, exc)

        return release

    async def _renew_holds(self) -> None:
        while self._holds:
            # Real time, never ``self.sleep`` (tests replace that with no
            # wait): a hold must outlive the worker by wall-clock seconds.
            await asyncio.sleep(self.lease_seconds / 3)
            for root_id in list(self._holds):
                try:
                    if not await self.store.hold(root_id, self.worker, self.hold_seconds):
                        log.warning("tree %s: hold taken over by another worker", root_id)
                except Exception as exc:  # noqa: BLE001 — retried on the next round
                    log.warning("tree %s: renewing the hold failed: %s", root_id, exc)

    async def _claim_once(self) -> Claim | None:
        roots = sorted(self.roots) if self.roots is not None else None
        return await self.store.claim(self.worker, self.queues, self.lease_seconds,
                                      roots=roots)

    async def run_claim(self, claim: Claim) -> None:
        """Run one claim until the task waits or ends. Cancelled (a
        shutdown), it hands the task back at once instead of leaving it
        ``running`` until the lease runs out."""
        from noeta.core.runner.task import TaskRun

        run = TaskRun(self, claim)
        try:
            await run.run()
        except asyncio.CancelledError:
            await self._release(run.claim)
            raise

    async def _release(self, claim: Claim) -> None:
        try:
            await self.store.release(claim)
        except Exception as exc:  # noqa: BLE001 — the lease covers it
            log.warning("task %s: release failed: %s", claim.task_id, exc)

    async def run_once(self) -> bool:
        """Claim one task and run it until it waits or ends. ``False`` when
        nothing was claimable."""
        claim = await self._claim_once()
        if claim is None:
            return False
        await self.run_claim(claim)
        return True

    async def preview(self, task_id: str) -> ModelRequest | None:
        """The request the task's next model call would start from (its
        log folded, system prompt, tools and messages; no reminder or
        ``before_model`` is run and nothing is recorded), for a front end
        that shows what fills the context. ``None`` when this runtime does
        not have the task's agent."""
        from noeta.core.runner.model import build_model_request
        from noeta.core.runner.task import TaskRun

        run = TaskRun(self, Claim(task_id=task_id, worker=self.worker, epoch=0,
                                  lease_until=0.0))
        await run.load()
        if run.agent is None:
            return None
        return build_model_request(run, final=False)

    def serve_root(self, root_id: str) -> None:
        """Add a task tree to ``roots`` (when this runtime is scoped) and
        look for claimable work at once."""
        if self.roots is not None:
            self.roots.add(root_id)
        if self._wake is not None:
            self._wake.set()

    async def run_until_idle(self) -> None:
        while await self.run_once():
            pass

    async def _claim(self) -> Claim | None:
        try:
            return await self._claim_once()
        except Exception as exc:  # noqa: BLE001 — a store outage must not end the worker
            log.warning("claim failed: %s", exc)
            return None

    async def serve(self, stop: asyncio.Event) -> None:
        """Run tasks as they become claimable until ``stop`` is set. On the
        way out, running tasks are cancelled and handed back."""
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()

        def nudge(_task: str, _kind: str) -> None:
            loop.call_soon_threadsafe(wake.set)

        self._wake = wake
        unsubscribe = self.store.on_change(nudge)
        running: set[asyncio.Task[None]] = set()
        try:
            while not stop.is_set():
                while len(running) < self.concurrency:
                    claim = await self._claim()
                    if claim is None:
                        break
                    t = asyncio.create_task(self.run_claim(claim))
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
            self._wake = None
            unsubscribe()
            for t in running:
                t.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            await self._drop_holds()

    async def _drop_holds(self) -> None:
        """On the way out: what this process held dies with it, so the trees
        go back to the other workers at once."""
        if self._renewer is not None:
            self._renewer.cancel()
            self._renewer = None
        for root_id in list(self._holds):
            self._holds.pop(root_id, None)
            try:
                await self.store.unhold(root_id, self.worker)
            except Exception as exc:  # noqa: BLE001 — the hold runs out by itself
                log.warning("tree %s: unhold failed: %s", root_id, exc)
