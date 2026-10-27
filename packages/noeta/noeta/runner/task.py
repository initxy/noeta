"""Running one claimed task: fold the log, take the inbox, act, append —
until the task waits or ends.

Two rules keep this crash- and race-safe:

* every write is ``Store.append`` with the claim's epoch and the seq this
  runner last saw, so a runner that lost its claim stops at its next write;
* ``ToolStarted`` is appended before a tool runs, so a runner that lost its
  claim cannot start new work, and a crash leaves an unfinished call that
  recovery answers with "outcome unknown" instead of running it again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence
from typing import Any

from noeta.agent import Agent
from noeta.errors import Conflict, NoetaError
from noeta.events import (
    Cancel,
    ChildEnded,
    Event,
    ExtensionRecorded,
    InputTaken,
    Recorded,
    TaskEnded,
    ToolFinished,
    Waiting,
)
from noeta.ext.base import Extension, TaskView
from noeta.loop import decide
from noeta.loop.state import CORE, TaskState, apply, fold
from noeta.runner import texts
from noeta.runner.runtime import Runtime
from noeta.store.base import Claim, Patch, Post
from noeta.tools.base import Tool
from noeta.types import Text, ToolResult, user

log = logging.getLogger("noeta.runner")


class LostClaim(Exception):
    pass


class TaskRun:
    def __init__(self, rt: Runtime, claim: Claim) -> None:
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
        self._write_lock = asyncio.Lock()
        self._env: Any = None

    # ------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        keeper = asyncio.create_task(self._keep_claim())
        unsubscribe = self.store.on_change(self._on_change)
        try:
            await self._load()
            if self.agent is None:
                # Releasing would hand it straight back to this worker; a task
                # whose agent only some workers have must be routed by queue.
                await self.append(
                    [TaskEnded(status="failed",
                               error=f"no agent named {self.state.agent!r} is registered in "
                                     f"the worker that claimed this task")],
                    patch=Patch(status="ended", end_status="failed"))
                return
            view = self.view()
            for ext in self.exts:
                await ext.on_start(view)
            await self.flush(view)
            while await self._step_once():
                pass
        except (Conflict, LostClaim):
            log.info("task %s: claim lost; stopping without further writes", self.task_id)
        except (NoetaError, OSError) as exc:
            # The store is unreachable: stop; the lease runs out and the task
            # is claimed again once the store is back.
            log.warning("task %s: store error, stopping: %s", self.task_id, exc)
        finally:
            unsubscribe()
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper

    async def _load(self) -> None:
        records = await self.store.read(self.task_id)
        first = records[0].event if records else None
        self.agent = self.rt.agents.get(getattr(first, "agent", ""))
        if self.agent is not None:
            self.exts = self.agent.all_extensions()
            self.reducers = {e.name: e.reduce for e in self.exts}
        initial = {e.name: e.initial_state() for e in self.exts}
        self.state = fold(records, self.reducers, initial)

    async def _keep_claim(self) -> None:
        interval = max(self.rt.lease_seconds / 3, 0.05)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self.store.renew(self.claim, self.rt.lease_seconds)
            except (NoetaError, OSError) as exc:
                if time.time() < self.claim.lease_until:
                    log.warning("task %s: renew failed, retrying: %s", self.task_id, exc)
                    continue
                renewed = None
            if renewed is None:
                self._lost = True
                if self._step is not None:
                    self._step.cancel()
                return
            self.claim = renewed

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
            with contextlib.suppress(NoetaError, OSError):
                await self._check_stop()

    # ------------------------------------------------------------ writing

    async def append(
        self,
        events: Sequence[Event],
        *,
        patch: Patch = Patch(),
        posts: Sequence[Post] = (),
        creates: Sequence[Any] = (),
    ) -> None:
        if self._lost:
            raise LostClaim()
        async with self._write_lock:
            base = self.state.last_seq
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
            write.result()
            now = time.time()
            recs = [Recorded(seq=base + i, at=now, event=e) for i, e in enumerate(events, 1)]
            for rec in recs:
                apply(self.state, rec, self.reducers)
            if cancelled:
                raise asyncio.CancelledError()
        view = self.view()
        for rec in recs:
            for ext in self.exts:
                try:
                    await ext.on_event(view, rec)
                except Exception:
                    log.exception("extension %s on_event failed", ext.name)
        await self.flush(view)

    def view(self) -> TaskView:
        return TaskView(state=self.state, complete=self.complete, env=self.env(),
                        post=self.post_self)

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
        from noeta.providers.retry import complete_with_retries

        return await complete_with_retries(self.rt.provider_for(request.model), request,
                                           sleep=self.rt.sleep)

    async def flush(self, view: TaskView) -> None:
        if view.pending:
            events, view.pending = list(view.pending), []
            await self.append(events)

    def tools(self, view: TaskView) -> list[Tool]:
        tools: list[Tool] = []
        seen: set[str] = set()
        for ext in self.exts:
            for t in ext.tools(view):
                if t.name not in seen:
                    seen.add(t.name)
                    tools.append(t)
        return tools

    @staticmethod
    def error_result(call_id: str, text: str) -> ToolFinished:
        return ToolFinished(result=ToolResult(call_id=call_id, content=[Text(text=text)],
                                              is_error=True))

    # ------------------------------------------------------------ the step

    async def _step_once(self) -> bool:
        """One boundary: take input, decide, act. ``False`` once the task has
        stopped running (waits, ends, or nothing to do)."""
        from noeta.runner.calls import run_calls
        from noeta.runner.model import call_model

        await self._take_input()
        assert self.agent is not None
        action = decide.next_action(self.state, one_shot=self.agent.one_shot,
                                    max_steps=self.agent.max_steps_per_turn)
        if isinstance(action, decide.Idle):
            return False
        if isinstance(action, decide.End):
            await self.end(action.status, action.answer, action.error)
            return False
        if isinstance(action, decide.WaitInput):
            detail: dict[str, Any] = {"answer": self.state.last_answer}
            if self.state.turn_error:
                detail = {"turn_failed": self.state.turn_error}
            await self.append([Waiting(on="input", detail=detail)],
                              patch=Patch(status="waiting", waiting_on="input",
                                          inbox_read_seq=self.state.inbox_read))
            return False
        if isinstance(action, decide.Interrupted):
            await self.append([self.error_result(i, texts.INTERRUPTED_CALL_ERROR)
                               for i in action.call_ids])
            return True
        if isinstance(action, decide.StopTurn):
            await self._stop_turn(action.call_ids)
            return True
        if isinstance(action, decide.WaitParked):
            await self.append([Waiting(on=action.on, ids=action.ids)],  # type: ignore[arg-type]
                              patch=Patch(status="waiting", waiting_on=action.on,
                                          inbox_read_seq=self.state.inbox_read))
            return False
        if isinstance(action, decide.ResumeCalls):
            from noeta.runner.calls import resume_calls

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
        items = await self.store.inbox(self.task_id, after=self.state.inbox_read)
        if items:
            await self.append([InputTaken(items=items, through=items[-1].seq)])

    async def _stop_turn(self, call_ids: Sequence[str]) -> None:
        from noeta.runner.calls import resume_calls

        parked = [c for c in self.state.calls if c.use.id in call_ids and c.parked]
        children = [k for c in parked if (c.parked or {}).get("on") == "children"
                    for k in (c.parked or {}).get("keys") or []]
        if parked:
            await resume_calls(self, [c.use.id for c in parked], withdrawn=True)
        rest = [i for i in call_ids if i not in {c.use.id for c in parked}]
        events: list[Event] = [self.error_result(i, texts.STOPPED_CALL_ERROR) for i in rest]
        events.append(ExtensionRecorded(extension=CORE, kind="turn_stopped"))
        await self.append(events, posts=[
            Post(task_id=k, body=Cancel(reason="the parent's turn was stopped"))
            for k in children if self.state.children.get(k) is None])

    async def end(self, status: str, answer: str = "", error: str = "") -> None:
        # Children die with their parent.
        posts: list[Post] = [Post(task_id=c, body=Cancel(reason=f"parent task {status}"))
                             for c in self.state.live_children()]
        if self.state.parent_id:
            posts.append(Post(
                task_id=self.state.parent_id,
                body=ChildEnded(child_id=self.task_id, status=status,  # type: ignore[arg-type]
                                answer=answer, error=error),
                dedupe_key=f"child:{self.task_id}",
            ))
        await self.append([TaskEnded(status=status, answer=answer, error=error)],  # type: ignore[arg-type]
                          patch=Patch(status="ended", end_status=status,
                                      inbox_read_seq=self.state.inbox_read),
                          posts=posts)

    async def fail_turn(self, reason: str, detail: str = "") -> bool:
        """A turn that cannot go on. A one-shot task ends failed; an
        interactive one keeps its conversation and waits for the next
        message (0.6's ``turn_failed``)."""
        assert self.agent is not None
        if self.agent.one_shot:
            error = f"{reason}: {detail}" if detail else reason
            await self.end("failed", self.state.last_answer, error)
            return False
        await self.append([ExtensionRecorded(extension=CORE, kind="turn_failed",
                                             data={"reason": reason, "detail": detail})])
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
        await self.append([ExtensionRecorded(extension=CORE, kind="continue",
                                             message=user(nudge, origin="system"))])
        return True
