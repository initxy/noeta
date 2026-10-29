"""The worker side: claim tasks and run them."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from noeta.core import snapshots
from noeta.core.agent import Agent
from noeta.core.errors import CommitUnknown, HeldElsewhere
from noeta.core.execenv import ExecEnv
from noeta.core.holds import Release, TreeHold
from noeta.core.provider import Provider
from noeta.core.store import Claim, Store
from noeta.core.tenants import Tenant, charged, over_budget, period
from noeta.core.types import Delta, ModelRequest, Usage

if TYPE_CHECKING:
    from noeta.core.loop.state import TaskState

log = logging.getLogger("noeta.core.runner")

#: Times a hold is asked for while its commit's outcome stays unknown
#: (``CommitUnknown``) before that error is raised.
HOLD_ATTEMPTS = 3

ProviderFor = Callable[[str], Provider]
EnvFor = Callable[["TaskState"], ExecEnv]
OnDelta = Callable[[str, Delta], None]


def _worker_id() -> str:
    """``host:pid:random``: one name for this worker process in claims,
    holds and the records of what it runs (a background job)."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


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
class _TreeLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Callers inside or waiting: the entry is dropped when none are left.
    users: int = 0


@dataclass
class Runtime:
    """Everything a runner needs. One ``Runtime`` runs many tasks at once."""

    store: Store
    agents: dict[str, Agent]
    provider_for: ProviderFor
    env_for: EnvFor = _default_env
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
    #: Serve only the task trees whose root ids are in this set (a front end
    #: adds each task it creates or resumes); ``None`` serves every task.
    roots: set[str] | None = None
    #: What each tenant is allowed (``noeta.core.tenants``), by name; a
    #: tenant not named here has no limits and uses ``provider_for``.
    tenants: Mapping[str, Tenant] = field(default_factory=dict)
    _wake: asyncio.Event | None = field(default=None, init=False, repr=False)
    #: This worker's holds, by tree root (:meth:`hold`).
    _holds: dict[str, TreeHold] = field(default_factory=dict, init=False, repr=False)
    _renewer: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    #: Per tree root: holds are taken one at a time, so one ``TreeHold``
    #: stands for the store's hold.
    _hold_locks: dict[str, _TreeLock] = field(default_factory=dict, init=False, repr=False)
    #: Per tree root: the store's hold being given back (the last share
    #: released); a new hold of the tree waits for it.
    _unholding: dict[str, asyncio.Future[None]] = field(default_factory=dict, init=False,
                                                        repr=False)
    #: Per task: claims in a row whose log could not be read or folded here
    #: (``TaskRun._unreadable``).
    _unreadable: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        # The snapshot version is of the code this process runs: hash it
        # now, before an upgrade on disk could change what is read.
        for agent in self.agents.values():
            try:
                snapshots.pin(agent.all_extensions())
            except ValueError:  # a misconfigured agent fails when its task loads
                continue

    @property
    def hold_seconds(self) -> float:
        """How long a hold outlives its last renewal: a dead worker's trees
        go back to the others after this."""
        return 2 * self.lease_seconds

    async def hold(self, root_id: str) -> Release:
        """Keep the tree of ``root_id`` with this worker (no other worker
        claims its tasks) until the returned release is awaited; holds are
        counted and renewed while this runtime lives. For what lives only in
        this process, e.g. a background job: it registers
        ``release.tree.on_lost`` to stop when the hold is gone (reaped after
        this worker was silent too long, or this runtime stopping).
        Raises ``HeldElsewhere`` while another worker holds the tree (one
        whose hold ran out is reaped first) or runs one of its tasks, or
        while one of its unended tasks is on a queue this runtime does not
        serve (no other worker would run it while the tree is held)."""
        async with self._tree_lock(root_id):
            tree = self._holds.get(root_id)
            if tree is None or tree.lost:
                unholding = self._unholding.get(root_id)
                if unholding is not None:
                    await asyncio.shield(unholding)  # the last share's give-back
                sent, epoch = await self._take_hold(root_id)
                if epoch is None:
                    await self._reap()
                    sent, epoch = await self._take_hold(root_id)
                if epoch is None:
                    raise HeldElsewhere(f"task tree {root_id} is held by another worker, "
                                       "another worker runs one of its tasks, or one of its "
                                       "tasks is on a queue this worker does not serve")
                tree = TreeHold(root_id=root_id, worker=self.worker, epoch=epoch,
                                seconds=self.hold_seconds, renewed=sent)
                self._holds[root_id] = tree
            tree.count += 1
        if self._renewer is None or self._renewer.done():
            self._renewer = asyncio.create_task(self._renew_holds())
        released = False

        async def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            tree.count -= 1
            if tree.count > 0 or self._holds.get(root_id) is not tree:
                return
            self._holds.pop(root_id, None)
            if not tree.lost:
                # A hold taken for the tree meanwhile waits for this, so it
                # never gets the epoch being deleted; given back even when
                # the caller is cancelled while it waits.
                unholding = asyncio.ensure_future(self._unhold(tree))
                self._unholding[root_id] = unholding
                await asyncio.shield(unholding)

        return Release(tree, release)

    async def _take_hold(self, root_id: str) -> tuple[float, int | None]:
        """``Store.hold`` without an epoch, and this process's monotonic
        time just before it was asked for. A commit whose outcome is unknown
        is asked again: taking a hold extends the one this worker already
        has, so a hold that landed comes back with its epoch, registered and
        renewed rather than left to be reaped as lost."""
        for attempt in range(HOLD_ATTEMPTS):
            sent = time.monotonic()
            try:
                return sent, await self.store.hold(root_id, self.worker, self.hold_seconds,
                                                   queues=self.queues)
            except CommitUnknown as exc:
                if attempt + 1 == HOLD_ATTEMPTS:
                    raise
                log.warning("tree %s: hold commit unknown, asking again: %s", root_id, exc)
        raise AssertionError("unreachable")

    async def _unhold(self, tree: TreeHold) -> None:
        try:
            await self.store.unhold(tree.root_id, self.worker, tree.epoch)
        except Exception as exc:  # noqa: BLE001 — the hold runs out and is reaped
            log.warning("tree %s: unhold failed: %s", tree.root_id, exc)
        finally:
            if self._unholding.get(tree.root_id) is asyncio.current_task():
                self._unholding.pop(tree.root_id, None)

    @contextlib.asynccontextmanager
    async def _tree_lock(self, root_id: str) -> AsyncIterator[None]:
        """Taking a hold of one tree, one at a time: two holds taken at
        once would make two ``TreeHold`` objects, one never renewed."""
        entry = self._hold_locks.get(root_id)
        if entry is None:
            entry = self._hold_locks[root_id] = _TreeLock()
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if not entry.users:
                self._hold_locks.pop(root_id, None)

    async def _renew_holds(self) -> None:
        while self._holds:
            # Real time, never ``self.sleep`` (tests replace that with no
            # wait): a hold must outlive the worker by wall-clock seconds.
            await asyncio.sleep(self.lease_seconds / 3)
            for tree in list(self._holds.values()):
                sent = time.monotonic()
                try:
                    kept = await self.store.hold(tree.root_id, self.worker, self.hold_seconds,
                                                 epoch=tree.epoch)
                except Exception as exc:  # noqa: BLE001 — retried on the next round
                    log.warning("tree %s: renewing the hold failed: %s", tree.root_id, exc)
                    continue
                if kept is None:
                    log.warning("tree %s: hold %s was reaped; stopping what it held",
                                tree.root_id, tree.epoch)
                    if self._holds.get(tree.root_id) is tree:
                        self._holds.pop(tree.root_id)
                    tree.mark_lost()
                else:
                    tree.renewed = sent

    async def _reap(self) -> None:
        """Reap the holds of silent workers (``Store.reap_holds``): every
        claim looks first, so no tree stays held by a dead worker."""
        try:
            reaped = await self.store.reap_holds()
        except Exception as exc:  # noqa: BLE001 — the next round tries again
            log.warning("reaping holds failed: %s", exc)
            return
        if reaped:
            log.info("reaped %d hold(s) of silent workers", reaped)

    async def _claim_once(self) -> tuple[Claim, float] | None:
        """A claim and this process's monotonic time just before it was
        asked for (when its lease surely started)."""
        await self._reap()
        roots = sorted(self.roots) if self.roots is not None else None
        limits = {name: t.max_running for name, t in self.tenants.items()
                  if t.max_running is not None}
        sent = time.monotonic()
        if limits:
            claim = await self.store.claim(self.worker, self.queues, self.lease_seconds,
                                           roots=roots, tenant_limits=limits)
        else:
            claim = await self.store.claim(self.worker, self.queues, self.lease_seconds,
                                           roots=roots)
        return (claim, sent) if claim is not None else None

    def provider(self, model: str, tenant: str) -> Provider:
        """The provider for ``model`` in a task of ``tenant``."""
        own = self.tenants.get(tenant)
        return (own.provider_for if own is not None and own.provider_for is not None
                else self.provider_for)(model)

    async def budget_refusal(self, tenant: str) -> str | None:
        """Why a task of ``tenant`` may make no model call now (its budget is
        spent), or ``None``."""
        own = self.tenants.get(tenant)
        read = getattr(self.store, "tenant_usage", None)
        if own is None or not own.budgeted or read is None:
            return None
        when = period()
        return over_budget(tenant, own, await read(tenant, when), when)

    async def charge(self, tenant: str, model: str, usage: Usage) -> None:
        """Count a model call against ``tenant``'s budget (when it has one)."""
        own = self.tenants.get(tenant)
        add = getattr(self.store, "add_tenant_usage", None)
        if own is None or not own.budgeted or add is None:
            return
        tokens, cost = charged(model, usage)
        try:
            await add(tenant, period(), tokens, cost)
        except Exception as exc:  # noqa: BLE001 — the call happened; the count is best effort
            log.warning("tenant %s: counting usage failed: %s", tenant, exc)

    async def run_claim(self, claim: Claim, *, sent: float | None = None) -> None:
        """Run one claim until the task waits or ends. Cancelled (a
        shutdown), it hands the task back at once instead of leaving it
        ``running`` until the lease runs out. ``sent`` is this process's
        monotonic time just before the claim was asked for (default now)."""
        from noeta.core.runner.task import TaskRun

        run = TaskRun(self, claim, sent=sent)
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

        def nudge(_task: str, _kind: str) -> None:
            loop.call_soon_threadsafe(wake.set)

        self._wake = wake
        unsubscribe = self.store.on_change(nudge)
        running: set[asyncio.Task[None]] = set()
        try:
            while not stop.is_set():
                while len(running) < self.concurrency:
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
            await self._drop_holds()

    async def _drop_holds(self) -> None:
        """On the way out: what this process held dies with it, and the
        trees are told so (``hold_lost``) and go back to the other workers
        at once."""
        if self._renewer is not None:
            self._renewer.cancel()
            self._renewer = None
        for tree in list(self._holds.values()):
            self._holds.pop(tree.root_id, None)
            tree.mark_lost()
            try:
                await self.store.unhold(tree.root_id, self.worker, tree.epoch, lost=True)
            except Exception as exc:  # noqa: BLE001 — the hold runs out and is reaped
                log.warning("tree %s: unhold failed: %s", tree.root_id, exc)
