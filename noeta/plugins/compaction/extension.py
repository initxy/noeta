"""The ``Compaction`` extension: clearing old tool results, summarizing old
history, restoring recent files, and ``RecallHistory``.

Everything a step sends is a pure function of the folded log, so a runner
that restarts mid-step sends the same bytes: the baseline is the newest
reply's recorded usage (``TaskState.last_usage``, reset by a compaction), the
results the valves cleared are recorded (``cleared``), the thrashing latch
folds from ``compacted`` entries, and a compaction is the core ``Compacted``
event plus the restored files it carries.
"""

from __future__ import annotations

import dataclasses
import logging
from collections import OrderedDict
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import msgspec

from noeta.core.errors import ContextOverflow, ModelError, TurnFailed
from noeta.core.events import Compacted
from noeta.core.extension import Extension, TaskView
from noeta.core.helpers import simulated_state
from noeta.core.loop.request import cache_breakpoints
from noeta.core.loop.state import TaskState
from noeta.core.models import model_limits
from noeta.core.tool import Tool, ToolContext
from noeta.core.types import Message, ModelRequest, ModelResponse, ToolResult, Usage, user
from noeta.plugins.compaction import clearing, summary
from noeta.plugins.compaction.estimate import messages_tokens, prefix_tokens
from noeta.plugins.compaction.recall import (
    READ_SUGGESTION,
    RecallHistoryTool,
    collapsed_context,
    summary_boundary,
)
from noeta.plugins.compaction.restore import recent_files, restore_files
from noeta.plugins.compaction.window import Window, check_positive, derive_window

log = logging.getLogger("noeta.compaction")

#: Compactions at most this many replies apart count as a close refill;
#: this many close refills in a row latch the read-suggestion reminder.
THRASH_CLOSE_TURNS = 3
THRASH_RUN_LIMIT = 3


@dataclass(frozen=True)
class CompactionState:
    #: Call id -> how many replies the task had when the result was cleared.
    cleared: Mapping[str, int] = field(default_factory=dict)
    #: One past the newest history message with a cleared result.
    cleared_boundary: int = 0
    compactions: int = 0
    last_marker: int | None = None
    close_run: int = 0
    thrashing: bool = False


def _replies(state: TaskState) -> int:
    return sum(1 for m in state.history if m.role == "assistant")


def _is_tool_results(message: Message) -> bool:
    return any(isinstance(b, ToolResult) for b in message.content)


class Compaction(Extension):
    """Keeps a long task inside its model's context window.

    Constructor arguments (they replace 0.6's ``HostConfig`` /
    ``Options`` fields of the same names; a resumed task must be given the
    same values, since they shape the request bytes):

    - ``auto_compact_window``: cap, in tokens, on the window every number
      derives from (Claude Code's ``autoCompactWindow``); ``None`` uses the
      model's window.
    - ``clear_tool_outputs_at``: request size, in tokens, at which old tool
      results start being cleared; ``None`` is half the usable window.
    - ``compaction_model``: a cheaper model for the summarize call only (sent
      with that model's own output ceiling).
    - ``clearable_tools``: names of the tools whose results may be cleared
      (re-fetchable output). Defaults to Claude Code's set: Read, Glob, Grep,
      Bash, BashOutput, Edit, Write, WebFetch, WebSearch. A host tool opts in
      by adding its name.
    - ``keep_recent`` / ``batch_tokens``: the second valve's newest results
      kept and its batch size (5 / 20,000); ``keep_recent=None`` turns that
      valve off.
    - ``read_tool``: the tool the post-compaction file restore reads through
      (default: the task's own ``Read`` tool; no restore without one);
      ``restore_files=False`` turns the restore off.
    - ``recall_history``: offer the ``RecallHistory`` tool (default on).
    """

    name = "compaction"

    def __init__(
        self,
        *,
        auto_compact_window: int | None = None,
        clear_tool_outputs_at: int | None = None,
        compaction_model: str | None = None,
        clearable_tools: Collection[str] = clearing.CLAUDE_CODE_CLEARABLE,
        keep_recent: int | None = clearing.KEEP_RECENT,
        batch_tokens: int = clearing.BATCH_TOKENS,
        restore_files: bool = True,
        read_tool: Tool | None = None,
        recall_history: bool = True,
        name: str | None = None,
    ) -> None:
        check_positive("auto_compact_window", auto_compact_window)
        check_positive("clear_tool_outputs_at", clear_tool_outputs_at)
        if name:
            self.name = name
        self.auto_compact_window = auto_compact_window
        self.clear_tool_outputs_at = clear_tool_outputs_at
        self.compaction_model = compaction_model
        self.clearable_tools = frozenset(clearable_tools)
        self.keep_recent = keep_recent
        self.batch_tokens = batch_tokens
        self.recall_history = recall_history
        self.restore = restore_files
        self.read_tool = read_tool
        #: The newest request per task, for the overflow path (which is not
        #: handed one). Only read within the same model step.
        self._last: OrderedDict[str, ModelRequest] = OrderedDict()

    # ------------------------------------------------------------ state

    def initial_state(self) -> CompactionState:
        return CompactionState()

    def reduce(self, state: Any, kind: str, data: Any) -> CompactionState:
        st = state if isinstance(state, CompactionState) else CompactionState()
        if not isinstance(data, Mapping):
            return st
        if kind == "cleared":
            at = int(data.get("at") or 0)
            cleared = dict(st.cleared)
            for call_id in data.get("ids") or []:
                cleared.setdefault(str(call_id), at)
            return dataclasses.replace(
                st, cleared=cleared,
                cleared_boundary=max(st.cleared_boundary, int(data.get("boundary") or 0)))
        if kind == "compacted":
            marker = int(data.get("marker") or 0)
            prev = st.last_marker
            run = st.close_run + 1 if prev is not None and marker - prev <= THRASH_CLOSE_TURNS \
                else 0
            return dataclasses.replace(
                st, compactions=st.compactions + 1, last_marker=marker, close_run=run,
                thrashing=run >= THRASH_RUN_LIMIT)
        return st

    def _ext(self, state: TaskState) -> CompactionState:
        ext = state.ext.get(self.name)
        return ext if isinstance(ext, CompactionState) else CompactionState()

    def tools(self, view: TaskView) -> Sequence[Tool]:
        if not self.recall_history:
            return ()
        return (RecallHistoryTool(view.state, self.name),)

    # ------------------------------------------------------------ hooks

    def _window(self, model: str) -> Window:
        try:
            return derive_window(model, auto_compact_window=self.auto_compact_window,
                                 clear_tool_outputs_at=self.clear_tool_outputs_at)
        except ValueError as exc:
            raise TurnFailed("compaction_config_invalid", str(exc)) from None

    def _remember(self, task_id: str, request: ModelRequest) -> None:
        self._last[task_id] = request
        self._last.move_to_end(task_id)
        while len(self._last) > 256:
            self._last.popitem(last=False)

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        window = self._window(request.model)
        self._remember(view.task_id, request)
        state = view.state
        n = len(state.messages)
        extras = list(request.messages[n:])
        composed, ext = self._clear(view, state, request, list(request.messages[:n]), window)
        cache_after = list(request.cache_after)
        for _ in range(3):
            trigger, dens = self._trigger(state, ext, request, composed)
            if trigger < window.usable or view.complete is None:
                break
            state = await self._compact(view, state, request, composed, window, dens,
                                        reason="proactive", estimated=trigger)
            composed, ext = self._clear(view, state, request, list(state.messages), window)
            cache_after = cache_breakpoints(state)
        reminders = self._reminders(state, ext)
        return msgspec.structs.replace(request, messages=[*composed, *reminders, *extras],
                                       cache_after=cache_after)

    async def on_context_overflow(self, view: TaskView) -> bool:
        request = self._last.pop(view.task_id, None)
        if request is None or view.complete is None:
            return False
        window = self._window(request.model)
        state = view.state
        composed, ext = self._clear(view, state, request, list(state.messages), window)
        _trigger, dens = self._trigger(state, ext, request, composed)
        await self._compact(view, state, request, composed, window, dens, reason="overflow",
                            estimated=_trigger)
        return True

    # ------------------------------------------------------------ clearing

    def _clear(self, view: TaskView, state: TaskState, request: ModelRequest,
               recorded: list[Message], window: Window) -> tuple[list[Message], CompactionState]:
        """The recorded messages with the valves applied; records results
        cleared for the first time, and returns the extension state as it is
        once that record is folded."""
        ext = self._ext(state)
        raw_estimate = prefix_tokens(request.system, request.tools) + messages_tokens(recorded)
        baseline = state.last_usage.input if state.last_usage is not None else 0
        valves = clearing.Valves(usable=window.usable, tail_budget=window.tail_budget,
                                 micro_fraction=window.micro_fraction,
                                 keep_recent=self.keep_recent, batch_tokens=self.batch_tokens)
        selected = clearing.select(
            recorded, is_clearable=self.clearable_tools.__contains__,
            size=max(raw_estimate, baseline), valves=valves, real_baseline=baseline,
            estimated=raw_estimate)
        new = [i for i in selected if i not in ext.cleared]
        if new:
            ids = {*ext.cleared, *new}
            data = {"ids": new, "at": _replies(state),
                    "boundary": clearing.raw_boundary(state.history, ids)}
            view.record(self.name, "cleared", data)
            ext = self.reduce(ext, "cleared", data)
        return clearing.apply_cleared(recorded, ext.cleared), ext

    def _trigger(self, state: TaskState, ext: CompactionState, request: ModelRequest,
                 composed: list[Message]) -> tuple[int, float]:
        """``max(estimate, last real input count + what was added since)``
        (ADR-003), and the observed density of real tokens per estimated
        token. The "since" is measured against the previous request as it was
        sent: the messages before the newest reply, with the results cleared
        by then."""
        prefix = prefix_tokens(request.system, request.tools)
        now = prefix + messages_tokens(composed)
        usage = state.last_usage
        if usage is None or usage.input <= 0:
            return now, 1.0
        last = max((i for i, m in enumerate(state.messages) if m.role == "assistant"),
                   default=-1)
        if last < 0:
            return max(now, usage.input), 1.0
        replies = _replies(state)
        before = {i for i, at in ext.cleared.items() if at < replies}
        then = prefix + messages_tokens(
            clearing.apply_cleared(state.messages[:last], before))
        delta = max(0, now - then)
        return max(now, usage.input + delta), clearing.density(usage.input, then)

    def _reminders(self, state: TaskState, ext: CompactionState) -> list[Message]:
        """Unrecorded per-request reminders, appended after the recorded
        messages. Rule shared with ``Todo``: an extension that records a
        message in ``before_model`` inserts it before these (right after the
        recorded messages, where the log puts it); these stay last."""
        texts: list[str] = []
        if ext.thrashing:
            texts.append(READ_SUGGESTION)
        pointer = collapsed_context(summary_boundary(state), ext.cleared_boundary)
        if pointer is not None and self.recall_history:
            texts.append(pointer)
        return [user(t, origin="system") for t in texts]

    # ------------------------------------------------------------ compaction

    def _boundary(self, state: TaskState, budget: int) -> int:
        """How many leading messages the summary covers: everything older
        than the protected tail, snapped forward so a tool result travels
        with its call and the cut falls between log positions."""
        if budget <= 0:
            return 0
        messages, seqs = state.messages, state.message_seqs
        acc = 0
        cutoff = len(messages)
        for i in range(len(messages) - 1, -1, -1):
            acc += messages_tokens([messages[i]])
            if acc > budget:
                cutoff = i + 1
                break
            cutoff = i
        while cutoff < len(messages) and (
                _is_tool_results(messages[cutoff])
                or (cutoff > 0 and seqs[cutoff] == seqs[cutoff - 1])):
            cutoff += 1
        return cutoff

    async def _compact(self, view: TaskView, state: TaskState, request: ModelRequest,
                       composed: list[Message], window: Window, dens: float, *,
                       reason: str, estimated: int) -> TaskState:
        """Summarize, restore files, record; returns the state as it is once
        the records are folded."""
        prefix = 0
        if state.compaction is not None:
            prefix = next((i for i, s in enumerate(state.message_seqs) if s != 0),
                          len(state.message_seqs))
        cut = self._boundary(state, int(window.tail_budget / dens))
        if cut <= prefix:
            raise TurnFailed(summary.NO_PROGRESS_REASON)
        through = state.message_seqs[cut - 1]
        to_summarize = list(composed[:cut])
        if state.compaction is not None:
            to_summarize[0] = user(state.compaction.summary)
        note, usage = await self._summarize(view, request, to_summarize,
                                            keep_first=state.compaction is not None)
        note = summary.enforce_verbatim_constraints(
            note, summary.extract_safety_constraints(to_summarize))
        # Residents (listings, instructions, the environment block) first,
        # then the restored files: both are in ``kept``, so the request
        # sent now is the one rebuilt from the log.
        kept: list[Message] = list(await view.residents())
        read_tool = (self.read_tool or view.find_tool("Read")) if self.restore else None
        if read_tool is not None and view.env is not None:
            raw_cut = sum(1 for s in state.history_seqs if s <= through)
            restored, records = await restore_files(
                recent_files(state.history, raw_cut), read_tool,
                self._restore_context(view, state), tail_budget=window.tail_budget)
            kept.extend(restored)
            for ext_name, kind, data in records:
                view.record(ext_name, kind, data)
        view.compacted(Compacted(summary=summary.framed(note), through=through, kept=kept,
                                 usage=usage))
        view.record(self.name, "compacted", {"reason": reason, "marker": _replies(state),
                                             "through": through, "estimated": estimated})
        log.info("task %s: compacted through seq %s (%s)", view.task_id, through, reason)
        return self._simulate(view)

    def _restore_context(self, view: TaskView, state: TaskState
                         ) -> Callable[[int, Callable[[str, str, Any], None]], ToolContext]:
        def make(i: int, record: Callable[[str, str, Any], None]) -> ToolContext:
            assert view.env is not None
            return ToolContext(task_id=view.task_id, call_id=f"compaction-restore-{i}",
                               env=view.env, state=dict(state.ext), record=record)
        return make

    def _simulate(self, view: TaskView) -> TaskState:
        """The task state with every record pending on ``view`` folded in, on
        a copy: what the next fold will see, so the request sent now is the
        one a restarted runner rebuilds from the log."""
        return simulated_state(view, {self.name: self.reduce})

    async def _summarize(self, view: TaskView, request: ModelRequest,
                         to_summarize: list[Message], *, keep_first: bool
                         ) -> tuple[str, Usage]:
        model = self.compaction_model or request.model
        if self.compaction_model:
            max_tokens: int | None = model_limits(self.compaction_model)[1]
        else:
            max_tokens = request.max_tokens or model_limits(request.model)[1]
        usage = Usage()

        async def once(history: list[Message]) -> tuple[ModelResponse | ModelError, str]:
            nonlocal usage
            outcome: ModelResponse | ModelError
            text = ""
            for forbid in (False, True):
                assert view.complete is not None
                req = summary.summary_request(
                    request, history, model=model, max_tokens=max_tokens, forbid_tools=forbid,
                    instruction=view.prompt("compaction/summarize", summary.SUMMARIZE_PROMPT))
                try:
                    outcome = await view.complete(req)
                except ModelError as exc:
                    return exc, ""
                usage = usage + outcome.usage
                text = summary.joined_text(outcome)
                if not summary.needs_tool_free_retry(outcome, text):
                    break
            return outcome, text

        attempt = to_summarize
        outcome, text = await once(attempt)
        for _ in range(summary.MAX_SUMMARY_OVERFLOW_RETRIES):
            if not isinstance(outcome, ContextOverflow):
                break
            shorter = summary.drop_oldest_rounds(attempt, keep_first=keep_first)
            if shorter is None:
                break
            attempt = shorter
            outcome, text = await once(attempt)
        failure = summary.summary_failure(outcome, text)
        if failure is not None:
            raise TurnFailed(summary.SUMMARY_FAILED_REASON, failure)
        return text, usage


__all__ = ["Compaction", "CompactionState"]
