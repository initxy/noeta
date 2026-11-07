"""Running one claimed task: fold the log, take the inbox, act, append —
until the task waits or ends.

Two rules keep this crash- and race-safe:

* every write is ``Store.append`` with the claim's epoch and the seq this
  runner last saw, so a runner that lost its claim stops at its next write;
* ``ToolStarted`` is appended before a tool runs, so a runner that lost its
  claim cannot start new work, and a crash leaves an unfinished call that
  recovery answers with "outcome unknown" instead of running it again,
  unless its tool recovers the outcome or is safe to run again
  (``Tool.recover`` / ``retry_safe``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Sequence
from typing import Any, cast

import msgspec
from msgspec.structs import replace

from noeta.core import records as core_records
from noeta.core import registry, snapshots
from noeta.core.agent import Agent
from noeta.core.errors import (
    Conflict,
    InboxUnread,
    StoreUnavailable,
)
from noeta.core.events import (
    Cancel,
    ChildEnded,
    Event,
    ExtensionRecorded,
    InputTaken,
    Recorded,
    TaskCreated,
    TaskEnded,
    ToolFinished,
    Waiting,
)
from noeta.core.extension import Extension, TaskView, ToolOffer, offer_tools, task_cwd
from noeta.core.loop import decide
from noeta.core.loop.state import TaskState, also_folding, apply, continued, fold, new_state
from noeta.core.prompts import load_overrides
from noeta.core.records import CORE
from noeta.core.runner import closing, texts, timers
from noeta.core.runner.runtime import Runtime
from noeta.core.runner.snapshotting import SnapshotWriter
from noeta.core.store import Claim, NewTask, Patch, Post, Snapshot, patch_for, pin_history
from noeta.core.tool import KeptClaim, Tool
from noeta.core.types import Effort, Text, Thinking, ToolResult

log = logging.getLogger("noeta.core.runner")


#: Continued conversations followed back from a task (``history_from``)
#: before the rest of the chain is ignored.
MAX_HISTORY_DEPTH = 64
#: Claims in a row (in one worker) whose log could not be read or folded
#: before the task is ended failed instead of claimed again.
LOAD_ATTEMPTS = 3
#: Tasks a worker keeps that count for (the oldest is forgotten): one that
#: ends on another worker is never loaded here again.
UNREADABLE_KEEP = 1024

class LostClaim(Exception):
    pass


class UnreadableInbox(Exception):
    """The inbox holds an item this version cannot decode (``cause``)."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


#: How msgspec words a tagged union member or a literal value it does not
#: know: what a newer version writes, unlike a corrupt or misshapen entry.
_NEWER_FORMAT = re.compile(r"Invalid (enum )?value '")


def _newer_format(exc: Exception) -> bool:
    if isinstance(exc, core_records.NewerRecord):
        return True
    return isinstance(exc, msgspec.ValidationError) and bool(_NEWER_FORMAT.match(str(exc)))


def _decoded(stored: Snapshot) -> TaskState | None:
    try:
        return snapshots.decode(stored.data)
    except Exception:  # noqa: BLE001 — another fold's data: fold from the start
        return None


class TaskRun:
    def __init__(self, rt: Runtime, claim: Claim, *, sent: float | None = None) -> None:
        self.rt = rt
        self.store = rt.store
        self.claim = claim
        self.task_id = claim.task_id
        self.state = TaskState()
        self.agent: Agent | None = None
        self.exts: list[Extension] = []
        self.reducers: dict[str, Any] = {}
        self.also: dict[str, list[str]] = {}
        self._step: asyncio.Task[Any] | None = None
        self._lost = False
        #: When the claim's lease runs out by this process's monotonic clock
        #: (``Claim.lease_until`` is the store's clock, which may differ):
        #: counted from just before the claim, then the last successful
        #: renewal, was asked for.
        self._lease_deadline = (time.monotonic() if sent is None else sent) + rt.lease_seconds
        self._write_lock = asyncio.Lock()
        self._env: Any = None
        #: The text the model call in flight has streamed so far, and the
        #: reasoning blocks it finished (what an interrupted reply keeps).
        self.partial: list[str] = []
        self.thinking: list[Thinking] = []
        #: An extension posted to this task's inbox with its records
        #: (``TaskView.enqueue``) since the input was last taken.
        self._enqueued = False
        #: The timers due by the store's clock were fired in this claim
        #: (``_woken_by_timer``).
        self._timer_forced = False
        self.snapshots = SnapshotWriter(self)
        #: What keeps the claim over the task's waits (``ToolContext.keep``).
        self.kept: set[KeptClaim] = set()
        #: The patch of the wait the task is in with its claim kept
        #: (``Patch.keep``), else ``None``.
        self._kept_wait: Patch | None = None
        #: Set by a post to the task, a kept claim given back or the claim
        #: lost: a kept wait looks again (:meth:`_wait_kept`).
        self._nudged = asyncio.Event()
        #: Set while the task waits with its claim kept (``Runtime.run_claim``
        #: then lets the run go on in the background).
        self.waiting_kept = asyncio.Event()
        self._kept_due: float | None = None
        #: A kept run woken to step holds a slot of ``Runtime.concurrency``.
        self._slot = False
        self._loop: asyncio.AbstractEventLoop | None = None
        #: The claim took the task over from one never given back; judged
        #: once, at the claim's first start (:meth:`_record_start`).
        self._reclaimed = claim.reclaimed

    # ------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        keeper = asyncio.create_task(self._keep_claim())
        unsubscribe = self.store.on_change(self._on_change)
        try:
            try:
                await self.load()
            except (Conflict, LostClaim, StoreUnavailable):
                raise
            except Exception as exc:
                if _newer_format(exc):
                    # A newer version wrote it (a rolling upgrade): not this
                    # worker's to fail. The lease runs out and the task is
                    # claimed again, by a newer worker sooner or later.
                    log.warning("task %s: its log has entries this version does not know, "
                                "leaving it to a newer worker: %s", self.task_id, exc)
                    return
                # The log could not even be read and folded (nothing was written).
                log.exception("task %s: could not load", self.task_id)
                try:
                    await self._unreadable(exc)
                except Exception:  # claimed again once the lease runs out
                    log.exception("task %s: could not end it failed", self.task_id)
                return
            # Kept for an unreadable inbox, which counts on from it.
            unreadable = self.rt._unreadable.pop(self.task_id, 0)
            self.snapshots.write_loaded()
            if self.agent is None:
                # Releasing would hand it straight back to this worker; a task
                # whose agent only some workers have must be routed by queue.
                # Ending it the ordinary way tells a parent that waits on it.
                await closing.end(self, "failed", error=f"no agent named {self.state.agent!r} is "
                                               f"registered in the worker that claimed this task")
                return
            await self._steps()
        except UnreadableInbox as exc:
            await self._unreadable_inbox(exc.cause, unreadable)
        except (Conflict, LostClaim):
            log.info("task %s: claim lost; stopping without further writes", self.task_id)
        except StoreUnavailable as exc:
            # The store is unreachable: stop; the lease runs out and the task
            # is claimed again once the store is back.
            log.warning("task %s: store error, stopping: %s", self.task_id, exc)
        except Exception:
            # Even failing the turn failed (the log itself read fine): the
            # task is claimed again once the lease runs out.
            log.exception("task %s: could not run", self.task_id)
        finally:
            unsubscribe()
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper
            # The claim leaves this worker: so does what it was kept for.
            for kept in list(self.kept):
                kept.mark_lost()
            self.kept.clear()
            self._give_slot()
            await self.snapshots.finish()

    async def _unreadable_inbox(self, exc: Exception, tries: int) -> None:
        """An inbox item that cannot be decoded, judged as a log entry is
        (``tries``: the unreadable claims before this one): a newer
        version's is left to a newer worker, a malformed one counts toward
        ending the task failed. Nothing is written until then."""
        if _newer_format(exc):
            log.warning("task %s: its inbox has an item this version does not know, "
                        "leaving it to a newer worker: %s", self.task_id, exc)
            return
        log.error("task %s: could not read its inbox: %r", self.task_id, exc)
        if tries:
            self.rt._unreadable[self.task_id] = tries
        try:
            await self._unreadable(exc)
        except Exception:  # claimed again once the lease runs out
            log.exception("task %s: could not end it failed", self.task_id)

    async def _unreadable(self, exc: Exception) -> None:
        """A claim whose log could not be read or folded: the task is
        claimed again once the lease runs out, and after ``LOAD_ATTEMPTS``
        such claims in a row here it ends failed, written from its row
        since there is no folded state: its parent hears it, its children
        are cancelled."""
        tries = self.rt._unreadable.get(self.task_id, 0) + 1
        if tries < LOAD_ATTEMPTS:
            counts = self.rt._unreadable
            if self.task_id not in counts and len(counts) >= UNREADABLE_KEEP:
                counts.pop(next(iter(counts)))
            counts[self.task_id] = tries
            return
        self.rt._unreadable.pop(self.task_id, None)
        error = texts.UNREADABLE_LOG.format(attempts=tries, error=repr(exc))
        while True:
            row = await self.store.get(self.task_id)
            if row is None or row.status == "ended":
                return
            posts = [Post(task_id=c.task_id, body=Cancel(reason="parent task failed"))
                     for c in await self.store.tasks(parent_id=self.task_id, limit=10_000)
                     if c.status != "ended"]
            if row.parent_id:
                posts.append(Post(task_id=row.parent_id, dedupe_key=f"child:{self.task_id}",
                                  body=ChildEnded(child_id=self.task_id, status="failed",
                                                  error=error)))
            try:
                await self.store.append(
                    self.claim, [TaskEnded(status="failed", error=error)],
                    expected_seq=row.last_seq, posts=posts,
                    patch=Patch(status="ended", end_status="failed",
                                inbox_read_seq=row.inbox_seq))
            except InboxUnread:
                continue  # posted to meanwhile: read the row again
            log.error("task %s: ended failed: %s", self.task_id, error)
            return

    async def _steps(self) -> None:
        """Step until the task waits or ends. A bug (an extension hook, a
        provider, the runner itself) fails the turn instead of leaving the
        task ``running`` to be claimed and fail again on every lease; a
        second one in a row ends the task."""
        started = False
        failures = 0
        loaded = self.state.last_seq
        while True:
            try:
                if not started:
                    if not await self._record_start():
                        return
                    view = self.view()
                    for ext in self.exts:
                        await ext.on_start(view)
                    await self.flush(view)
                    started = True
                    await self._redeliver(loaded)
                if not await self._step_once():
                    if not await self._wait_kept():
                        return
                    # Woken as by a new claim: the pickup's start runs again.
                    started = False
                failures = 0
            except (Conflict, LostClaim, StoreUnavailable, UnreadableInbox,
                    asyncio.CancelledError):
                raise
            except Exception as exc:
                log.exception("task %s: internal error", self.task_id)
                failures += 1
                started = True
                if failures > 1:
                    await closing.end(self, "failed", self.state.last_answer,
                                   f"{texts.INTERNAL_ERROR}: {exc!r}")
                    return
                if not await closing.fail_turn(self, texts.INTERNAL_ERROR, repr(exc)):
                    return

    async def _record_prompt_files(self, view: TaskView) -> None:
        """Read the prompt override files once, when the task first starts,
        and record them so every later pickup sends the same bytes."""
        env = self.env()
        cwd = task_cwd(self.state, env)
        trusted = self.rt.workspace_trusted
        found = await load_overrides(self.rt.prompt_dirs, env, cwd,
                                     workspace=trusted is None or (bool(cwd) and trusted(cwd)))
        prompts = core_records.Prompts(files=found)
        view.record(CORE, prompts.kind, prompts.data())

    async def _plugin_missing(self, missing: list[str]) -> bool:
        """A pinned plugin this worker lacks, as in a rolling install: the
        lease runs out with nothing written (a worker that has it claims the
        task), and after ``LOAD_ATTEMPTS`` such claims in a row here with the
        log unchanged the task ends failed. Always ``False``."""
        seq = self.state.last_seq
        tries, at = self.rt._plugin_missing.get(self.task_id, (0, seq))
        tries = tries + 1 if at == seq else 1
        if tries < LOAD_ATTEMPTS:
            counts = self.rt._plugin_missing
            if self.task_id not in counts and len(counts) >= UNREADABLE_KEEP:
                counts.pop(next(iter(counts)))
            counts[self.task_id] = (tries, seq)
            log.warning("task %s: runs with %s, which this worker does not have; leaving it "
                        "to a worker that has them", self.task_id, ", ".join(missing))
            return False
        self.rt._plugin_missing.pop(self.task_id, None)
        await closing.end(self, "failed", self.state.last_answer,
                          texts.PLUGIN_MISSING.format(names=", ".join(missing)))
        return False

    async def _record_start(self) -> bool:
        """At each pickup, before ``on_start``: record the prompt override
        files when the task first starts, and the plugins it runs with then
        and whenever a pickup finds them changed (a reload, an upgrade).
        ``False`` when a plugin it recorded is missing here
        (:meth:`_plugin_missing`): it never runs without it."""
        view = self.view()
        if self.state.prompt_files is None:
            await self._record_prompt_files(view)
        current = [registry.pin(e) for e in self.exts]
        pinned = self.state.plugins
        if pinned is not None:
            missing = sorted({str(p.get("name")) for p in pinned}
                             - {p["name"] for p in current})
            if missing:
                return await self._plugin_missing(missing)
            self.rt._plugin_missing.pop(self.task_id, None)
            if pinned != current:
                log.info("task %s: plugins changed since it last ran", self.task_id)
        if pinned != current:
            plugins = core_records.Plugins(pins=current)
            view.record(CORE, plugins.kind, plugins.data())
        if self._reclaimed:
            self._reclaimed = False
            return await self._record_reclaim(view)
        await self.flush(view)
        return True

    async def _record_reclaim(self, view: TaskView) -> bool:
        """This claim took the task over from one never given back. Counted
        only by a worker that will run the task (after the plugin check, and
        once its inbox reads: a worker that walks away writes nothing), in
        the log, so the count survives the crash it is about. Past
        ``Runtime.restart_limit`` such restarts in a row with no step
        finished, the task ends ``failed`` instead: its parent hears it like
        any failure. ``False`` once it has ended."""
        try:
            await self.store.inbox(self.task_id, after=self.state.inbox_read)
        except msgspec.DecodeError as exc:
            raise UnreadableInbox(exc) from exc
        limit = self.rt.restart_limit
        if limit <= 0 or self.state.reclaims < limit:
            reclaimed = core_records.Reclaimed()
            view.record(CORE, reclaimed.kind, reclaimed.data())
            await self.flush(view)
            return True
        await self.flush(view)
        error = texts.RESTART_LIMIT.format(n=self.state.reclaims + 1)
        log.error("task %s: ended failed: %s", self.task_id, error)
        await closing.end(self, "failed", self.state.last_answer, error,
                          close_open=f"{texts.TURN_FAILED_CALL_ERROR}restart_limit")
        return False

    async def load(self) -> None:
        """Fold the log (and the conversation it continues): from the
        task's snapshot when one of this fold's version exists, else from
        the start."""
        if await self._load_snapshot():
            return
        records = await self.store.read(self.task_id)
        first = records[0].event if records else None
        self._use_agent(getattr(first, "agent", ""))
        initial = {e.name: e.initial_state() for e in self.exts}
        base = await self._history(first, initial) or new_state(initial)
        base.task_id = self.task_id  # the log never names its own task
        if self._snapshots_on():
            data = snapshots.encode(base)
            if data is not None:
                self.snapshots.known(0, data, records)
        self.state = fold(records, self.reducers, state=base, also=self.also)

    def _use_agent(self, name: str) -> None:
        self.agent = self.rt.agent(name)
        self.exts = self.agent.all_extensions() if self.agent is not None else []
        self.reducers = {e.name: e.reduce for e in self.exts}
        self.also = also_folding(self.exts)

    def _snapshots_on(self) -> bool:
        return self.rt.snapshot_every > 0 and self.agent is not None \
            and getattr(self.store, "put_snapshot", None) is not None

    def fold_version(self) -> str:
        """The snapshot version of this task's fold (``snapshots.version``)."""
        assert self.agent is not None
        return snapshots.version(self.agent.name, self.exts)

    async def _load_snapshot(self) -> bool:
        """Start from the task's snapshot and fold only the events after it;
        ``False`` (nothing changed) when there is none of this version."""
        get = getattr(self.store, "snapshot", None)
        if get is None or self.rt.snapshot_every <= 0:
            return False
        stored: Snapshot | None = await get(self.task_id)
        if stored is None:
            return False
        state = _decoded(stored)
        if state is not None:
            agent = state.agent
        else:  # another fold's data (a rolling upgrade): the row names the agent
            row = await self.store.get(self.task_id)
            agent = row.agent if row is not None else ""
        self._use_agent(agent)
        if self.agent is None:
            return False
        mine = self.fold_version()
        if stored.version != mine:
            # The newest is another fold version's: this version's own, if any.
            stored = await get(self.task_id, mine)
            state = _decoded(stored) if stored is not None else None
        if stored is None or state is None or state.agent != agent:
            return False
        records = await self.store.read(self.task_id, after=stored.seq)
        self.state = fold(records, self.reducers, state=state, also=self.also)
        self.snapshots.known(stored.seq, stored.data, records)
        return True

    async def _history(self, first: Any, initial: dict[str, Any],
                       depth: int = 0, task_id: str = "") -> TaskState | None:
        """The conversation a task created with ``history_from`` starts
        from: its source's log up to ``history_through``, itself folded on
        the conversation that source continued (a fork of a fork, a resumed
        sub-agent resumed again). The source's call that started the task
        (a fork's ``Agent`` call) is answered as the one that made it."""
        if not isinstance(first, TaskCreated) or not first.history_from \
                or depth > MAX_HISTORY_DEPTH:
            return None
        task_id = task_id or self.task_id
        source = await self.store.read(first.history_from)
        through = first.history_through
        base = await self._history(source[0].event if source else None, initial, depth + 1,
                                   first.history_from)
        return continued(fold([r for r in source if through is None or r.seq <= through],
                              self.reducers, initial, state=base, also=self.also),
                         {e.name: e.continue_state for e in self.exts},
                         started_by=core_records.spawning_call(source, task_id))

    async def _keep_claim(self) -> None:
        interval = max(self.rt.lease_seconds / 3, 0.05)
        while True:
            await asyncio.sleep(interval)
            sent = time.monotonic()
            try:
                renewed = await self.store.renew(self.claim, self.rt.lease_seconds)
            except Exception as exc:  # noqa: BLE001 — the keeper must never die silently
                if time.monotonic() + interval < self._lease_deadline:
                    log.warning("task %s: renew failed, retrying: %s", self.task_id, exc)
                    continue
                log.warning("task %s: renew kept failing; giving the task up: %s",
                            self.task_id, exc)
                renewed = None
            if renewed is None:
                self._lost = True
                self._nudged.set()
                if self._step is not None:
                    self._step.cancel()
                return
            self.claim = renewed
            self._lease_deadline = sent + self.rt.lease_seconds

    def _on_change(self, task_id: str, kind: str) -> None:
        if task_id == self.task_id and kind == "posted" and self._loop is not None:
            with contextlib.suppress(RuntimeError):  # the loop has closed
                self._loop.call_soon_threadsafe(self._nudged.set)
        if task_id == self.task_id and kind == "posted" and self._step is not None:
            try:
                asyncio.get_running_loop().create_task(self._check_stop())
            except RuntimeError:  # posted from another thread's loop; polling covers it
                pass

    async def _check_stop(self) -> None:
        try:
            items = await self.store.inbox(self.task_id, after=self.state.inbox_read)
        except msgspec.DecodeError:
            return  # the next take of the input judges it

        if any(isinstance(i.body, Cancel) for i in items) and self._step is not None:
            self._step.cancel()

    async def _watch_inbox(self) -> None:
        """Polling fallback for posts made by other processes: every
        ``poll_interval``, or every third of the lease where the store's
        change feed hears them (``shared_changes``) but may drop some while
        it reconnects."""
        rt = self.rt
        interval = max(rt.lease_seconds / 3, rt.poll_interval) \
            if getattr(self.store, "shared_changes", False) else rt.poll_interval
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                await self._check_stop()

    # ------------------------------------------------------------ writing

    async def append(
        self,
        events: Sequence[Event],
        *,
        posts: Sequence[Post] = (),
        creates: Sequence[Any] = (),
        patch: Patch | None = None,
    ) -> None:
        if self._lost:
            raise LostClaim()
        prepare = self.rt.prepare_child
        creates = [replace(c, created=await pin_history(
            self.store, c.created if prepare is None else await prepare(c.created)))
                   for c in creates]
        hand_back: Patch | None = None
        async with self._write_lock:
            base = self.state.last_seq
            if patch is None:
                patch = patch_for(events, self.state.inbox_read)
                if patch.status == "waiting":
                    # Kept until the extensions have seen the Waiting and their
                    # records landed: a crash before leaves the task claimable,
                    # and its claimer delivers the events again.
                    hand_back, patch = patch, Patch()
            if patch.status == "waiting" and self.kept:
                # What lives only here (a background job) keeps the claim.
                patch = replace(patch, keep=True)
            write = asyncio.ensure_future(self.store.append(
                self.claim, events, expected_seq=base, patch=patch, posts=posts, creates=creates))
            cancelled = False
            # A cancel must not separate a committed write from this runner's
            # view of the log, or its next write would conflict with itself.
            while not write.done():
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    cancelled = True
            at = write.result().at
            self._kept_wait = patch if patch.keep else None
            recs = [Recorded(seq=base + i, at=at, event=e) for i, e in enumerate(events, 1)]
            for rec in recs:
                apply(self.state, rec, self.reducers, self.also)
            self.snapshots.count(events)
            if cancelled:
                raise asyncio.CancelledError()
        self.snapshots.write_later()
        view = self.view()
        for rec in recs:
            await self._deliver(view, rec, self.exts)
        if patch.status == "ended":
            if view.pending or view.creates or view.enqueued:
                log.warning("task %s: records made after its end dropped", self.task_id)
            return
        await self.flush(view)
        if hand_back is not None:
            await self.append([], patch=replace(hand_back, inbox_read_seq=self.state.inbox_read))

    async def _deliver(self, view: TaskView, rec: Recorded,
                       exts: Sequence[Extension]) -> None:
        for ext in exts:
            try:
                await ext.on_event(view, rec)
            except Exception:
                log.exception("extension %s on_event failed", ext.name)

    async def _redeliver(self, through: int) -> None:
        """At each claim: the events up to ``through`` (the log as loaded)
        after each extension's ``delivered_through`` cursor go to its
        ``on_event`` again."""
        view = self.view()
        cursors = [(e, c) for e in self.exts
                   if (c := e.delivered_through(view)) is not None and c < through]
        if not cursors:
            return
        for rec in await self.store.read(self.task_id, after=min(c for _, c in cursors)):
            if rec.seq > through:
                break
            await self._deliver(view, rec, [e for e, c in cursors if rec.seq > c])
        await self.flush(view)

    async def wait(self, waiting: Waiting) -> None:
        """Hand the task back waiting: ``waiting`` is appended unless it is
        the one already standing (a wake that changed nothing it waits on),
        so observers see each wait once."""
        if self.state.waiting == waiting:
            await self.keep_waiting()
        else:
            await self.append([waiting])

    async def keep_waiting(self) -> None:
        """Hand the task back still waiting on its log's last ``Waiting`` (a
        wake that brought nothing it waits for), without writing it again. A
        message taken since clears ``waiting``; an asked approval still stands."""
        waiting = self.state.waiting or self.state.asked
        assert waiting is not None
        await self.append([], patch=patch_for([waiting], self.state.inbox_read))

    @property
    def one_shot(self) -> bool:
        """The task ends when its turn ends: a one-shot agent, or any
        sub-agent (its parent waits for it to end, whatever its agent file
        says)."""
        assert self.agent is not None
        return self.agent.one_shot or self.state.parent_id is not None

    @property
    def model(self) -> str:
        """The task's model: its config's, else its agent's."""
        return str(self.state.config.get("model") or (self.agent.model if self.agent else ""))

    @property
    def identity(self) -> str:
        """The identity line the task's requests open with (``identity/<agent>``)."""
        agent = self.agent
        return self.state.prompt(f"identity/{agent.name}", agent.identity) if agent else ""

    @property
    def effort(self) -> Effort | None:
        """The task's effort: its config's, else its agent's."""
        effort = self.state.config.get("effort") or (self.agent.effort if self.agent else None)
        return cast("Effort | None", effort)

    def keep(self) -> KeptClaim:
        """``ToolContext.keep``: keep this claim over the task's waits until
        the returned claim is released."""
        kept = KeptClaim(lease_seconds=self.rt.lease_seconds, _release=self._unkeep)
        self.kept.add(kept)
        return kept

    def _unkeep(self, kept: KeptClaim) -> None:
        if kept in self.kept:
            self.kept.discard(kept)
            self._nudged.set()

    async def _wait_kept(self) -> bool:
        """The task waits with its claim kept (the last append, ``Patch.keep``):
        the claim's keeper renews the lease meanwhile, so no other worker
        claims it. ``True`` once there is input to take or a timer is due;
        ``False`` (the claim given back, the task left waiting) once nothing
        keeps it, or when the wait did not keep the claim."""
        if self._kept_wait is None:
            return False
        rt = self.rt
        self._give_slot()
        self.waiting_kept.set()
        shared = getattr(self.store, "shared_changes", False)
        poll = max(rt.lease_seconds / 3, rt.poll_interval) if shared else rt.poll_interval
        try:
            while True:
                if self._lost:
                    raise LostClaim()
                self._nudged.clear()
                row = await self.store.get(self.task_id)
                if row is None or row.owner_epoch != self.claim.epoch or row.status == "ended":
                    raise LostClaim()
                now = time.time()
                # A timer is woken for once: one whose firing brought no
                # input is not looked at again.
                due = row.ready_at if row.ready_at != self._kept_due else None
                if due is not None and due <= now:
                    self._kept_due = due
                    await self._take_slot()
                    return True
                if row.status == "ready":  # woken: a post that does not wake leaves it
                    await self._take_slot()
                    return True
                if not self.kept:
                    await self.store.release(self.claim)
                    self._kept_wait = None
                    return False
                timeout = poll if due is None else min(poll, due - now)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._nudged.wait(), max(timeout, 0.01))
        finally:
            self.waiting_kept.clear()

    async def _take_slot(self) -> None:
        await self.rt.take_slot()
        self._slot = True

    def _give_slot(self) -> None:
        if self._slot:
            self._slot = False
            self.rt.give_slot()

    def view(self) -> TaskView:
        model = self.model
        return TaskView(state=self.state, complete=self.complete, env=self.env(),
                        post=self.post_self, extensions=self.exts, model=model,
                        agents=self.rt.agents,
                        allowed_tools=self.agent.allowed_tools if self.agent else None,
                        denied_tools=self.agent.denied_tools if self.agent else (),
                        # Asks the host for the provider on every view:
                        # build_model_request relies on it coming first.
                        count_tokens=self.counter(model), read_log=self._read_log,
                        child_tasks=self._child_ids,
                        spill_dir=self.rt.spill_root, identity=self.identity,
                        effort=self.effort, next_request=self._next_request)

    def _next_request(self) -> Any:
        """``TaskView.next_request``."""
        from noeta.core.runner.model import build_model_request

        return build_model_request(self, final=False)

    async def _read_log(self, task_id: str, after: int = 0) -> Sequence[Recorded]:
        """``TaskView.read_log``: an unknown task reads as empty."""
        if task_id != self.task_id and await self.store.get(task_id) is None:
            return []
        return await self.store.read(task_id, after)

    async def _child_ids(self, task_id: str) -> list[str]:
        return [row.task_id for row in await self.store.tasks(
            parent_id=task_id, limit=10_000)]

    def counter(self, model: str) -> Any:
        """``count_tokens(text)`` against ``model``'s provider, or ``None``
        when the provider cannot count."""
        if not model:
            return None
        try:
            provider = self.rt.provider_of(self.state, model)
        except Exception:  # noqa: BLE001 — no provider, no exact count
            return None
        count = getattr(provider, "count_tokens", None)
        if count is None:
            return None

        async def counted(text: str) -> int | None:
            try:
                result: int | None = await count(model, text)
            except Exception:  # an estimate stands in for a failed count
                log.debug("count_tokens failed", exc_info=True)
                return None
            return result

        return counted

    async def post_self(self, body: Any, dedupe_key: str | None = None) -> None:
        """Post to this task's own inbox (extensions: ``TaskView.post``)."""
        await self.store.post(Post(task_id=self.task_id, body=body, dedupe_key=dedupe_key))

    def env(self) -> Any:
        """The task's ExecEnv, made once per run (after the log is folded, so
        the task's config — its cwd — is known)."""
        if self._env is None and self.agent is not None:
            self._env = self.rt.env_for(self.state)
        return self._env

    async def complete(self, request: Any) -> Any:
        """A model call for an extension (a summary, a judge): retried like
        the main call, not recorded as a step."""
        from noeta.core.retry import complete_with_retries

        return await complete_with_retries(
            self.rt.provider_of(self.state, request.model), request, sleep=self.rt.sleep)

    async def flush(self, view: TaskView) -> None:
        if view.pending or view.creates or view.enqueued:
            events, view.pending = list(view.pending), []
            await self.append(events, creates=self.staged(view),
                              posts=await self.enqueued(view))

    async def enqueued(self, view: TaskView) -> list[Post]:
        """The posts ``view.enqueue`` staged, taken out of the view; one to a
        task that does not exist is dropped."""
        staged, view.enqueued = list(view.enqueued), []
        posts: list[Post] = []
        for body, key, target in staged:
            if target is not None and target != self.task_id:
                row = await self.store.get(target)
                if row is None:
                    log.warning("task %s: post to unknown task %s dropped", self.task_id, target)
                    continue
            else:
                target = self.task_id
                self._enqueued = True
            posts.append(Post(task_id=target, body=body, dedupe_key=key))
        return posts

    def staged(self, view: TaskView) -> list[NewTask]:
        """The children ``view.spawn`` staged, taken out of the view."""
        staged, view.creates = list(view.creates), []
        return [NewTask(task_id=c.task_id, created=c.created, queue=c.queue or self.claim.queue,
                        inbox=[c.first])
                for c in staged]

    def tool_offer(self, view: TaskView) -> ToolOffer:
        """The task's tools for this step (``offer_tools`` with the agent's
        allow and deny lists)."""
        agent = self.agent
        return offer_tools(self.exts, view, agent.allowed_tools if agent else None,
                           agent.denied_tools if agent else ())

    def tools(self, view: TaskView) -> list[Tool]:
        """The tools the task offers (``tool_offer``'s): the ones a call runs."""
        return self.tool_offer(view).tools

    @staticmethod
    def error_result(call_id: str, text: str, note: str | None = None) -> ToolFinished:
        return ToolFinished(result=ToolResult(call_id=call_id, content=[Text(text=text)],
                                              is_error=True), note=note)

    # ------------------------------------------------------------ the step

    async def _step_once(self) -> bool:
        """One boundary: take input, decide, act. ``False`` once the task has
        stopped running (waits, ends, or nothing to do)."""
        from noeta.core.runner.calls import run_calls
        from noeta.core.runner.model import call_model

        await self.take_input()
        assert self.agent is not None
        action = decide.next_action(self.state, one_shot=self.one_shot,
                                    max_steps=self.agent.step_budget)
        if isinstance(action, decide.Idle):
            return False
        if isinstance(action, decide.End):
            return not await closing.end(self, action.status, action.answer, action.error,
                                      decide_again=True)
        if isinstance(action, decide.WaitInput):
            detail: dict[str, Any] = {"answer": self.state.last_answer}
            if self.state.turn_error:
                detail = {"turn_failed": self.state.turn_error}
            due = timers.next_timer(self.state)
            waiting = Waiting(on="input", detail=detail, until=due)
            if await self._woken_by_timer(waiting):
                assert due is not None
                await timers.fire_timers(self, now=due)
                return True
            await self.wait(waiting)
            return False
        if isinstance(action, decide.Interrupted):
            from noeta.core.runner.calls import recover_calls

            return await self._guarded(recover_calls(self, action.call_ids))
        if isinstance(action, decide.MaxTurns):
            return await closing.fail_turn(self, texts.MAX_TURNS,
                                        texts.MAX_TURNS_DETAIL.format(n=action.limit))
        if isinstance(action, decide.StopTurn):
            await closing.stop_turn(self, action.call_ids)
            return True
        if isinstance(action, decide.WaitParked):
            if action.on == "question" and self.state.config.get("unattended"):
                return await closing.fail_turn(self, texts.NEEDS_HUMAN, texts.NEEDS_HUMAN_DETAIL)
            # Each parked call's detail (a question's text and options), keyed
            # by call id like an approval's, so front ends can show it.
            parked = {c.use.id: {**((c.parked or {}).get("detail") or {}),
                                 "on": (c.parked or {}).get("on")}
                      for c in self.state.open_calls if c.parked}
            await self.wait(Waiting(on=action.on, ids=action.ids,  # type: ignore[arg-type]
                                    detail=parked))
            return False
        if isinstance(action, decide.WaitApproval):
            await self.keep_waiting()
            return False
        if isinstance(action, decide.ResumeCalls):
            from noeta.core.runner.calls import resume_calls

            await self._guarded(resume_calls(self, action.call_ids))
            return True
        if isinstance(action, decide.Continue):
            return await closing.continue_after_cap(self)
        if isinstance(action, decide.RunCalls):
            return await self._guarded(run_calls(self, action.call_ids))
        return await self._guarded(call_model(self, final=isinstance(action, decide.FinalReport)))

    async def _woken_by_timer(self, waiting: Waiting) -> bool:
        """Whether the log ends with this very ``waiting`` (nothing happened
        since but plugins' records of it, such as a relay's), so the store
        woke the task for its ``until`` by the store's clock, which this
        worker's may lag. Writing it again would leave the task claimable at
        once, again and again; once per claim the timers due by then fire
        instead."""
        at = self.state.waiting_seq
        if waiting.until is None or self._timer_forced or self.state.waiting != waiting \
                or at < 1:
            return False
        tail = await self.store.read(self.task_id, after=at - 1)
        if not tail or tail[0].event != waiting or tail[-1].seq != self.state.last_seq \
                or any(not isinstance(r.event, ExtensionRecorded)
                       or r.event.extension == CORE for r in tail[1:]):
            return False
        self._timer_forced = True
        return True

    async def _guarded(self, coro: Any) -> bool:
        """Run a step so a cancel posted mid-step (or a lost claim) can
        interrupt it."""
        self._step = asyncio.create_task(coro)
        watcher = asyncio.create_task(self._watch_inbox())
        try:
            result: bool = await self._step
            return result
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            if self._lost:
                raise LostClaim() from None
            return True  # a cancel landed; the next boundary takes it
        finally:
            watcher.cancel()
            self._step = None

    async def take_input(self) -> None:
        """Take the inbox. What extensions enqueue while handling it (a
        notice decided from a signal) is taken in the same step, so it
        reaches the same model call."""
        await timers.fire_timers(self)
        for _ in range(3):
            self._enqueued = False
            try:
                items = await self.store.inbox(self.task_id, after=self.state.inbox_read)
            except msgspec.DecodeError as exc:
                raise UnreadableInbox(exc) from exc
            if not items:
                return
            await self.append([InputTaken(items=items, through=items[-1].seq)])
            if not self._enqueued:
                return
