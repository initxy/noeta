"""The worker side: claim tasks and run them."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from noeta.core import snapshots
from noeta.core.agent import Agent
from noeta.core.errors import CommitUnknown
from noeta.core.execenv import ExecEnv
from noeta.core.provider import Provider
from noeta.core.store import Claim, Store
from noeta.core.types import Delta, ModelRequest

if TYPE_CHECKING:
    from noeta.core.events import TaskCreated
    from noeta.core.loop.state import TaskState

log = logging.getLogger("noeta.core.runner")

#: The change kinds after which a task may be claimable (``Store.on_change``).
_CLAIMABLE_CHANGES = frozenset({"created", "posted", "released"})

ProviderFor = Callable[[str], Provider]
TaskProviderFor = Callable[["TaskState", str], Provider]
PrepareChild = Callable[["TaskCreated"], Awaitable["TaskCreated"]]
EnvFor = Callable[["TaskState"], ExecEnv]
OnDelta = Callable[[str, Delta], None]


def _worker_id() -> str:
    """``host:pid:random``: one name for this worker process in claims."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def _default_env(state: TaskState) -> ExecEnv:
    from noeta.core import registry

    return registry.execenv("local", str(state.config.get("cwd") or os.getcwd()))


def catalog_output_ceiling(model: str) -> int:
    """The ``max_tokens`` the model catalog (:mod:`noeta.core.models`) gives
    ``model`` by default (``ModelSpec.output_default``, Claude Code's); an
    unknown model gets the unknown-model cap."""
    from noeta.core import models

    spec = models.find_spec(model)
    return spec.output_default if spec is not None else models.UNKNOWN_MODEL_MAX_OUTPUT_TOKENS


@dataclass
class Runtime:
    """Everything a runner needs. One ``Runtime`` runs many tasks at once."""

    store: Store
    agents: dict[str, Agent]
    provider_for: ProviderFor
    env_for: EnvFor = _default_env
    #: ``task_provider(state, model)``: the provider of one task's call,
    #: picked with the task's folded state (a route its config records);
    #: ``None`` asks ``provider_for(model)``.
    task_provider: TaskProviderFor | None = None
    #: ``await prepare_child(created)``: a child task's ``TaskCreated`` as
    #: the host completes it before it is written (the route of its model);
    #: ``None`` writes it as staged.
    prepare_child: PrepareChild | None = None
    worker: str = field(default_factory=_worker_id)
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
    #: Write a task's fold snapshot (``noeta.core.snapshots``) once its log
    #: is at least this many events past the newest one and those events
    #: hold a share of its bytes (``noeta.core.runner.snapshotting.SNAPSHOT_GROWTH``);
    #: ``0`` never reads or writes snapshots.
    snapshot_every: int = 100
    #: An agent's former name -> its name now, for the tasks a log records
    #: under the former one (a host that renamed agents, e.g. a Claude Code
    #: plugin's ``<agent>`` that is now ``<plugin>:<agent>``). Never offered
    #: to a model; only a task's recorded agent is looked up through it.
    agent_aliases: dict[str, str] = field(default_factory=dict)
    #: Serve only the task trees whose root ids are in this set (a front end
    #: adds each task it creates or resumes); ``None`` serves every task.
    roots: set[str] | None = None
    _wake: asyncio.Event | None = field(default=None, init=False, repr=False)
    #: Per task: claims in a row whose log could not be read or folded here
    #: (``TaskRun._unreadable``).
    _unreadable: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    #: Per task: claims in a row, and the log's last seq, that found a
    #: pinned plugin missing here (``TaskRun._plugin_missing``).
    _plugin_missing: dict[str, tuple[int, int]] = field(default_factory=dict, init=False,
                                                        repr=False)
    #: The tasks this runtime runs now (:meth:`run_claim`).
    _running: set[str] = field(default_factory=set, init=False, repr=False)
    #: Of those, the ones waiting with their claim kept (``ToolContext.keep``):
    #: they take no slot of :attr:`concurrency`.
    kept_waiting: int = field(default=0, init=False, repr=False)

    def provider_of(self, state: TaskState, model: str) -> Provider:
        """The provider ``state``'s task sends a ``model`` call to."""
        if self.task_provider is not None:
            return self.task_provider(state, model)
        return self.provider_for(model)

    def __post_init__(self) -> None:
        # The snapshot version is of the code this process runs: hash it
        # now, before an upgrade on disk could change what is read.
        for agent in self.agents.values():
            try:
                snapshots.pin(agent.all_extensions())
            except ValueError:  # a misconfigured agent fails when its task loads
                continue

    def agent(self, name: str) -> Agent | None:
        """The agent a task's log names: by its name, else by a former one
        (:attr:`agent_aliases`)."""
        found = self.agents.get(name)
        if found is None and name in self.agent_aliases:
            found = self.agents.get(self.agent_aliases[name])
        return found

    async def _claim_once(self) -> tuple[Claim, float] | None:
        """A claim and this process's monotonic time just before it was
        asked for (when its lease surely started)."""
        roots = sorted(self.roots) if self.roots is not None else None
        sent = time.monotonic()
        try:
            claim = await self.store.claim(self.worker, self.queues, self.lease_seconds,
                                           roots=roots)
        except CommitUnknown as exc:
            log.warning("claim commit unknown, looking for what it took: %s", exc)
            claim = await self._adopt()
        return (claim, sent) if claim is not None else None

    async def _adopt(self) -> Claim | None:
        """A task the store says this worker runs and it does not: a claim
        whose commit landed unseen (``CommitUnknown``), run now rather than
        after its lease ran out. The epoch in the row fences it as any claim."""
        for row in await self.store.tasks(owner=self.worker, status="running", limit=100):
            if row.task_id not in self._running:
                return Claim(task_id=row.task_id, worker=self.worker, epoch=row.owner_epoch,
                             lease_until=row.lease_until or 0.0, queue=row.queue)
        return None

    async def run_claim(self, claim: Claim, *, sent: float | None = None) -> None:
        """Run one claim until the task waits or ends. Cancelled (a
        shutdown), it hands the task back at once instead of leaving it
        ``running`` until the lease runs out. ``sent`` is this process's
        monotonic time just before the claim was asked for (default now)."""
        from noeta.core.runner.task import TaskRun

        run = TaskRun(self, claim, sent=sent)
        self._running.add(claim.task_id)
        try:
            await run.run()
        except asyncio.CancelledError:
            await self._release(run.claim)
            raise
        finally:
            self._running.discard(claim.task_id)

    async def _release(self, claim: Claim) -> None:
        try:
            await self.store.release(claim)
        except Exception as exc:  # noqa: BLE001 — the lease covers it
            log.warning("task %s: release failed: %s", claim.task_id, exc)

    async def run_once(self) -> bool:
        """Claim one task and run it until it waits or ends. ``False`` when
        nothing was claimable."""
        claimed = await self._claim_once()
        if claimed is None:
            return False
        await self.run_claim(claimed[0], sent=claimed[1])
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

    async def _claim(self) -> tuple[Claim, float] | None:
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

        def nudge(_task: str, kind: str) -> None:
            # Only a change that can make a task claimable: an append alone
            # (the bulk of the traffic) never does unless it is reported
            # ``released`` too.
            if kind in _CLAIMABLE_CHANGES:
                loop.call_soon_threadsafe(wake.set)

        self._wake = wake
        unsubscribe = self.store.on_change(nudge)
        running: set[asyncio.Task[None]] = set()
        try:
            while not stop.is_set():
                while len(running) - self.kept_waiting < self.concurrency:
                    claimed = await self._claim()
                    if claimed is None:
                        break
                    t = asyncio.create_task(self.run_claim(claimed[0], sent=claimed[1]))
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
