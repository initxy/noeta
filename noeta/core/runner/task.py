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
import time
from collections.abc import Sequence
from typing import Any

from msgspec.structs import replace

from noeta.core import records as core_records
from noeta.core import registry, snapshots
from noeta.core.agent import Agent
from noeta.core.deferred import with_tool_search
from noeta.core.errors import Conflict, InboxUnread, StoreUnavailable
from noeta.core.events import (
    Cancel,
    ChildEnded,
    Event,
    InputTaken,
    Recorded,
    TaskCreated,
    TaskEnded,
    TimerFired,
    ToolFinished,
    Waiting,
)
from noeta.core.extension import Extension, TaskView, task_cwd
from noeta.core.loop import decide
from noeta.core.loop.state import TaskState, apply, continued, fold, new_state
from noeta.core.prompts import load_overrides
from noeta.core.records import CORE
from noeta.core.runner import texts
from noeta.core.runner.runtime import Runtime
from noeta.core.runner.snapshotting import SnapshotWriter
from noeta.core.store import Claim, NewTask, Post, Snapshot, patch_for, pin_history
from noeta.core.tool import Tool
from noeta.core.types import Text, Thinking, ToolResult, user

log = logging.getLogger("noeta.core.runner")


#: Continued conversations followed back from a task (``history_from``)
#: before the rest of the chain is ignored.
MAX_HISTORY_DEPTH = 64

class LostClaim(Exception):
    pass


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
        self.snapshots = SnapshotWriter(self)

    # ------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        keeper = asyncio.create_task(self._keep_claim())
        unsubscribe = self.store.on_change(self._on_change)
        try:
            await self.load()
            self.snapshots.write_loaded()
            if self.agent is None:
                # Releasing would hand it straight back to this worker; a task
                # whose agent only some workers have must be routed by queue.
                # Ending it the ordinary way tells a parent that waits on it.
                await self.end("failed", error=f"no agent named {self.state.agent!r} is "
                                               f"registered in the worker that claimed this task")
                return
            await self._steps()
        except (Conflict, LostClaim):
            log.info("task %s: claim lost; stopping without further writes", self.task_id)
        except StoreUnavailable as exc:
            # The store is unreachable: stop; the lease runs out and the task
            # is claimed again once the store is back.
            log.warning("task %s: store error, stopping: %s", self.task_id, exc)
        except Exception:
            # The log could not even be read and folded (nothing was written).
            log.exception("task %s: could not run", self.task_id)
        finally:
            unsubscribe()
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper
            await self.snapshots.finish()

    async def _steps(self) -> None:
        """Step until the task waits or ends. A bug (an extension hook, a
        provider, the runner itself) fails the turn instead of leaving the
        task ``running`` to be claimed and fail again on every lease; a
        second one in a row ends the task."""
        started = False
        failures = 0
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
                if not await self._step_once():
                    return
                failures = 0
            except (Conflict, LostClaim, StoreUnavailable, asyncio.CancelledError):
                raise
            except Exception as exc:
                log.exception("task %s: internal error", self.task_id)
                failures += 1
                started = True
                if failures > 1:
                    await self.end("failed", self.state.last_answer,
                                   f"{texts.INTERNAL_ERROR}: {exc!r}")
                    return
                if not await self.fail_turn(texts.INTERNAL_ERROR, repr(exc)):
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

    async def _record_start(self) -> bool:
        """At each pickup, before ``on_start``: record the prompt override
        files when the task first starts, and the plugins it runs with then
        and whenever a pickup finds them changed (a reload, an upgrade).
        ``False`` (the task ended failed) when a plugin it recorded is
        missing here: it never runs without it."""
        view = self.view()
        if self.state.prompt_files is None:
            await self._record_prompt_files(view)
        current = [registry.pin(e) for e in self.exts]
        pinned = self.state.plugins
        if pinned is not None:
            missing = sorted({str(p.get("name")) for p in pinned}
                             - {p["name"] for p in current})
            if missing:
                await self.flush(view)
                await self.end("failed", self.state.last_answer,
                               texts.PLUGIN_MISSING.format(names=", ".join(missing)))
                return False
            if pinned != current:
                log.info("task %s: plugins changed since it last ran", self.task_id)
        if pinned != current:
            plugins = core_records.Plugins(pins=current)
            view.record(CORE, plugins.kind, plugins.data())
        await self.flush(view)
        return True

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
        self.state = fold(records, self.reducers, state=base)

    def _use_agent(self, name: str) -> None:
        self.agent = self.rt.agents.get(name)
        self.exts = self.agent.all_extensions() if self.agent is not None else []
        self.reducers = {e.name: e.reduce for e in self.exts}

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
        try:
            state = snapshots.decode(stored.data)
        except Exception:  # noqa: BLE001 — another fold's data: fold from the start
            return False
        self._use_agent(state.agent)
        if self.agent is None or stored.version != self.fold_version():
            return False
        records = await self.store.read(self.task_id, after=stored.seq)
        self.state = fold(records, self.reducers, state=state)
        self.snapshots.known(stored.seq, stored.data, records)
        return True

    async def _history(self, first: Any, initial: dict[str, Any],
                       depth: int = 0) -> TaskState | None:
        """The conversation a task created with ``history_from`` starts
        from: its source's log up to ``history_through``, itself folded on
        the conversation that source continued (a fork of a fork, a resumed
        sub-agent resumed again)."""
        if not isinstance(first, TaskCreated) or not first.history_from \
                or depth > MAX_HISTORY_DEPTH:
            return None
        source = await self.store.read(first.history_from)
        through = first.history_through
        base = await self._history(source[0].event if source else None, initial, depth + 1)
        return continued(fold([r for r in source if through is None or r.seq <= through],
                              self.reducers, initial, state=base),
                         {e.name: e.continue_state for e in self.exts})

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
                if self._step is not None:
                    self._step.cancel()
                return
            self.claim = renewed
            self._lease_deadline = sent + self.rt.lease_seconds

    def _on_change(self, task_id: str, kind: str) -> None:
        if task_id == self.task_id and kind == "posted" and self._step is not None:
            try:
                asyncio.get_running_loop().create_task(self._check_stop())
            except RuntimeError:  # posted from another thread's loop; polling covers it
                pass

    async def _check_stop(self) -> None:
        items = await self.store.inbox(self.task_id, after=self.state.inbox_read)
        if any(isinstance(i.body, Cancel) for i in items) and self._step is not None:
            self._step.cancel()

    async def _watch_inbox(self) -> None:
        """Polling fallback for posts made by other processes."""
        while True:
            await asyncio.sleep(self.rt.poll_interval)
            with contextlib.suppress(Exception):
                await self._check_stop()

    # ------------------------------------------------------------ writing

    async def append(
        self,
        events: Sequence[Event],
        *,
        posts: Sequence[Post] = (),
        creates: Sequence[Any] = (),
    ) -> None:
        if self._lost:
            raise LostClaim()
        creates = [replace(c, created=await pin_history(self.store, c.created))
                   for c in creates]
        async with self._write_lock:
            base = self.state.last_seq
            patch = patch_for(events, self.state.inbox_read)
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
            recs = [Recorded(seq=base + i, at=at, event=e) for i, e in enumerate(events, 1)]
            for rec in recs:
                apply(self.state, rec, self.reducers)
            self.snapshots.count(events)
            if cancelled:
                raise asyncio.CancelledError()
        self.snapshots.write_later()
        view = self.view()
        for rec in recs:
            for ext in self.exts:
                try:
                    await ext.on_event(view, rec)
                except Exception:
                    log.exception("extension %s on_event failed", ext.name)
        await self.flush(view)

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

    def view(self) -> TaskView:
        model = self.model
        return TaskView(state=self.state, complete=self.complete, env=self.env(),
                        post=self.post_self, extensions=self.exts, model=model,
                        agents=self.rt.agents,
                        allowed_tools=self.agent.allowed_tools if self.agent else None,
                        denied_tools=self.agent.denied_tools if self.agent else (),
                        count_tokens=self.counter(model), read_log=self.store.read,
                        child_tasks=self._child_ids)

    async def _child_ids(self, task_id: str) -> list[str]:
        return [row.task_id for row in await self.store.tasks(parent_id=task_id, limit=10_000)]

    def counter(self, model: str) -> Any:
        """``count_tokens(text)`` against ``model``'s provider, or ``None``
        when the provider cannot count."""
        if not model:
            return None
        try:
            provider = self.rt.provider(model, self.state.tenant)
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

        response = await complete_with_retries(
            self.rt.provider(request.model, self.state.tenant), request, sleep=self.rt.sleep)
        await self.rt.charge(self.state.tenant, request.model, response.usage)
        return response

    async def flush(self, view: TaskView) -> None:
        if view.pending or view.creates or view.enqueued:
            events, view.pending = list(view.pending), []
            await self.append(events, creates=self.staged(view),
                              posts=await self.enqueued(view))

    async def enqueued(self, view: TaskView) -> list[Post]:
        """The posts ``view.enqueue`` staged, taken out of the view; one to a
        task that is not of this task's tenant is dropped."""
        staged, view.enqueued = list(view.enqueued), []
        posts: list[Post] = []
        for body, key, target in staged:
            if target is not None and target != self.task_id:
                row = await self.store.get(target)
                if row is None or row.tenant != self.state.tenant:
                    log.warning("task %s: post to unknown task %s dropped", self.task_id, target)
                    continue
            else:
                target = self.task_id
                self._enqueued = True
            posts.append(Post(task_id=target, body=body, dedupe_key=key))
        return posts

    @staticmethod
    def staged(view: TaskView) -> list[NewTask]:
        """The children ``view.spawn`` staged, taken out of the view."""
        staged, view.creates = list(view.creates), []
        return [NewTask(task_id=c.task_id, created=c.created, queue=c.queue, inbox=[c.first])
                for c in staged]

    def tools(self, view: TaskView) -> list[Tool]:
        allowed = self.agent.allowed_tools if self.agent is not None else None
        denied = set(self.agent.denied_tools if self.agent is not None else ())
        tools: list[Tool] = []
        seen: set[str] = set()
        for ext in self.exts:
            for t in ext.tools(view):
                if (allowed is not None and t.name not in allowed) or t.name in denied:
                    continue
                if t.name not in seen:
                    seen.add(t.name)
                    tools.append(t)
        return with_tool_search(tools)

    @staticmethod
    def error_result(call_id: str, text: str) -> ToolFinished:
        return ToolFinished(result=ToolResult(call_id=call_id, content=[Text(text=text)],
                                              is_error=True))

    # ------------------------------------------------------------ the step

    async def _step_once(self) -> bool:
        """One boundary: take input, decide, act. ``False`` once the task has
        stopped running (waits, ends, or nothing to do)."""
        from noeta.core.runner.calls import run_calls
        from noeta.core.runner.model import call_model

        await self._take_input()
        assert self.agent is not None
        action = decide.next_action(self.state, one_shot=self.one_shot,
                                    max_steps=self.agent.step_budget)
        if isinstance(action, decide.Idle):
            return False
        if isinstance(action, decide.End):
            return not await self.end(action.status, action.answer, action.error,
                                      decide_again=True)
        if isinstance(action, decide.WaitInput):
            detail: dict[str, Any] = {"answer": self.state.last_answer}
            if self.state.turn_error:
                detail = {"turn_failed": self.state.turn_error}
            due = self.next_timer()
            await self.append([Waiting(on="input", detail=detail, until=due)])
            return False
        if isinstance(action, decide.Interrupted):
            from noeta.core.runner.calls import recover_calls

            return await self._guarded(recover_calls(self, action.call_ids))
        if isinstance(action, decide.MaxTurns):
            return await self.fail_turn(texts.MAX_TURNS,
                                        texts.MAX_TURNS_DETAIL.format(n=action.limit))
        if isinstance(action, decide.StopTurn):
            await self._stop_turn(action.call_ids)
            return True
        if isinstance(action, decide.WaitParked):
            if action.on == "question" and self.state.config.get("unattended"):
                return await self.fail_turn(texts.NEEDS_HUMAN, texts.NEEDS_HUMAN_DETAIL)
            # Each parked call's detail (a question's text and options), keyed
            # by call id like an approval's, so front ends can show it.
            parked = {c.use.id: {**((c.parked or {}).get("detail") or {}),
                                 "on": (c.parked or {}).get("on")}
                      for c in self.state.open_calls if c.parked}
            await self.append([Waiting(on=action.on, ids=action.ids,  # type: ignore[arg-type]
                                       detail=parked)])
            return False
        if isinstance(action, decide.ResumeCalls):
            from noeta.core.runner.calls import resume_calls

            await self._guarded(resume_calls(self, action.call_ids))
            return True
        if isinstance(action, decide.Continue):
            return await self.continue_after_cap()
        if isinstance(action, decide.RunCalls):
            return await self._guarded(run_calls(self, action.call_ids))
        return await self._guarded(call_model(self, final=isinstance(action, decide.FinalReport)))

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

    async def _take_input(self) -> None:
        """Take the inbox. What extensions enqueue while handling it (a
        notice decided from a signal) is taken in the same step, so it
        reaches the same model call."""
        await self._fire_timers()
        for _ in range(3):
            self._enqueued = False
            items = await self.store.inbox(self.task_id, after=self.state.inbox_read)
            if not items:
                return
            await self.append([InputTaken(items=items, through=items[-1].seq)])
            if not self._enqueued:
                return

    async def _fire_timers(self) -> None:
        """Post the due timers to the inbox while the task is idle. A turn
        in progress keeps them for later, also while it is parked on an
        approval, a question or children: Claude Code fires a scheduled
        prompt only once it is idle, so a timer due meanwhile fires (once)
        when the turn ends. The dedupe key, the record that
        set the timer, makes a crash between post and take harmless and
        lets a timer set again for the same name and time fire again."""
        st = self.state
        if st.turn_open or st.calls or st.ended is not None or not st.timers:
            return
        now = time.time()
        for name, timer in sorted(st.timers.items(), key=lambda kv: kv[1]["at"]):
            if timer["at"] <= now:
                await self.store.post(Post(task_id=self.task_id, body=TimerFired(name=name),
                                           dedupe_key=f"timer:{name}:{timer['seq']}"))

    def next_timer(self) -> float | None:
        """When the earliest timer is due, or ``None`` without timers."""
        return min((t["at"] for t in self.state.timers.values()), default=None)

    async def _stop_turn(self, call_ids: Sequence[str]) -> None:
        stopped = core_records.TurnStopped()
        if not call_ids and self.state.model_attempts:
            # Stopped while the reply streamed: the streamed text and the
            # reasoning finished before it stay in the conversation, and the
            # marker opens the person's next turn.
            stopped = core_records.TurnStopped(
                partial="".join(self.partial), marker=texts.INTERRUPTED_MARKER,
                thinking=list(self.thinking) or None)
        self.partial, self.thinking = [], []
        await self._close_calls(call_ids, texts.STOPPED_CALL_ERROR, core_records.event(stopped),
                                "the parent's turn was stopped")

    async def _close_calls(self, call_ids: Sequence[str], error: str, closing: Event,
                           cancel_reason: str, *, started_error: str | None = None) -> None:
        """Answer the open calls (parked ones are withdrawn), cancel the
        children they wait on, and append ``closing`` with the last results,
        so the conversation never keeps a call without a result. A call that
        started gets ``started_error`` when given (its outcome is unknown)."""
        from noeta.core.runner.calls import resume_calls

        parked = [c for c in self.state.calls if c.use.id in call_ids and c.parked]
        children = [k for c in parked if (c.parked or {}).get("on") == "children"
                    for k in (c.parked or {}).get("keys") or []]
        if parked:
            await resume_calls(self, [c.use.id for c in parked], withdrawn=True)
        rest = [c for c in self.state.calls if c.use.id in call_ids and not c.parked
                and c.result is None]
        events: list[Event] = [
            self.error_result(c.use.id, started_error if c.started and started_error else error)
            for c in rest]
        events.append(closing)
        await self.append(events, posts=[
            Post(task_id=k, body=Cancel(reason=cancel_reason))
            for k in children if self.state.children.get(k) is None])

    async def end(self, status: str, answer: str = "", error: str = "",
                  *, decide_again: bool = False) -> bool:
        """End the task, unless its inbox got something it has not taken:
        the end never lands over an unread message (a ``hold_lost`` there
        would otherwise be heard by no one). The input is taken first; with
        ``decide_again`` that returns ``False`` so the caller decides anew,
        otherwise (an end that stands whatever arrived) it ends after it."""
        while True:
            # Children die with their parent.
            posts: list[Post] = [Post(task_id=c, body=Cancel(reason=f"parent task {status}"))
                                 for c in self.state.live_children()]
            if self.state.parent_id:
                posts.append(Post(
                    task_id=self.state.parent_id,
                    body=ChildEnded(child_id=self.task_id, status=status,  # type: ignore[arg-type]
                                    answer=answer, error=error,
                                    last_usage=self.state.last_usage,
                                    tool_calls=self.state.tool_calls),
                    dedupe_key=f"child:{self.task_id}",
                ))
            try:
                await self.append([TaskEnded(status=status, answer=answer, error=error)],  # type: ignore[arg-type]
                                  posts=posts)
                return True
            except InboxUnread:
                await self._take_input()
                if decide_again:
                    return False

    async def fail_turn(self, reason: str, detail: str = "") -> bool:
        """A turn that cannot go on. A one-shot task ends failed; an
        interactive one keeps its conversation and waits for the next
        message (0.6's ``turn_failed``)."""
        assert self.agent is not None
        if self.one_shot:
            error = f"{reason}: {detail}" if detail else reason
            await self.end("failed", self.state.last_answer, error)
            return False
        await self._close_calls(
            [c.use.id for c in self.state.open_calls], f"{texts.TURN_FAILED_CALL_ERROR}{reason}",
            core_records.event(core_records.TurnFailed(reason=reason, detail=detail)),
            "the parent's turn failed", started_error=texts.INTERRUPTED_CALL_ERROR)
        return True

    async def continue_after_cap(self, *, thinking_only: bool = False) -> bool:
        recoveries = 0
        for m in reversed(self.state.messages):
            if m.role == "user" and m.origin == "system" and m.text in (
                    texts.OUTPUT_LIMIT_RESUME_NUDGE, texts.OUTPUT_LIMIT_THINKING_NUDGE):
                recoveries += 1
            elif m.role == "user":
                break
        if recoveries >= texts.MAX_OUTPUT_TOKENS_RECOVERY:
            return await self.fail_turn(texts.LLM_TRUNCATED,
                                        "the response kept hitting the output token limit")
        nudge = texts.OUTPUT_LIMIT_THINKING_NUDGE if thinking_only \
            else texts.OUTPUT_LIMIT_RESUME_NUDGE
        await self.append([core_records.event(core_records.Continue(),
                                         message=user(nudge, origin="system"))])
        return True
