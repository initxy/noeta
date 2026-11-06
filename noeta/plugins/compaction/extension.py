"""The ``Compaction`` extension: Claude Code's auto-compaction and
``/compact``.

Before every model request (mid-turn included) the context is measured as
the newest reply's reported usage plus an estimate of what was appended
since. At the threshold (``window.py``) the conversation is summarized by
the main request itself with the summarize instruction appended. The newest
reply and what followed it are kept verbatim after the summary, unless that
reply answered the first request after the previous compaction (then
everything is summarized); with nothing before the newest reply worth a
summary, an automatic compaction waits. The project instructions and the
workspace context go ahead of the summary (a manual compaction puts them
after the kept messages instead), then come the kept messages, then the
files read recently and the invoked skills; the listings and the
environment are given again with the next request. When the summarize
call errs or brings no summary, a plainer fallback call is made; an
automatic compaction that still fails lets the request go out as it is, and
three failures in a row turn automatic compaction off for the task (Claude
Code's).

Three options follow Claude Code's further ways of staying in the window.
From about 80% of the effective window a summary of the older rounds is
written in the background (``auto_compact_precompute``, on by default): the
call goes out beside the task, its result comes back through the task's
inbox and is recorded, and at the threshold (or after a context overflow)
it is swapped in, the rounds since kept verbatim, with no summarize call in
that step; a summary of a conversation that has changed since is dropped.
After an idle gap longer than the prompt cache lives, older results of
tools that can be called again are cleared (``auto_compact_clear_results``).
With the one-hour cache, a task left idle with a large context is compacted
before its cache expires (``auto_compact_idle``), on a kernel timer.

Everything a step sends is a pure function of the folded log, so a runner
that restarts mid-step sends the same bytes: a compaction is the core
``Compacted`` event, carrying everything re-attached after it; a summary
written ahead, a clearing and an idle arming are this plugin's records.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import posixpath
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import msgspec

from noeta.core import records as core_records
from noeta.core.errors import ContextOverflow, ModelError, RequestStale, TurnDone, TurnFailed
from noeta.core.events import Compacted, External, InputTaken, ModelReplied, Recorded, TimerFired
from noeta.core.extension import Command, CommandContext, Deny, Extension, Reminder, TaskView
from noeta.core.loop.state import TaskState
from noeta.core.models import model_limits
from noeta.core.tool import Tool, ToolContext
from noeta.core.types import Message, ModelRequest, ModelResponse, ToolResult, Usage
from noeta.plugins.compaction import clearing, summary
from noeta.plugins.compaction.estimate import messages_tokens, prefix_tokens
from noeta.plugins.compaction.restore import recent_files, restore_files
from noeta.plugins.compaction.transcript import full_conversation
from noeta.plugins.compaction.window import (
    Window,
    check_percent,
    check_positive,
    derive_window,
    percent_from_env,
    window_from_env,
)
from noeta.toolkit import plugin_records as pr
from noeta.toolkit.local_env import is_local
from noeta.toolkit.memory_index import MEMORY_DIR_KEY, MEMORY_INDEX_FILE
from noeta.toolkit.paths import file_name_part, noeta_home, project_dir

log = logging.getLogger("noeta.compaction")

#: The restored files' place in the next request's system turn
#: (``Reminder.order``): first, ahead of the invoked skills and the lists.
RESTORED_ORDER = 10

#: Claude Code's rapid-refill breaker: a compaction (automatic, or after a
#: context overflow) needed again within this many model calls of the
#: previous one, in the same turn, is a refill; this many refills in a row
#: stop the turn instead.
RAPID_REFILL_CALLS = 3
RAPID_REFILLS_TO_STOP = 3
THRASHING_REASON = "autocompact_thrashing"
THRASHING_DETAIL = (
    f"Automatic compaction stopped: the context filled up again within "
    f"{RAPID_REFILL_CALLS} model calls of the previous compaction, "
    f"{RAPID_REFILLS_TO_STOP} times in a row. What a request still carries after a summary "
    "(the system prompt and tools, project instructions, files read again, the newest tool "
    "output) does not fit the model's context window. Read large files in smaller parts, "
    "check the model's context window is registered right, or start a new conversation.")

#: Claude Code's failure breaker: an automatic compaction whose summary
#: fails lets the request go out as it is; after this many failures in a
#: row automatic compaction is off for the task (``/compact`` and the
#: compaction after a context overflow still run).
AUTO_FAILURES_TO_STOP = 3

#: The inbox signal a summary written ahead comes back with, and Claude
#: Code's limit on the failed ones before no more are written for the task.
PRECOMPUTED_SIGNAL = "compaction.precomputed"
PRECOMPUTE_FAILURES_TO_STOP = 3

#: Idle compaction (Claude Code's numbers): the kernel timer, the smallest
#: context worth it, when in the cache's life it fires, and how late a
#: firing may be before it is dropped.
IDLE_TIMER = "compaction.idle"
IDLE_MIN_TOKENS = 200_000  # Claude Code's default
IDLE_FIRE_FRACTION = 0.9
IDLE_LATE_SECONDS = 60
IDLE_NOTICE = "Compacted while idle, before the prompt cache expired."
#: The prompt cache life idle compaction runs with: the one-hour cache's.
ONE_HOUR_CACHE_SECONDS = 3_600


def cache_life(state: TaskState) -> float | None:
    """How long, in seconds, the task's requests keep the prompt cache when
    that is the one-hour cache (``TaskState.cache_ttl``, what the newest
    reply's cache was written for); ``None`` otherwise."""
    ttl = state.cache_ttl
    return float(ttl) if ttl == ONE_HOUR_CACHE_SECONDS else None

#: The ``/compact`` command and the text it puts in the conversation.
COMMAND = "compact"
COMMAND_DESCRIPTION = ("Summarize the conversation so far to free context "
                       "(optional: what the summary should focus on)")
COMMAND_CAVEAT = (
    "<local-command-caveat>The person ran the command below themselves; it was not a request "
    "to you, and what it printed was shown to them directly. Do not answer it or act on it "
    "unless they ask; it is here only as context for the messages that follow."
    "</local-command-caveat>"
)
COMMAND_DONE = "Compacted the conversation."
_COMMAND_RE = re.compile(
    r"<command-name>/compact</command-name>[\s\S]*?<command-args>([\s\S]*?)</command-args>")


def command_text(args: str) -> str:
    """What ``/compact <args>`` sends: the caveat, the command, its output."""
    return (f"{COMMAND_CAVEAT}\n\n<command-name>/{COMMAND}</command-name>\n"
            f"            <command-message>{COMMAND}</command-message>\n"
            f"            <command-args>{args.strip()}</command-args>\n\n"
            f"<local-command-stdout>{COMMAND_DONE}</local-command-stdout>")


@dataclass(frozen=True)
class Precomputed:
    """A summary written ahead of the threshold: of the first ``cut``
    messages as they were (``digest``) after ``compactions`` compactions."""

    note: str
    cut: int
    digest: str
    compactions: int
    usage: Usage = field(default_factory=Usage)
    head_dropped: bool = False


@dataclass(frozen=True)
class CompactionState:
    compactions: int = 0
    #: Replies the last compaction kept after its summary.
    kept_replies: int = 0
    #: Files read again after the last compaction, owed to the next request.
    restored: tuple[str, ...] = ()
    #: The newest history seq when the last compaction ran and the files it
    #: restored in full: the next restore's candidates are the files touched
    #: after that, then these (``None``: a log from before they were
    #: recorded, where every touch counts).
    seen_through: int | None = None
    restored_paths: tuple[str, ...] | None = None
    #: Where the last compaction ran (the person's newest prompt, by log seq,
    #: and model calls in that turn) and the automatic compactions in a row that each came
    #: within ``RAPID_REFILL_CALLS`` calls of the one before.
    turn: int = -1
    step: int = 0
    rapid_refills: int = 0
    #: Automatic compactions in a row whose summary failed, and where the
    #: last one failed (the person's newest prompt, model calls in that turn): the
    #: request it was for went out as it was, so a runner rebuilding that
    #: request does not try again.
    failures: int = 0
    failed_at: tuple[int, int] | None = None
    #: The ``threshold`` record last published: threshold, window, auto.
    published: tuple[int, int, bool] | None = None
    #: The summary written ahead, until a compaction uses or outdates it,
    #: and how many failed.
    precomputed: Precomputed | None = None
    precompute_failures: int = 0
    #: The id of the summary being written ahead: only a result that names
    #: it is taken, on whichever worker holds the task then.
    job: str | None = None
    #: Calls whose results are cleared, the tokens the last clearing saved,
    #: and the log seq of the reply whose reported usage still counts them.
    cleared: tuple[str, ...] = ()
    cleared_saved: int = 0
    cleared_reply: int = -1
    #: The idle compaction armed: the newest model call's time then, and
    #: when the timer fires.
    idle: tuple[float, float] | None = None


def _last_reply(messages: Sequence[Message]) -> int:
    return max((i for i, m in enumerate(messages) if m.role == "assistant"), default=-1)


def _digest(state: TaskState, cut: int) -> str:
    """What the first ``cut`` messages are: a summary of them fits only
    while this is unchanged."""
    data = msgspec.json.encode([state.message_seqs[:cut], state.messages[:cut]])
    return hashlib.sha256(data).hexdigest()


class Compaction(Extension):
    """Keeps a long task inside its model's context window.

    Constructor arguments (a resumed task must be given the same values,
    since they shape the request bytes):

    - ``auto_compact_window``: cap, in tokens, on the window the threshold
      derives from; default ``$NOETA_AUTO_COMPACT_WINDOW``, else the
      model's window.
    - ``auto_compact_percent``: fire at this percentage of the effective
      window when that is lower than the default threshold; default
      ``$NOETA_AUTOCOMPACT_PCT_OVERRIDE``.
    - ``auto``: compact automatically (``/compact`` works either way).
    - ``compaction_model``: another model for the summarize call only.
    - ``restore_files`` / ``read_tool``: re-read recently used files after
      the summary through the task's ``Read`` tool (or this one).
    - ``transcripts`` / ``home``: write the whole conversation to
      ``<home>/projects/<workspace>/<task id>.jsonl`` at each compaction
      (local workspaces only) and point the summary at it.
    - ``auto_compact_precompute``: write the summary in the background from
      about 80% of the effective window, to swap in at the threshold.
    - ``auto_compact_clear_results``: once more than
      ``auto_compact_clear_idle_seconds`` passed since the previous model
      call (the cache is cold), clear older results of ``clearable`` tools.
    - ``auto_compact_idle``: with the one-hour prompt cache
      (:func:`cache_life`), compact a large idle task before its cache
      expires.
    - ``clock``: epoch seconds (tests).
    """

    name = "compaction"

    def __init__(
        self,
        *,
        auto_compact_window: int | None = None,
        auto_compact_percent: float | None = None,
        auto: bool = True,
        compaction_model: str | None = None,
        restore_files: bool = True,
        read_tool: Tool | None = None,
        transcripts: bool = True,
        home: str | None = None,
        name: str | None = None,
        auto_compact_precompute: bool = True,
        auto_compact_clear_results: bool = False,
        auto_compact_clear_idle_seconds: float = clearing.IDLE_SECONDS,
        auto_compact_idle: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        check_positive("auto_compact_window", auto_compact_window)
        if isinstance(auto_compact_clear_idle_seconds, bool) \
                or not isinstance(auto_compact_clear_idle_seconds, (int, float)) \
                or auto_compact_clear_idle_seconds <= 0:
            raise ValueError("auto_compact_clear_idle_seconds must be a positive number; "
                             f"got {auto_compact_clear_idle_seconds!r}")
        check_percent(auto_compact_percent)
        if name:
            self.name = name
        self.auto_compact_window = auto_compact_window or window_from_env()
        self.auto_compact_percent = auto_compact_percent or percent_from_env()
        self.auto = auto
        self.compaction_model = compaction_model
        self.restore = restore_files
        self.read_tool = read_tool
        self.transcripts = transcripts
        self.home = Path(home) if home else None
        self.precompute = auto_compact_precompute
        self.clear_results = auto_compact_clear_results
        self.clear_idle_seconds = float(auto_compact_clear_idle_seconds)
        self.idle = auto_compact_idle
        self.clock = clock
        #: Summaries being written ahead in this process, by task id, with
        #: their job id. A finished one stays until its result is taken (here
        #: or by another worker: the log no longer names it), so a step
        #: between its post and that take does not start another.
        self._jobs: dict[str, tuple[asyncio.Future[None], str]] = {}

    # ------------------------------------------------------------ state

    def initial_state(self) -> CompactionState:
        return CompactionState()

    def reduce(self, state: Any, kind: str, data: Any) -> CompactionState:
        st = state if isinstance(state, CompactionState) else CompactionState()
        if not isinstance(data, Mapping):
            return st
        try:
            return self._reduce(st, kind, data)
        except (TypeError, ValueError, msgspec.ValidationError):
            log.warning("compaction: ignored a malformed %r record", kind)
            return st

    @staticmethod
    def _reduce(st: CompactionState, kind: str, data: Mapping[str, Any]) -> CompactionState:
        if kind == "restored":
            return replace(st, restored=tuple(data.get("texts") or ()))
        if kind == "restored_sent":
            return replace(st, restored=())
        if kind == pr.AUTO_COMPACT_KIND:
            return replace(st, published=(int(data.get("threshold") or 0),
                                          int(data.get("window") or 0),
                                          bool(data.get("auto", True))))
        if kind == "failed":
            return replace(st, failures=int(data.get("failures") or 0),
                           failed_at=(int(data.get("turn") or 0), int(data.get("step") or 0)))
        if kind == "precompute_started":
            return replace(st, job=str(data.get("job") or "") or None)
        if kind == "precomputed":
            return replace(st, precomputed=msgspec.convert(data, Precomputed), job=None)
        if kind == "precompute_failed":
            return replace(st, precompute_failures=int(data.get("failures") or 0), job=None)
        if kind == "cleared":
            return replace(st, cleared=tuple(data.get("ids") or ()),
                           cleared_saved=int(data.get("saved") or 0),
                           cleared_reply=int(data.get("reply", -1)))
        if kind == "idle_armed":
            return replace(st, idle=(float(data.get("called_at") or 0.0),
                                     float(data.get("due") or 0.0)))
        if kind == "idle_done":
            return replace(st, idle=None)
        if kind != "compacted":
            return st
        seen, paths = data.get("seen_through"), data.get("restored_paths")
        return replace(st, compactions=st.compactions + 1, failures=0, failed_at=None,
                       precomputed=None, job=None, idle=None,
                       turn=int(data.get("turn") or 0), step=int(data.get("step") or 0),
                       rapid_refills=int(data.get("rapid_refills") or 0),
                       kept_replies=int(data.get("kept_replies") or 0),
                       seen_through=seen if isinstance(seen, int) else None,
                       restored_paths=tuple(paths) if isinstance(paths, list) else None)

    def continue_state(self, state: Any) -> Any:
        """The ``threshold`` record went to the source's log; front ends
        follow the continuing task's own, so it is written there again. Where
        the last compaction ran, and what it had seen, are seqs of the
        source's log: every message of this task's own is newer (the carried
        ones sit at seq 0). A summary written ahead, the reply a clearing
        was measured against and an idle arming belong to the source."""
        return replace(state, published=None, turn=-1, failed_at=None, seen_through=0,
                       precomputed=None, job=None, cleared_reply=-1, cleared_saved=0,
                       idle=None) \
            if isinstance(state, CompactionState) else state

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        """The files read again after a compaction, first in the next
        request's system turn (Claude Code's place for them)."""
        return [Reminder(text=text, kind="restored_sent", data={}, role="system",
                         order=RESTORED_ORDER) for text in self._ext(view.state).restored]

    def _ext(self, state: TaskState) -> CompactionState:
        ext = state.ext.get(self.name)
        return ext if isinstance(ext, CompactionState) else CompactionState()

    # ------------------------------------------------------------ commands

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return (Command(name=COMMAND, description=COMMAND_DESCRIPTION,
                        argument_hint="[instructions]"),)

    async def run_command(self, ctx: CommandContext, name: str, args: str) -> str:
        if name != COMMAND:
            return await super().run_command(ctx, name, args)
        return command_text(args)

    # ------------------------------------------------------------ hooks

    def _window(self, model: str) -> Window:
        try:
            return derive_window(model, auto_compact_window=self.auto_compact_window,
                                 auto_compact_percent=self.auto_compact_percent)
        except ValueError as exc:
            raise TurnFailed("compaction_config_invalid", str(exc)) from None

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        request, state = self._clear(view, request)
        n = len(state.messages)
        command = self._command(state)
        if command is not None:
            at, args = command
            if view.complete is not None:
                cut = self._cut(state, at, manual=True)
                assert cut is not None
                extra = await self._instructions(view, "manual", args) \
                    if self._through(state, cut) is not None else None
                if extra is not None:
                    await self._compact(view, state, request, cut, reason="manual",
                                        estimated=self._tokens(state, request, at)[0],
                                        extra=extra, command_at=at)
            raise TurnDone()
        self._publish(view, request.model)
        if not self.auto or view.complete is None:
            return request
        if state.compaction is not None and state.last_usage is None:
            return request  # the first request after a compaction goes as it is
        window = self._window(request.model)
        tokens, _ = self._tokens(state, request, n)
        if tokens < window.threshold:
            if tokens >= window.precompute:
                await self._precompute(view, state, request, n)
            return request
        ready = self._ready(state)
        cut = ready.cut if ready is not None else self._cut(state, n, manual=False)
        if cut is None or self._through(state, cut) is None:
            return request
        ext = self._ext(state)
        if ready is None and (ext.failures >= AUTO_FAILURES_TO_STOP
                              or ext.failed_at == (state.prompt_seq, state.steps_in_turn)):
            return request
        rapid = self._refills_or_stop(view, state)
        if ready is not None:
            # The summary written ahead (its PreCompact hooks ran then).
            await self._compact(view, state, request, cut, reason="auto", estimated=tokens,
                                rapid_refills=rapid, ready=ready)
            raise RequestStale()
        extra = await self._instructions(view, "auto", "")
        if extra is None:
            return request
        if not await self._auto_compact(view, state, request, cut, reason="auto",
                                        estimated=tokens, extra=extra, rapid_refills=rapid):
            return request
        # Every extension builds the request again from the compacted
        # conversation (betas and notes that depend on it included).
        raise RequestStale()

    async def _auto_compact(self, view: TaskView, state: TaskState, request: ModelRequest,
                            cut: int, *, reason: str, estimated: int, extra: str,
                            rapid_refills: int = 0) -> bool:
        """An automatic compaction; ``False`` when its summary failed."""
        try:
            await self._compact(view, state, request, cut, reason=reason, estimated=estimated,
                                extra=extra, rapid_refills=rapid_refills)
        except TurnFailed as exc:
            if exc.reason != summary.SUMMARY_FAILED_REASON:
                raise
            # Claude Code's: the request goes out uncompacted (an overflow
            # then compacts after it); three failures in a row turn
            # automatic compaction off for the task.
            failures = self._ext(state).failures + 1
            log.warning("task %s: automatic compaction failed (%d in a row%s): %s",
                        view.task_id, failures,
                        "; off for this task" if failures >= AUTO_FAILURES_TO_STOP else "",
                        exc.detail)
            view.record(self.name, "failed", {"failures": failures, "turn": state.prompt_seq,
                                              "step": state.steps_in_turn,
                                              "detail": exc.detail})
            return False
        return True

    def _publish(self, view: TaskView, model: str) -> None:
        """The threshold for front ends' "N% until auto-compact", recorded
        when it changes (a model switch)."""
        try:
            window = derive_window(model, auto_compact_window=self.auto_compact_window,
                                   auto_compact_percent=self.auto_compact_percent)
        except ValueError:
            return
        published = (window.threshold, window.effective, self.auto)
        if self._ext(view.state).published != published:
            view.record(self.name, pr.AUTO_COMPACT_KIND, {
                "threshold": window.threshold, "window": window.effective, "auto": self.auto})

    async def on_context_overflow(self, view: TaskView, request: ModelRequest) -> bool:
        if view.complete is None:
            return False
        state = view.state
        n = len(state.messages)
        ready = self._ready(state)
        if ready is None and self._through(state, n) is None:
            return False
        # What a compaction re-attaches can by itself overflow the window:
        # the refill breaker stops that loop here too.
        rapid = self._refills_or_stop(view, state)
        if ready is not None:
            await self._compact(view, state, request, ready.cut, reason="overflow",
                                estimated=self._tokens(state, request, n)[0],
                                rapid_refills=rapid, ready=ready)
            return True
        extra = await self._instructions(view, "auto", "")
        if extra is None:
            return False
        await self._compact(view, state, request, n, reason="overflow",
                            estimated=self._tokens(state, request, n)[0], extra=extra,
                            rapid_refills=rapid)
        return True

    # ------------------------------------------------------------ clearing

    def _clear(self, view: TaskView, request: ModelRequest) -> tuple[ModelRequest, TaskState]:
        """``request`` with the cleared results replaced, and the state the
        step goes on with; first, once the cache has gone cold
        (``auto_compact_clear_results``), older results of clearable tools
        are cleared and recorded, and the returned state has that record
        folded in, so this step measures the context with what it saved."""
        state = view.state
        cleared: tuple[str, ...] = self._ext(state).cleared
        if self.clear_results and state.model_called_at \
                and self.clock() - state.model_called_at > self.clear_idle_seconds:
            known: dict[str, bool] = {}

            def clearable(name: str) -> bool:
                if name not in known:
                    known[name] = bool(getattr(view.find_tool(name), "clearable", False))
                return known[name]

            ids, saved = clearing.to_clear(state.messages, clearable, set(cleared))
            if ids:
                present = {b.call_id for m in state.messages for b in m.content
                           if isinstance(b, ToolResult)}
                cleared = (*(i for i in cleared if i in present), *ids)
                last = _last_reply(state.messages)
                data = {"ids": list(cleared), "saved": saved,
                        "reply": state.message_seqs[last] if last >= 0 else -1}
                view.record(self.name, "cleared", data)
                log.info("task %s: cleared %d old tool results (~%d tokens) after an idle gap",
                         view.task_id, len(ids), saved)
                state = replace(state, ext={**state.ext, self.name: self.reduce(
                    self._ext(state), "cleared", data)})
        return clearing.cleared_request(request, set(cleared)), state

    # ------------------------------------------------------------ precomputing

    def _ready(self, state: TaskState) -> Precomputed | None:
        """The summary written ahead, while it still fits the conversation:
        no compaction since, and the messages it covers unchanged."""
        ext = self._ext(state)
        ready = ext.precomputed
        if ready is None or ready.compactions != ext.compactions \
                or not 0 < ready.cut <= len(state.messages) \
                or _digest(state, ready.cut) != ready.digest:
            return None
        return ready

    async def _precompute(self, view: TaskView, state: TaskState, request: ModelRequest,
                          n: int) -> None:
        """Start writing the summary of the older rounds in the background,
        unless one is being written, is ready, or failed too often. Its
        PreCompact hooks run now; the result comes back through the inbox
        (:meth:`_took_precomputed`), so nothing outside a step writes."""
        if not self.precompute or view.post is None or view.complete is None:
            return
        ext = self._ext(state)
        job = self._jobs.get(view.task_id)
        if job is not None:
            future, job_id = job
            if not future.done() or ext.job == job_id:
                return  # being written, or posted and not taken yet
            del self._jobs[view.task_id]  # taken (on any worker), or outdated
        if ext.precompute_failures >= PRECOMPUTE_FAILURES_TO_STOP \
                or self._ready(state) is not None:
            return
        cut = self._cut(state, n, manual=False)
        if cut is None or self._through(state, cut) is None:
            return
        extra = await self._instructions(view, "auto", "")
        if extra is None:
            view.record(self.name, "precompute_failed", {
                "failures": ext.precompute_failures + 1, "detail": "declined"})
            return
        job_id = secrets.token_hex(8)
        view.record(self.name, "precompute_started", {"job": job_id})
        key = {"job": job_id, "cut": cut, "digest": _digest(state, cut),
               "compactions": ext.compactions}
        text = summary.instruction(
            view.prompt("compaction/summarize", summary.SUMMARIZE_PROMPT), extra)
        job_future = asyncio.ensure_future(self._write_ahead(
            view.complete, view.post, view.task_id, request,
            self._to_summarize(state, request, cut), text, key))
        self._jobs[view.task_id] = (job_future, job_id)
        log.info("task %s: writing a summary of %d messages ahead of the threshold",
                 view.task_id, cut)

    async def _write_ahead(self, complete: Callable[[ModelRequest], Awaitable[ModelResponse]],
                           post: Callable[..., Awaitable[None]], task_id: str,
                           request: ModelRequest, history: list[Message], text: str,
                           key: dict[str, Any]) -> None:
        """The background summarize call; its outcome, and what it cost, go
        to the task's inbox."""
        spent: list[dict[str, Any]] = []

        def spend(model: str, usage: Usage) -> None:
            spent.append({"model": model, "usage": msgspec.to_builtins(usage)})

        data: dict[str, Any] = dict(key)
        try:
            note, usage, dropped = await self._summarize(complete, task_id, request, history,
                                                         text, spend)
            data.update(note=note, usage=msgspec.to_builtins(usage), head_dropped=dropped)
        except TurnFailed as exc:
            data["failed"] = exc.detail
        except Exception as exc:  # a summary written ahead never fails the task
            log.exception("task %s: writing the summary ahead failed", task_id)
            data["failed"] = repr(exc)
        data["spent"] = spent
        try:
            await post(External(name=PRECOMPUTED_SIGNAL, data=data))
        except Exception:
            log.warning("task %s: could not hand the summary written ahead back", task_id,
                        exc_info=True)
            if self._jobs.get(task_id, (None, ""))[1] == key["job"]:
                del self._jobs[task_id]  # lost: the next step writes another

    def _took_precomputed(self, view: TaskView, data: Mapping[str, Any]) -> None:
        """A summary written ahead, taken from the inbox: its calls' cost is
        recorded, and the summary (or the failure) when it is the result of
        the job the log says is running; any other is outdated or forged."""
        job_id = data.get("job")
        if self._jobs.get(view.task_id, (None, None))[1] == job_id:
            del self._jobs[view.task_id]
        spent_items = data.get("spent")
        for item in spent_items if isinstance(spent_items, list) else ():
            try:
                spent = core_records.Spent(model=str(item.get("model") or ""),
                                           usage=msgspec.convert(item.get("usage") or {}, Usage),
                                           purpose="compaction")
            except (AttributeError, msgspec.ValidationError):
                continue
            view.record(core_records.CORE, spent.kind, spent.data())
        ext = self._ext(view.folded({self.name: self.reduce}))
        if not isinstance(job_id, str) or job_id != ext.job:
            log.info("task %s: dropped a summary written ahead for another job", view.task_id)
            return
        result = None
        if data.get("failed") is None:
            try:
                result = msgspec.convert({k: data[k] for k in (
                    "note", "cut", "digest", "compactions", "usage", "head_dropped")},
                    Precomputed)
            except (KeyError, msgspec.ValidationError):
                pass
        if result is None or not result.note:
            failures = ext.precompute_failures + 1
            log.warning("task %s: the summary written ahead failed (%d%s): %s", view.task_id,
                        failures, "; no more for this task"
                        if failures >= PRECOMPUTE_FAILURES_TO_STOP else "",
                        data.get("failed") or "a malformed result")
            view.record(self.name, "precompute_failed", {
                "failures": failures, "detail": str(data.get("failed") or "malformed")})
            return
        view.record(self.name, "precomputed", msgspec.to_builtins(result))

    # ------------------------------------------------------------ idle

    async def on_start(self, view: TaskView) -> None:
        """Finish an idle firing a crash cut short: the take of its timer is
        in the log, what it decided is not."""
        if self._ext(view.state).idle is not None and IDLE_TIMER not in view.state.timers:
            await self._idle_fired(view, self.clock())

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        if isinstance(ev, InputTaken):
            for item in ev.items:
                body = item.body
                if isinstance(body, External) and body.name == PRECOMPUTED_SIGNAL \
                        and isinstance(body.data, Mapping):
                    self._took_precomputed(view, body.data)
                elif isinstance(body, TimerFired) and body.name == IDLE_TIMER:
                    await self._idle_fired(view, item.at)
        elif isinstance(ev, ModelReplied):
            self._arm_idle(view)

    def _arm_idle(self, view: TaskView) -> None:
        """At a turn's end, with the one-hour cache and a large context, set
        the timer that compacts the idle task before its cache expires (at
        ``IDLE_FIRE_FRACTION`` of the cache's life after the newest model
        call); otherwise take back one set earlier."""
        state = view.state
        if state.turn_open or state.calls:
            return
        life = cache_life(state)
        usage = state.last_usage
        if self.idle and self.auto and life is not None and usage is not None \
                and not state.parent_id and usage.input + usage.output >= IDLE_MIN_TOKENS:
            due = state.model_called_at + IDLE_FIRE_FRACTION * life
            view.set_timer(IDLE_TIMER, due)
            view.record(self.name, "idle_armed", {"called_at": state.model_called_at,
                                                  "due": due})
        elif self._ext(state).idle is not None or IDLE_TIMER in state.timers:
            view.clear_timer(IDLE_TIMER)
            view.record(self.name, "idle_done", {"outcome": "disarmed"})

    async def _idle_fired(self, view: TaskView, at: float) -> None:
        """The idle timer fired at ``at``: compact, unless a newer request
        went out, the firing is late, the task is busy or the context is now
        small. The outcome is recorded either way."""
        state = view.state
        ext = self._ext(state)
        if ext.idle is None:
            return
        called_at, due = ext.idle
        outcome = None
        if not self.idle:
            outcome = "idle_off"
        elif not self.auto:
            outcome = "auto_off"
        elif state.model_called_at != called_at:
            outcome = "newer_request"
        elif at - due > IDLE_LATE_SECONDS:
            outcome = "lapsed"
        elif state.turn_open or state.calls or state.ended is not None:
            outcome = "busy"
        elif view.complete is None or view.next_request is None:
            outcome = "no_runner"
        if outcome is not None:
            view.record(self.name, "idle_done", {"outcome": outcome})
            return
        assert view.next_request is not None
        request = clearing.cleared_request(view.next_request(), set(ext.cleared))
        n = len(state.messages)
        tokens = self._tokens(state, request, n)[0]
        ready = self._ready(state)
        cut = ready.cut if ready is not None else self._cut(state, n, manual=False)
        if tokens < IDLE_MIN_TOKENS or cut is None or self._through(state, cut) is None \
                or (ready is None and ext.failures >= AUTO_FAILURES_TO_STOP):
            view.record(self.name, "idle_done", {"outcome": "not_needed"})
            return
        if ready is not None:
            await self._compact(view, state, request, cut, reason="idle", estimated=tokens,
                                ready=ready)
        else:
            extra = await self._instructions(view, "auto", "")
            if extra is None or not await self._auto_compact(
                    view, state, request, cut, reason="idle", estimated=tokens, extra=extra):
                view.record(self.name, "idle_done", {"outcome": "failed"})
                return
        view.notify(IDLE_NOTICE)

    # ------------------------------------------------------------ measuring

    @staticmethod
    def _command(state: TaskState) -> tuple[int, str] | None:
        """The newest message, when it is a ``/compact`` nobody answered:
        its index and arguments."""
        if not state.messages or state.steps_in_turn:
            return None
        at = len(state.messages) - 1
        while at >= 0 and state.messages[at].origin in ("system", "memory"):
            at -= 1
        if at < 0:
            return None
        m = state.messages[at]
        if not m.from_person:
            return None
        match = _COMMAND_RE.search(m.text)
        return (at, match.group(1).strip()) if match else None

    def _tokens(self, state: TaskState, request: ModelRequest, n: int) -> tuple[int, int]:
        """``(context, before)``: the context of the first ``n`` messages, and
        of those up to and including the newest reply. As Claude Code's: the
        newest reply's reported usage plus the estimate of what came after
        it; the estimate of the whole only while no reply reported usage.
        A clearing since that reply takes what it saved off its usage."""
        prefix = prefix_tokens(request.system, request.tools)
        messages = request.messages[:n]
        last = _last_reply(messages)
        usage = state.last_usage
        if usage is None or last < 0:
            return (prefix + messages_tokens(messages),
                    prefix + messages_tokens(messages[:last + 1]))
        before = usage.input + usage.output
        ext = self._ext(state)
        if ext.cleared_saved and last < len(state.message_seqs) \
                and state.message_seqs[last] == ext.cleared_reply:
            before = max(0, before - ext.cleared_saved)
        return before + messages_tokens(messages[last + 1:]), before

    def _cut(self, state: TaskState, n: int, *, manual: bool) -> int | None:
        """How many leading messages the summary covers: everything before
        the newest reply, which stays with what followed it (Claude Code's);
        all ``n`` when that reply answered the first request after the
        previous compaction. With no earlier reply to summarize, an
        automatic compaction waits (``None``) and a manual one takes all."""
        last = _last_reply(state.messages[:n])
        if last < 0 or not any(m.role == "assistant" for m in state.messages[:last]):
            return n if manual else None
        replies = sum(1 for m in state.messages[:n] if m.role == "assistant")
        if state.compaction is not None and replies == self._ext(state).kept_replies + 1:
            return n
        return last

    def _refills_or_stop(self, view: TaskView, state: TaskState) -> int:
        """The refill count a compaction now would make: one more than the
        last one's when that ran in this turn fewer than
        ``RAPID_REFILL_CALLS`` model calls ago, else 0. At
        ``RAPID_REFILLS_TO_STOP`` the turn fails instead."""
        ext = self._ext(state)
        calls = state.steps_in_turn - ext.step
        rapid = ext.rapid_refills + 1 if ext.compactions and ext.turn == state.prompt_seq \
            and 0 <= calls < RAPID_REFILL_CALLS else 0
        if rapid >= RAPID_REFILLS_TO_STOP:
            log.warning("task %s: compaction refilled %d times in a row; stopping",
                        view.task_id, rapid)
            raise TurnFailed(THRASHING_REASON, THRASHING_DETAIL)
        return rapid

    def _to_summarize(self, state: TaskState, request: ModelRequest, cut: int
                      ) -> list[Message]:
        """The first ``cut`` messages of ``request`` as a summary reads them:
        the cleared results given back (clearing saves the main request
        tokens; the summary should still know what they said)."""
        return clearing.uncleared(request.messages[:cut], state.messages,
                                  set(self._ext(state).cleared))

    @staticmethod
    def _through(state: TaskState, cut: int) -> int | None:
        """The newest log seq among the first ``cut`` messages, or ``None``
        when they hold nothing a previous compaction did not already cover."""
        seqs = [s for s in state.message_seqs[:cut] if s]
        return max(seqs) if seqs else None

    # ------------------------------------------------------------ compacting

    async def _instructions(self, view: TaskView, trigger: Literal["manual", "auto"],
                            instructions: str) -> str | None:
        """The summary's extra instructions once every extension has had its
        say (``Extension.before_compact``), or ``None`` when one refused this
        compaction (the person is told why)."""
        parts = [instructions.strip()] if instructions.strip() else []
        for ext in view.extensions:
            try:
                verdict = await ext.before_compact(view, trigger, instructions)
            except Exception:  # a failing extension does not hold compaction up
                log.exception("extension %s before_compact failed", ext.name)
                continue
            if isinstance(verdict, Deny):
                view.notify(f"Compaction skipped: {verdict.reason or ext.name}")
                return None
            if isinstance(verdict, str) and verdict.strip():
                parts.append(verdict.strip())
        return "\n\n".join(parts)

    async def _compact(self, view: TaskView, state: TaskState, request: ModelRequest,
                       cut: int, *, reason: str, estimated: int, extra: str = "",
                       command_at: int | None = None, rapid_refills: int = 0,
                       ready: Precomputed | None = None) -> TaskState:
        """Summarize the first ``cut`` messages (or take the summary written
        ahead, ``ready``), rebuild what follows the summary, record; returns
        the state as it is once the records are folded. ``command_at``: the
        index of the ``/compact`` message a manual compaction answers."""
        through = self._through(state, cut)
        assert through is not None
        to_summarize = self._to_summarize(state, request, cut)
        if IDLE_TIMER in state.timers:
            view.clear_timer(IDLE_TIMER)  # the compaction takes the idle arming back
        if ready is not None:
            note, usage, head_dropped = ready.note, ready.usage, ready.head_dropped
        else:
            assert view.complete is not None
            text = summary.instruction(
                view.prompt("compaction/summarize", summary.SUMMARIZE_PROMPT), extra)
            note, usage, head_dropped = await self._summarize(
                view.complete, view.task_id, request, to_summarize, text, self._spender(view))
        kept_replies = sum(1 for m in state.messages[cut:] if m.role == "assistant")
        kept = kept_replies > 0
        transcript = await self._transcript(view, state)
        text = summary.summary_message(
            note, tools=summary.tools_used(to_summarize), transcript=transcript,
            head_dropped=head_dropped, kept=kept)
        manual = reason == "manual"
        tail = kept and not manual
        lead = list(await view.residents(lead=True, keeps_tail=tail))
        after: list[Message] = []
        full: list[str] = []
        read_tool = (self.read_tool or view.find_tool("Read")) if self.restore else None
        if read_tool is not None and view.env is not None:
            ext = self._ext(state)
            since, carried = 0, ext.restored_paths or ()
            if ext.seen_through is not None and ext.restored_paths is not None:
                # What came after the last compaction: its kept messages and
                # what it re-attached are as old as it is.
                seen = ext.seen_through
                since = next((i for i, s in enumerate(state.message_seqs) if s > seen),
                             len(state.message_seqs))
            restored, records, full = await restore_files(
                recent_files(state.messages, cut, since=since, carried=carried,
                             cwd=pr.shell_cwd(state.ext) or view.cwd,
                             shown=pr.files_shown_by_commands(state.ext),
                             skip=_carried_files(state.ext, state.config,
                                                 rehung=pr.instructions_rehang(view))),
                read_tool, self._restore_context(view, state))
            for ext_name, kind, data in records:
                view.record(ext_name, kind, data)
            if restored:
                view.record(self.name, "restored", {"texts": [m.text for m in restored]})
        after.extend(await view.residents(keeps_tail=tail, kept=state.messages[cut:]))
        if manual and command_at is not None and state.message_seqs[command_at]:
            # Claude Code's order: nothing ahead of the summary; the kept
            # reply, then what is given again in the person's turn ahead of
            # the command's own message, and the system turns after them.
            again = [*lead, *after]
            through = state.message_seqs[command_at]
            lead_turn: list[Message] = []
            after_summary = [*state.messages[cut:command_at],
                             *(m for m in again if m.role != "system"),
                             state.messages[command_at]]
            after = [m for m in again if m.role == "system"]
        elif manual:
            lead_turn, after_summary, after = [], [], [*lead, *after]
        else:
            # A system turn cannot sit inside the summary's user turn: it
            # follows the summary.
            lead_turn = [m for m in lead if m.role != "system"]
            after_summary = [m for m in lead if m.role == "system"]
        view.compacted(Compacted(summary=text, through=through, lead=lead_turn,
                                 kept=after_summary, after=after, usage=usage))
        view.record(self.name, "compacted", {"reason": reason, "through": through,
                                             "estimated": estimated, "turn": state.prompt_seq,
                                             "step": state.steps_in_turn,
                                             "rapid_refills": rapid_refills,
                                             "precomputed": ready is not None,
                                             "kept_replies": kept_replies,
                                             "restored_paths": full,
                                             "seen_through": max(state.message_seqs,
                                                                 default=0)})
        log.info("task %s: compacted through seq %s (%s)", view.task_id, through, reason)
        return view.folded({self.name: self.reduce})

    async def _transcript(self, view: TaskView, state: TaskState) -> str | None:
        """Write the whole conversation so far (read from the log, never
        compacted) as JSON lines and return the file's path; ``None`` when
        there is nowhere the model could read it."""
        if not self.transcripts or view.env is None or not is_local(view.env) \
                or view.read_log is None:
            return None
        try:
            messages = await full_conversation(view.read_log, view.task_id)
        except Exception:  # the file is incidental: the compaction goes on without it
            log.warning("task %s: could not read the log for the transcript", view.task_id,
                        exc_info=True)
            return None
        cwd = str(state.config.get("cwd") or view.env.cwd)
        name = file_name_part(view.task_id, "task")
        path = project_dir(cwd, self.home or noeta_home()) / f"{name}.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"".join(msgspec.json.encode(m) + b"\n" for m in messages))
        except OSError:
            log.warning("task %s: could not write the transcript %s", view.task_id, path)
            return None
        return str(path)

    def _restore_context(self, view: TaskView, state: TaskState
                         ) -> Callable[[int, Callable[[str, str, Any], None]], ToolContext]:
        def make(i: int, record: Callable[[str, str, Any], None]) -> ToolContext:
            assert view.env is not None
            return ToolContext(task_id=view.task_id, call_id=f"compaction-restore-{i}",
                               env=view.env, state=dict(state.ext), record=record)
        return make

    @staticmethod
    def _spender(view: TaskView) -> Callable[[str, Usage], None]:
        """Every summarize call's cost counts in the task's usage (and its
        USD budget), the failed ones' too: the log says what was spent."""
        def spend(model: str, usage: Usage) -> None:
            spent = core_records.Spent(model=model, usage=usage, purpose="compaction")
            view.record(core_records.CORE, spent.kind, spent.data())
        return spend

    async def _summarize(self, complete: Callable[[ModelRequest], Awaitable[ModelResponse]],
                         task_id: str, request: ModelRequest, to_summarize: list[Message],
                         text: str, spend: Callable[[str, Usage], None]
                         ) -> tuple[str, Usage, bool]:
        """The summary text, the calls' usage, and whether the oldest rounds
        had to be dropped for the call to fit; ``spend`` hears each call's
        cost."""
        model = self.compaction_model or request.model
        max_tokens = model_limits(model)[1] if self.compaction_model else None
        usage = Usage()

        async def call(req: ModelRequest) -> ModelResponse | ModelError:
            nonlocal usage
            try:
                outcome = await complete(req)
            except ModelError as exc:
                return exc
            usage = usage + outcome.usage
            spend(outcome.model or model, outcome.usage)
            return outcome

        async def once(history: list[Message]) -> ModelResponse | ModelError:
            """The summarize call; when it errs or brings no summary, Claude
            Code's fallback call."""
            req = summary.summary_request(request, history, text, model=model,
                                          max_tokens=max_tokens)
            outcome = await call(req)
            note = summary.joined_text(outcome) if isinstance(outcome, ModelResponse) else ""
            failure = summary.summary_failure(outcome, note)
            if failure is None:
                return outcome
            log.info("task %s: summarize call failed (%s); trying the fallback call",
                     task_id, failure)
            return await call(summary.fallback_request(req))

        attempt = to_summarize
        dropped = False
        outcome = await once(attempt)
        for _ in range(summary.MAX_SUMMARY_OVERFLOW_RETRIES):
            if not isinstance(outcome, ContextOverflow):
                break
            shorter = summary.drop_oldest_rounds(attempt, summary.overflow_gap(outcome))
            if shorter is None:
                break
            attempt, dropped = shorter, True
            outcome = await once(attempt)
        note = summary.joined_text(outcome) if isinstance(outcome, ModelResponse) else ""
        failure = summary.summary_failure(outcome, note)
        if failure is not None:
            raise TurnFailed(summary.SUMMARY_FAILED_REASON, failure)
        return note, usage, dropped


__all__ = ["COMMAND", "Compaction", "CompactionState", "Precomputed", "cache_life",
           "command_text"]


def _carried_files(ext: Mapping[str, Any], config: Mapping[str, Any], *,
                   rehung: bool = True) -> set[str]:
    """Files the conversation carries after a compaction anyway, so they are
    not restored as files (Claude Code's exact-path filter): the instruction
    files the instructions resident gives again (``rehung``), the memory
    index when it recorded showing it, the task's plan file."""
    out: set[str] = set()
    if rehung:
        for entry in pr.shown_instruction_files(ext):
            out.update(p for p in (entry.get("path"), entry.get("real")) if isinstance(p, str))
        directory = pr.memory_dir(ext) or config.get(MEMORY_DIR_KEY)
        index = posixpath.join(directory, MEMORY_INDEX_FILE) \
            if isinstance(directory, str) and directory else None
        if index is not None and index in pr.instruction_digests(ext):
            out.add(index)
    path = pr.plan_file(ext)
    if path is not None:
        out.add(path)
    return {posixpath.normpath(p) for p in out}
