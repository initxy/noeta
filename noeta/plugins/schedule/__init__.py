"""Scheduled prompts as Claude Code has them: ``ScheduleWakeup`` paces a
self-paced ``/loop``; ``CronCreate`` / ``CronList`` / ``CronDelete``
(deferred) run a prompt on a cron schedule or once; ``/loop`` sets either
up.

Every wake-up and job is a kernel timer recorded in the task's log, so it
survives restarts and fires on whichever worker holds the task (Claude Code
keeps session jobs in memory). A prompt that is a slash command, or a loop
sentinel, is expanded when it is scheduled; its text arrives as the next
message when the timer fires while the task is idle.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from noeta.core.commands import expand_with
from noeta.core.events import InputTaken, ModelReplied, Recorded, TimerFired
from noeta.core.extension import Command, CommandContext, Extension, TaskView
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.plugins.schedule import cron, texts
from noeta.toolkit.tools import SCHEMA_DIALECT, load_description

__all__ = ["Schedule"]

NAME = "schedule"
#: The kernel timer of the loop's next wake-up, and a cron job's prefix.
WAKEUP_TIMER = "schedule.wakeup"
JOB_TIMER = "schedule.job:"


def _when(at: float) -> str:
    return datetime.fromtimestamp(at).astimezone().strftime("%a %b %d %H:%M %Z").strip()


def _trim(text: str, n: int) -> str:
    one = " ".join(text.split())
    return one if len(one) <= n else one[: n - 1] + "…"


class Schedule(Extension):
    """``ScheduleWakeup`` (always offered), the deferred cron tools and the
    ``/loop`` command. Constructor: ``clock`` (epoch seconds; tests)."""

    name = NAME

    def __init__(self, *, clock: Any = time.time) -> None:
        self.clock = clock

    # ------------------------------------------------------------ state

    def initial_state(self) -> dict[str, Any]:
        return {"jobs": {}, "loop": {}}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        st = {**self.initial_state(), **(state or {})}
        jobs, loop = dict(st["jobs"]), dict(st["loop"])
        if kind == "job":
            jobs[data["id"]] = data
        elif kind == "job_deleted":
            jobs.pop(data["id"], None)
        elif kind == "job_next":
            if data["id"] in jobs:
                jobs[data["id"]] = {**jobs[data["id"]], "next_at": data["next_at"]}
        elif kind == "wakeup":
            loop = {**loop, **data, "pending": True, "armed": True, "keepalives": 0}
        elif kind == "keepalive":
            loop = {**loop, "pending": True, "armed": True,
                    "keepalives": int(loop.get("keepalives") or 0) + 1}
        elif kind == "tick":
            loop = {**loop, "pending": False, "armed": False}
        elif kind == "stopped":
            loop = {}
        return {"jobs": jobs, "loop": loop}

    def continue_state(self, state: Any) -> Any:
        """A continuation starts with no jobs and no loop: their kernel
        timers stay with the source task, which keeps firing them, so
        copies here would be listed and never fire."""
        return self.initial_state()

    def _st(self, state: Any) -> dict[str, Any]:
        return {**self.initial_state(), **(state or {})}

    # ------------------------------------------------------------ surface

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return (WakeupTool(self, view), CronCreateTool(self, view), CronListTool(self, view),
                CronDeleteTool(self, view))

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return (Command(name=texts.LOOP_COMMAND, description=texts.LOOP_DESCRIPTION,
                        template=texts.LOOP_TEMPLATE, argument_hint=texts.LOOP_HINT),)

    async def expand(self, view: TaskView, prompt: str, sentinel_text: str) -> str:
        """What a scheduled prompt sends when it fires."""
        if prompt.strip() in (texts.DYNAMIC_SENTINEL, texts.CRON_SENTINEL):
            return sentinel_text if prompt.strip() == texts.DYNAMIC_SENTINEL \
                else texts.CRON_TICK
        cwd = view.cwd
        try:
            ctx = CommandContext(cwd=cwd, task_id=view.task_id)
            expanded = await expand_with(view.extensions, ctx, prompt.strip())
        except Exception:  # noqa: BLE001 — a command that fails to expand is sent as typed
            expanded = None
        return expanded or prompt

    # ------------------------------------------------------------ firing

    async def on_start(self, view: TaskView) -> None:
        """Finish a firing a crash cut short: the take that fired a timer
        is in the log, what it decided (re-arming a recurring job, deleting
        a one-shot one, the loop's tick) is not. A job or a pending loop
        whose kernel timer is gone is exactly that."""
        st = self._st(view.ext_state(self.name))
        timers = view.state.timers
        for job_id in sorted(st["jobs"]):
            if JOB_TIMER + job_id not in timers:
                self._fired(view, st, JOB_TIMER + job_id, self.clock())
        if st["loop"].get("pending") and WAKEUP_TIMER not in timers:
            self._fired(view, st, WAKEUP_TIMER, self.clock())

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        st = self._st(view.ext_state(self.name))
        if isinstance(ev, InputTaken):
            for item in ev.items:
                if isinstance(item.body, TimerFired):
                    self._fired(view, st, item.body.name, item.at)
        elif isinstance(ev, ModelReplied) and not ev.response.message.tool_uses \
                and ev.response.stop_reason != "max_tokens":
            loop = st["loop"]
            # A loop iteration that ended without scheduling the next one
            # gets one fallback wake-up, so a forgotten call does not end it.
            if loop and not loop.get("armed") and not loop.get("pending") \
                    and int(loop.get("keepalives") or 0) < 1 and loop.get("text"):
                view.set_timer(WAKEUP_TIMER, self.clock() + texts.KEEPALIVE_SECONDS,
                               str(loop["text"]))
                view.record(self.name, "keepalive")

    def _fired(self, view: TaskView, st: dict[str, Any], name: str, at: float) -> None:
        if name == WAKEUP_TIMER:
            if st["loop"]:
                view.record(self.name, "tick")
            return
        if not name.startswith(JOB_TIMER):
            return
        job = st["jobs"].get(name[len(JOB_TIMER):])
        if job is None:
            return
        now = max(at, self.clock())
        if not job.get("recurring") or now - float(job["created_at"]) \
                >= cron.RECURRING_MAX_AGE_SECONDS:
            view.record(self.name, "job_deleted", {"id": job["id"]})
            return
        nxt = cron.next_fire(job["cron"], now, job["id"], recurring=True)
        if nxt is None:
            view.record(self.name, "job_deleted", {"id": job["id"]})
            return
        view.set_timer(name, nxt, str(job["text"]))
        view.record(self.name, "job_next", {"id": job["id"], "next_at": nxt})


class _Base:
    read_only = False
    clearable = False

    def __init__(self, ext: Schedule, view: TaskView) -> None:
        self.ext = ext
        self.view = view

    def _st(self, ctx: ToolContext) -> dict[str, Any]:
        return self.ext._st(ctx.state.get(NAME))


def _object(properties: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"$schema": SCHEMA_DIALECT, "type": "object",
                              "properties": properties}
    if required:
        schema["required"] = list(required)
    schema["additionalProperties"] = False
    return schema


class WakeupTool(_Base):
    name = texts.WAKEUP_TOOL
    description = load_description("noeta.plugins.schedule", "schedule_wakeup")
    input_schema = _object({
        "delaySeconds": {"type": "number", "description": (
            "Seconds from now until the wake-up; the runtime clamps it to [60, 3600]. "
            "Required unless `stop` is true.")},
        "reason": {"type": "string", "description": (
            "One short, specific sentence on why this delay; shown to the user. Required "
            "unless `stop` is true.")},
        "prompt": {"type": "string", "description": (
            "The /loop input the wake-up fires. Pass the same /loop input back verbatim every "
            "turn, so the next firing runs the loop again. An autonomous /loop (no user "
            f"prompt) passes the literal `{texts.DYNAMIC_SENTINEL}` instead (not "
            f"`{texts.CRON_SENTINEL}`, which belongs to CronCreate loops). Required unless "
            "`stop` is true.")},
        "stop": {"type": "boolean", "description": (
            "true ends the self-paced loop now instead of scheduling a wake-up; every other "
            "field is then ignored and nothing fires again.")},
        "noop": {"type": "boolean", "description": (
            "true: nothing changed (you checked and have nothing to report). false: something "
            "worth keeping happened (a file edited, a message posted, progress made, a "
            "finding). Consecutive true iterations are folded together in the user's view. "
            "Required unless `stop` is true.")},
    })

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        st = self._st(ctx)
        loop = st["loop"]
        if input.get("stop") is True:
            cancelled = 1 if loop.get("pending") else 0
            ctx.clear_timer(WAKEUP_TIMER)
            ctx.record(NAME, "stopped", {})
            text = texts.WAKEUP_STOPPED.format(cancelled=cancelled) if cancelled \
                else texts.WAKEUP_STOPPED_NONE
            return text_output(text, stopped=True, cancelled=cancelled)
        delay, reason = input.get("delaySeconds"), input.get("reason")
        if not isinstance(delay, (int, float)) or isinstance(delay, bool) \
                or not isinstance(reason, str):
            return text_output(texts.WAKEUP_NEEDS_DELAY, is_error=True)
        prompt = input.get("prompt")
        if not isinstance(prompt, str):
            return text_output(texts.WAKEUP_NEEDS_PROMPT, is_error=True)
        if not isinstance(input.get("noop"), bool):
            return text_output(texts.WAKEUP_NEEDS_NOOP, is_error=True)
        now = self.ext.clock()
        last = float(loop.get("scheduled_for") or 0)
        started = float(loop.get("started_at") or now)
        if not loop or now > last + texts.MAX_DELAY_SECONDS:
            started = now  # a new chain (or one long idle) starts its clock again
        if now - started >= cron.RECURRING_MAX_AGE_SECONDS:
            ctx.clear_timer(WAKEUP_TIMER)
            ctx.record(NAME, "stopped", {})
            return text_output(texts.WAKEUP_AGED_OUT)
        clamped = min(max(float(delay), texts.MIN_DELAY_SECONDS), texts.MAX_DELAY_SECONDS)
        at = now + clamped
        text = await self.ext.expand(self.view, prompt, texts.DYNAMIC_TICK)
        ctx.set_timer(WAKEUP_TIMER, at, text)
        ctx.record(NAME, "wakeup", {"scheduled_for": at, "started_at": started,
                                    "prompt": prompt, "text": text, "reason": reason,
                                    "noop": input["noop"]})
        ctx.notify(f"next loop iteration in {int(clamped)}s: {reason}", kind="loop_wakeup",
                   data={"scheduled_for": at, "reason": reason, "noop": input["noop"]})
        clock = datetime.fromtimestamp(at).strftime("%H:%M:%S")
        note = texts.WAKEUP_CLAMPED.format(seconds=int(clamped)) if clamped != delay else ""
        return text_output(texts.WAKEUP_SCHEDULED.format(clock=clock, seconds=int(clamped),
                                                         clamped=note),
                           scheduled_for=at, clamped_delay_seconds=clamped,
                           was_clamped=clamped != delay)


class CronCreateTool(_Base):
    name = texts.CRON_CREATE_TOOL
    description = load_description("noeta.plugins.schedule", "cron_create")
    deferred = True
    input_schema = _object({
        "cron": {"type": "string", "description": (
            'A standard 5-field cron expression in local time, "M H DoM Mon DoW" ("*/5 * * * *" '
            'is every 5 minutes; "30 14 28 2 *" is Feb 28 at 2:30pm, once).')},
        "prompt": {"type": "string", "description": "The prompt to run at each firing."},
        "recurring": {"type": "boolean", "description": (
            "true (the default): fire at every match until deleted, or until it expires after "
            "7 days. false: fire once at the next match, then delete itself; use it for "
            "\"remind me at X\" with the minute, hour, day and month pinned.")},
        "durable": {"type": "boolean", "description": (
            "Accepted for compatibility; every job is stored with the task and survives "
            "restarts either way.")},
    }, ["cron", "prompt"])

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        expr, prompt = str(input.get("cron") or ""), input.get("prompt")
        if not isinstance(prompt, str):
            return text_output("CronCreate: `prompt` must be a string.", is_error=True)
        if cron.parse(expr) is None:
            return text_output(texts.CRON_INVALID.format(cron=expr), is_error=True)
        now = self.ext.clock()
        if cron.next_match(expr, now) is None:
            return text_output(texts.CRON_NEVER.format(cron=expr), is_error=True)
        st = self._st(ctx)
        if len(st["jobs"]) >= texts.MAX_JOBS:
            return text_output(texts.CRON_TOO_MANY.format(max=texts.MAX_JOBS), is_error=True)
        recurring = input.get("recurring") is not False
        job_id = secrets.token_hex(4)
        nxt = cron.next_fire(expr, now, job_id, recurring=recurring)
        assert nxt is not None
        text = await self.ext.expand(self.view, prompt, texts.DYNAMIC_TICK)
        human = cron.describe(expr)
        ctx.set_timer(JOB_TIMER + job_id, nxt, text)
        ctx.record(NAME, "job", {"id": job_id, "cron": expr, "human": human, "prompt": prompt,
                                 "text": text, "recurring": recurring, "created_at": now,
                                 "next_at": nxt})
        template = texts.CRON_SCHEDULED_RECURRING if recurring else texts.CRON_SCHEDULED_ONCE
        return text_output(template.format(id=job_id, human=human,
                                           next=texts.CRON_NEXT.format(when=_when(nxt))),
                           id=job_id, human_schedule=human, recurring=recurring, next_fire_at=nxt)


class CronListTool(_Base):
    name = texts.CRON_LIST_TOOL
    description = load_description("noeta.plugins.schedule", "cron_list")
    deferred = True
    read_only = True
    retry_safe = True
    input_schema = _object({})

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        jobs = sorted(self._st(ctx)["jobs"].values(), key=lambda j: float(j["created_at"]))
        if not jobs:
            return text_output(texts.CRON_NONE)
        now = self.ext.clock()
        lines = [f"{j['id']} — {j['human']} ({'recurring' if j['recurring'] else 'one-shot'}), "
                 f"next run {_when(max(float(j['next_at']), now))}: {_trim(j['prompt'], 80)}"
                 for j in jobs]
        return text_output("\n".join(lines))


class CronDeleteTool(_Base):
    name = texts.CRON_DELETE_TOOL
    description = load_description("noeta.plugins.schedule", "cron_delete")
    deferred = True
    input_schema = _object({"id": {"type": "string",
                                   "description": "The job id CronCreate returned."}}, ["id"])

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        job_id = str(input.get("id") or "")
        if job_id not in self._st(ctx)["jobs"]:
            return text_output(texts.CRON_UNKNOWN.format(id=job_id), is_error=True)
        ctx.clear_timer(JOB_TIMER + job_id)
        ctx.record(NAME, "job_deleted", {"id": job_id})
        return text_output(texts.CRON_DELETED.format(id=job_id))
