"""Running tasks: claim, fold the log, take the inbox, act, append — until the
task waits or ends.

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
import os
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from noeta.agent import Agent
from noeta.errors import Conflict, ContextOverflow, ModelError
from noeta.events import (
    ChildEnded,
    Event,
    ExtensionRecorded,
    InputTaken,
    ModelCalled,
    ModelReplied,
    Recorded,
    TaskEnded,
    ToolFinished,
    ToolStarted,
    Waiting,
)
from noeta.execenv.base import ExecEnv
from noeta.ext.base import Allow, Ask, Deny, Extension, Gate, Rewrite, TaskView
from noeta.loop import decide
from noeta.loop.request import build_request, request_hash
from noeta.loop.state import CORE, Call, TaskState, apply, fold
from noeta.providers.base import Provider
from noeta.providers.retry import complete_with_retries
from noeta.store.base import Claim, Patch, Post, Store
from noeta.tools.base import Tool, ToolContext, ToolOutput
from noeta.types import Delta, Text, ToolResult, ToolUse, user

log = logging.getLogger("noeta.runner")

INTERRUPTED_CALL_ERROR = (
    "Interrupted before its result was recorded, so its outcome is unknown — "
    "it may have fully, partly, or not applied. Check its effect before "
    "running it again."
)
STOPPED_CALL_ERROR = "interrupted: a human stop landed during this batch"
CUT_OFF_CALL_ERROR = (
    "Your response hit the output token limit while writing this tool call, so "
    "it was not run. Make the call again with smaller input."
)
OUTPUT_LIMIT_RESUME_NUDGE = (
    "Output token limit hit. Resume directly — no apology, no recap of "
    "what you were doing. Pick up mid-thought if that is where the cut "
    "happened. Break remaining work into smaller pieces."
)
OUTPUT_LIMIT_THINKING_NUDGE = (
    "Output token limit hit while you were still reasoning, before this "
    "response produced any text or tool call. That reasoning was not kept and "
    "is not part of this conversation, so there is nothing to resume. "
    "Continue from the conversation above: reason much more briefly and "
    "produce your answer or next tool call well within the output limit."
)
#: Consecutive output-cap recoveries before the turn fails (Claude Code's limit).
MAX_OUTPUT_TOKENS_RECOVERY = 3
#: A model call that was started this many times without a reply recorded
#: (crashes mid-call) fails the task instead of looping.
MAX_MODEL_ATTEMPTS = 3

ProviderFor = Callable[[str], Provider]
EnvFor = Callable[[TaskState], ExecEnv]
OnDelta = Callable[[str, Delta], None]


def _default_env(state: TaskState) -> ExecEnv:
    from noeta.execenv.local import LocalExecEnv

    env: ExecEnv = LocalExecEnv(str(state.config.get("cwd") or os.getcwd()))
    return env


class _LostClaim(Exception):
    pass


@dataclass
class Runtime:
    """Everything a runner needs. One ``Runtime`` can run many tasks at once."""

    store: Store
    agents: dict[str, Agent]
    provider_for: ProviderFor
    env_for: EnvFor = _default_env
    worker: str = field(default_factory=lambda: f"w-{uuid.uuid4().hex[:8]}")
    queues: Sequence[str] = ("default",)
    lease_seconds: float = 30.0
    poll_interval: float = 0.5
    concurrency: int = 8
    spill_root: str | None = None
    on_delta: OnDelta | None = None
    sleep: Callable[[float], Any] = asyncio.sleep

    async def run_once(self) -> bool:
        """Claim one task and run it until it waits or ends. ``False`` when
        nothing was claimable."""
        claim = await self.store.claim(self.worker, self.queues, self.lease_seconds)
        if claim is None:
            return False
        await TaskRun(self, claim).run()
        return True

    async def run_until_idle(self) -> None:
        while await self.run_once():
            pass

    async def serve(self, stop: asyncio.Event) -> None:
        """Run tasks as they become claimable until ``stop`` is set."""
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        def nudge(_task: str, _kind: str) -> None:
            loop.call_soon_threadsafe(wake.set)

        unsubscribe = self.store.on_change(nudge)
        running: set[asyncio.Task[None]] = set()
        try:
            while not stop.is_set():
                while len(running) < self.concurrency:
                    claim = await self.store.claim(self.worker, self.queues, self.lease_seconds)
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

    # ------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        keeper = asyncio.create_task(self._keep_claim())
        unsubscribe = self.store.on_change(self._on_change)
        try:
            await self._load()
            if self.agent is None:
                # Releasing would hand it straight back to this worker; a task
                # whose agent only some workers have must be routed by queue.
                await self._append(
                    [TaskEnded(status="failed",
                               error=f"no agent named {self.state.agent!r} is registered in "
                                     f"the worker that claimed this task")],
                    patch=Patch(status="ended", end_status="failed"))
                return
            view = self._view()
            for ext in self.exts:
                await ext.on_start(view)
            await self._flush(view)
            while await self._step_once():
                pass
        except (Conflict, _LostClaim):
            log.info("task %s: claim lost; stopping without further writes", self.task_id)
        finally:
            unsubscribe()
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper

    async def _load(self) -> None:
        records = await self.store.read(self.task_id)
        first = records[0].event if records else None
        name = getattr(first, "agent", "")
        self.agent = self.rt.agents.get(name)
        if self.agent is not None:
            self.exts = self.agent.all_extensions()
            self.reducers = {e.name: e.reduce for e in self.exts}
        initial = {e.name: e.initial_state() for e in self.exts}
        self.state = fold(records, self.reducers, initial)

    async def _keep_claim(self) -> None:
        interval = max(self.rt.lease_seconds / 3, 0.05)
        while True:
            await asyncio.sleep(interval)
            renewed = await self.store.renew(self.claim, self.rt.lease_seconds)
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
        from noeta.events import Cancel

        items = await self.store.inbox(self.task_id, after=self.state.inbox_read)
        if any(isinstance(i.body, Cancel) for i in items) and self._step is not None:
            self._step.cancel()

    async def _watch_inbox(self) -> None:
        """Polling fallback for posts made by other processes."""
        while True:
            await asyncio.sleep(self.rt.poll_interval)
            await self._check_stop()

    # ------------------------------------------------------------ writing

    async def _append(
        self,
        events: Sequence[Event],
        *,
        patch: Patch = Patch(),
        posts: Sequence[Post] = (),
        creates: Sequence[Any] = (),
    ) -> None:
        if self._lost:
            raise _LostClaim()
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
        view = self._view()
        for rec in recs:
            for ext in self.exts:
                try:
                    await ext.on_event(view, rec)
                except Exception:
                    log.exception("extension %s on_event failed", ext.name)

    def _view(self) -> TaskView:
        return TaskView(state=self.state)

    async def _flush(self, view: TaskView) -> None:
        if view.pending:
            events, view.pending = list(view.pending), []
            await self._append(events)

    # ------------------------------------------------------------ the step

    async def _step_once(self) -> bool:
        """One boundary: take input, decide, act. ``False`` once the task has
        stopped running (waits, ends, or nothing to do)."""
        await self._take_input()
        assert self.agent is not None
        action = decide.next_action(self.state, one_shot=self.agent.one_shot,
                                    max_steps=self.agent.max_steps_per_turn)
        if isinstance(action, decide.Idle):
            return False
        if isinstance(action, decide.End):
            await self._end(action.status, action.answer, action.error)
            return False
        if isinstance(action, decide.WaitInput):
            await self._append([Waiting(on="input")],
                               patch=Patch(status="waiting", waiting_on="input",
                                           inbox_read_seq=self.state.inbox_read))
            return False
        if isinstance(action, decide.Interrupted):
            await self._finish_with_error(action.call_ids, INTERRUPTED_CALL_ERROR)
            return True
        if isinstance(action, decide.CutOffCalls):
            await self._finish_with_error(action.call_ids, CUT_OFF_CALL_ERROR)
            return True
        if isinstance(action, decide.StopTurn):
            events: list[Event] = [self._error_result(i, STOPPED_CALL_ERROR)
                                   for i in action.call_ids]
            events.append(ExtensionRecorded(extension=CORE, kind="turn_stopped"))
            await self._append(events)
            return True
        if isinstance(action, decide.Continue):
            return await self._continue()
        step = self._call_model() if isinstance(action, decide.CallModel) \
            else self._run_calls(action.call_ids)
        return await self._guarded(step)

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
                raise _LostClaim() from None
            return True  # a cancel landed; the next boundary takes it
        finally:
            watcher.cancel()
            self._step = None

    async def _take_input(self) -> None:
        items = await self.store.inbox(self.task_id, after=self.state.inbox_read)
        if items:
            await self._append([InputTaken(items=items, through=items[-1].seq)])

    async def _end(self, status: str, answer: str = "", error: str = "") -> None:
        posts: list[Post] = []
        if self.state.parent_id:
            posts.append(Post(
                task_id=self.state.parent_id,
                body=ChildEnded(child_id=self.task_id, status=status,  # type: ignore[arg-type]
                                answer=answer, error=error),
                dedupe_key=f"child:{self.task_id}",
            ))
        await self._append([TaskEnded(status=status, answer=answer, error=error)],  # type: ignore[arg-type]
                           patch=Patch(status="ended", end_status=status,
                                       inbox_read_seq=self.state.inbox_read),
                           posts=posts)

    @staticmethod
    def _error_result(call_id: str, text: str) -> ToolFinished:
        return ToolFinished(result=ToolResult(call_id=call_id, content=[Text(text=text)],
                                              is_error=True))

    async def _finish_with_error(self, call_ids: Sequence[str], text: str) -> None:
        await self._append([self._error_result(i, text) for i in call_ids])

    async def _continue(self) -> bool:
        recoveries = 0
        for m in reversed(self.state.messages):
            if m.role == "user" and m.origin == "system" and m.text in (
                    OUTPUT_LIMIT_RESUME_NUDGE, OUTPUT_LIMIT_THINKING_NUDGE):
                recoveries += 1
            elif m.role == "user":
                break
        if recoveries >= MAX_OUTPUT_TOKENS_RECOVERY:
            await self._end("failed", self.state.last_answer,
                            "the response kept hitting the output token limit")
            return False
        last = self.state.messages[-1]
        nudge = OUTPUT_LIMIT_RESUME_NUDGE if last.text else OUTPUT_LIMIT_THINKING_NUDGE
        await self._append([ExtensionRecorded(extension=CORE, kind="continue",
                                              message=user(nudge, origin="system"))])
        return True

    # ------------------------------------------------------------ model

    def _tools(self, view: TaskView) -> list[Tool]:
        tools: list[Tool] = []
        seen: set[str] = set()
        for ext in self.exts:
            for t in ext.tools(view):
                if t.name not in seen:
                    seen.add(t.name)
                    tools.append(t)
        return tools

    async def _call_model(self) -> bool:
        assert self.agent is not None
        if self.state.model_attempts >= MAX_MODEL_ATTEMPTS:
            await self._end("failed", error="the model call kept being interrupted")
            return False
        view = self._view()
        parts = [p for ext in self.exts for p in ext.context(view)]
        model = str(self.state.config.get("model") or self.agent.model)
        request = build_request(
            self.state,
            model=model,
            system_prompt=str(self.state.config.get("system_prompt") or self.agent.system_prompt),
            parts=parts,
            tools=self._tools(view),
            effort=self.state.config.get("effort") or self.agent.effort,
            max_tokens=self.agent.max_tokens,
            output_schema=self.agent.output_schema,
        )
        for ext in self.exts:
            request = await ext.before_model(view, request)
        await self._flush(view)
        await self._append([ModelCalled(model=request.model, request_hash=request_hash(request),
                                        attempt=self.state.model_attempts + 1)])
        on_delta = None
        if self.rt.on_delta is not None:
            sink, tid = self.rt.on_delta, self.task_id
            on_delta = lambda d: sink(tid, d)  # noqa: E731
        try:
            response = await complete_with_retries(self.rt.provider_for(request.model), request,
                                                   on_delta, sleep=self.rt.sleep)
        except ContextOverflow as exc:
            await self._end("failed", error=f"context overflow: {exc}")
            return False
        except ModelError as exc:
            await self._end("failed", error=f"model error: {exc}")
            return False
        await self._append([ModelReplied(response=response)])
        return True

    # ------------------------------------------------------------ tools

    async def _gate(self, view: TaskView, call: ToolUse) -> Gate:
        answer = self.state.answers.get(call.id)
        if answer is not None and answer.decision is not None:
            return Allow() if answer.decision == "allow" else Deny(reason="denied by the user")
        verdicts = []
        for ext in self.exts:
            try:
                v = await ext.before_tool(view, call)
            except Exception as exc:  # noqa: BLE001 — a failing check denies
                v = Deny(reason=f"permission check failed: {exc}")
            if v is not None:
                verdicts.append(v)
        for kind in (Deny, Ask, Rewrite):
            for v in verdicts:
                if isinstance(v, kind):
                    return v
        return Allow()

    async def _run_calls(self, call_ids: Sequence[str]) -> bool:
        view = self._view()
        tools = {t.name: t for t in self._tools(view)}
        calls: list[Call] = [c for c in self.state.calls if c.use.id in call_ids]
        runnable: list[tuple[ToolUse, Tool]] = []
        asks: dict[str, Ask] = {}
        for c in calls:
            use = c.use
            gate = await self._gate(view, use)
            if isinstance(gate, Rewrite):
                use = ToolUse(id=use.id, name=gate.name or use.name, input=gate.input)
                gate = Allow()
            if isinstance(gate, Deny):
                await self._append([self._error_result(use.id, gate.reason or "denied")])
                continue
            if isinstance(gate, Ask):
                asks[use.id] = gate
                continue
            tool = tools.get(use.name)
            if tool is None:
                await self._append([self._error_result(use.id, f"No such tool: {use.name}")])
                continue
            runnable.append((use, tool))
        await self._flush(view)
        i = 0
        while i < len(runnable):
            group = [runnable[i]]
            if runnable[i][1].read_only:
                while i + len(group) < len(runnable) and runnable[i + len(group)][1].read_only:
                    group.append(runnable[i + len(group)])
            await self._append([ToolStarted(call=u) for u, _ in group])
            await asyncio.gather(*(self._run_one(u, t) for u, t in group))
            i += len(group)
        if asks:
            await self._append(
                [Waiting(on="approval", ids=list(asks),
                         detail={k: {"prompt": a.prompt, **a.detail} for k, a in asks.items()})],
                patch=Patch(status="waiting", waiting_on="approval",
                            inbox_read_seq=self.state.inbox_read),
            )
            return False
        return True

    async def _run_one(self, use: ToolUse, tool: Tool) -> None:
        records: list[Event] = []

        def record(extension: str, kind: str, data: Any) -> None:
            records.append(ExtensionRecorded(extension=extension, kind=kind, data=data))

        spill = os.path.join(self.rt.spill_root, self.task_id) if self.rt.spill_root else None
        ctx = ToolContext(task_id=self.task_id, call_id=use.id, env=self.rt.env_for(self.state),
                          state=dict(self.state.ext), record=record, spill_dir=spill)
        try:
            output = await tool.run(use.input, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a tool bug becomes an error result, not a dead task
            log.exception("tool %s failed", use.name)
            output = ToolOutput(content=[Text(text=f"{type(exc).__name__}: {exc}")], is_error=True)
        view = self._view()
        for ext in self.exts:
            output = await ext.after_tool(view, use, output)
        meta = dict(output.meta)
        if output.display is not None:
            meta["display"] = output.display
        finished = ToolFinished(result=ToolResult(call_id=use.id, content=output.content,
                                                  is_error=output.is_error), meta=meta)
        await self._append([*records, *view.pending, finished])

