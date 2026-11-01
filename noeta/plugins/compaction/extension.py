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
environment are given again with the next request.

Everything a step sends is a pure function of the folded log, so a runner
that restarts mid-step sends the same bytes: a compaction is the core
``Compacted`` event, carrying everything re-attached after it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import msgspec

from noeta.core.errors import ContextOverflow, ModelError, RequestStale, TurnDone, TurnFailed
from noeta.core.events import Compacted
from noeta.core.extension import Command, CommandContext, Deny, Extension, Reminder, TaskView
from noeta.core.loop.state import TaskState
from noeta.core.models import model_limits
from noeta.core.tool import Tool, ToolContext
from noeta.core.types import Message, ModelRequest, ModelResponse, ToolResult, Usage
from noeta.plugins.compaction import summary
from noeta.plugins.compaction.estimate import messages_tokens, prefix_tokens
from noeta.plugins.compaction.restore import recent_files, restore_files
from noeta.plugins.compaction.window import (
    Window,
    check_percent,
    check_positive,
    derive_window,
    percent_from_env,
    window_from_env,
)
from noeta.toolkit.local_env import is_local
from noeta.toolkit.paths import file_name_part, noeta_home, project_dir

log = logging.getLogger("noeta.compaction")

#: The restored files' place in the next request's system turn
#: (``Reminder.order``): first, ahead of the invoked skills and the lists.
RESTORED_ORDER = 10

#: Claude Code's rapid-refill breaker: an automatic compaction needed again
#: within this many model calls of the previous one, in the same turn, is
#: a refill; this many refills in a row stop the turn instead.
RAPID_REFILL_CALLS = 3
RAPID_REFILLS_TO_STOP = 3
THRASHING_REASON = "autocompact_thrashing"
THRASHING_DETAIL = (
    f"Automatic compaction stopped: the context filled up again within "
    f"{RAPID_REFILL_CALLS} model calls of the previous compaction, "
    f"{RAPID_REFILLS_TO_STOP} times in a row. A file or tool output is probably too large "
    "for the context window; read it in smaller parts, or start a new conversation.")

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
    #: Where the last compaction ran (person turns so far, model calls in
    #: that turn) and the automatic compactions in a row that each came
    #: within ``RAPID_REFILL_CALLS`` calls of the one before.
    turn: int = -1
    step: int = 0
    rapid_refills: int = 0


def _user_turns(state: TaskState) -> int:
    return sum(1 for m in state.history if m.from_person
               and not any(isinstance(b, ToolResult) for b in m.content))


def _last_reply(messages: Sequence[Message]) -> int:
    return max((i for i, m in enumerate(messages) if m.role == "assistant"), default=-1)


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
    ) -> None:
        check_positive("auto_compact_window", auto_compact_window)
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

    # ------------------------------------------------------------ state

    def initial_state(self) -> CompactionState:
        return CompactionState()

    def reduce(self, state: Any, kind: str, data: Any) -> CompactionState:
        st = state if isinstance(state, CompactionState) else CompactionState()
        if not isinstance(data, Mapping):
            return st
        if kind == "restored":
            return replace(st, restored=tuple(data.get("texts") or ()))
        if kind == "restored_sent":
            return replace(st, restored=())
        if kind != "compacted":
            return st
        seen, paths = data.get("seen_through"), data.get("restored_paths")
        return replace(st, compactions=st.compactions + 1,
                       turn=int(data.get("turn") or 0), step=int(data.get("step") or 0),
                       rapid_refills=int(data.get("rapid_refills") or 0),
                       kept_replies=int(data.get("kept_replies") or 0),
                       seen_through=seen if isinstance(seen, int) else None,
                       restored_paths=tuple(paths) if isinstance(paths, list) else None)

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
        state = view.state
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
        if not self.auto or view.complete is None:
            return request
        if state.compaction is not None and state.last_usage is None:
            return request  # the first request after a compaction goes as it is
        window = self._window(request.model)
        tokens, _ = self._tokens(state, request, n)
        if tokens < window.threshold:
            return request
        cut = self._cut(state, n, manual=False)
        if cut is None or self._through(state, cut) is None:
            return request
        rapid = self._rapid_refills(state)
        if rapid >= RAPID_REFILLS_TO_STOP:
            log.warning("task %s: auto-compaction refilled %d times in a row; stopping",
                        view.task_id, rapid)
            raise TurnFailed(THRASHING_REASON, THRASHING_DETAIL)
        extra = await self._instructions(view, "auto", "")
        if extra is None:
            return request
        await self._compact(view, state, request, cut, reason="auto", estimated=tokens,
                            extra=extra, rapid_refills=rapid)
        # Every extension builds the request again from the compacted
        # conversation (betas and notes that depend on it included).
        raise RequestStale()

    async def on_context_overflow(self, view: TaskView, request: ModelRequest) -> bool:
        if view.complete is None:
            return False
        state = view.state
        n = len(state.messages)
        if self._through(state, n) is None:
            return False
        extra = await self._instructions(view, "auto", "")
        if extra is None:
            return False
        await self._compact(view, state, request, n, reason="overflow",
                            estimated=self._tokens(state, request, n)[0], extra=extra)
        return True

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

    @staticmethod
    def _tokens(state: TaskState, request: ModelRequest, n: int) -> tuple[int, int]:
        """``(context, before)``: the context of the first ``n`` messages, and
        of those up to and including the newest reply. As Claude Code's: the
        newest reply's reported usage plus the estimate of what came after
        it; the estimate of the whole only while no reply reported usage."""
        prefix = prefix_tokens(request.system, request.tools)
        messages = request.messages[:n]
        last = _last_reply(messages)
        usage = state.last_usage
        if usage is None or last < 0:
            return (prefix + messages_tokens(messages),
                    prefix + messages_tokens(messages[:last + 1]))
        before = usage.input + usage.output
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

    def _rapid_refills(self, state: TaskState) -> int:
        """The refill count an automatic compaction now would make: one more
        than the last one's when that ran in this turn fewer than
        ``RAPID_REFILL_CALLS`` model calls ago, else 0."""
        ext = self._ext(state)
        if not ext.compactions or ext.turn != _user_turns(state):
            return 0
        calls = state.steps_in_turn - ext.step
        return ext.rapid_refills + 1 if 0 <= calls < RAPID_REFILL_CALLS else 0

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
                       command_at: int | None = None, rapid_refills: int = 0) -> TaskState:
        """Summarize the first ``cut`` messages, rebuild what follows the
        summary, record; returns the state as it is once the records are
        folded. ``command_at``: the index of the ``/compact`` message a
        manual compaction answers."""
        through = self._through(state, cut)
        assert through is not None
        to_summarize = list(request.messages[:cut])
        note, usage, head_dropped = await self._summarize(view, request, to_summarize, extra)
        kept_replies = sum(1 for m in state.messages[cut:] if m.role == "assistant")
        kept = kept_replies > 0
        text = summary.summary_message(
            note, tools=summary.tools_used(to_summarize), transcript=self._transcript(view, state),
            head_dropped=head_dropped, kept=kept)
        manual = reason == "manual"
        tail = kept and not manual
        lead = list(await view.residents(lead=True, keeps_tail=tail))
        after: list[Message] = []
        full: list[str] = []
        read_tool = (self.read_tool or view.find_tool("Read")) if self.restore else None
        if read_tool is not None and view.env is not None:
            raw_cut = sum(1 for s in state.history_seqs if s <= through)
            ext = self._ext(state)
            since, carried = 0, ext.restored_paths or ()
            if ext.seen_through is not None and ext.restored_paths is not None:
                since = sum(1 for s in state.history_seqs if s <= ext.seen_through)
            restored, records, full = await restore_files(
                recent_files(state.history, raw_cut, since=since, carried=carried),
                read_tool, self._restore_context(view, state))
            for ext_name, kind, data in records:
                view.record(ext_name, kind, data)
            if restored:
                view.record(self.name, "restored", {"texts": [m.text for m in restored]})
        after.extend(await view.residents(keeps_tail=tail))
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
                                             "estimated": estimated, "turn": _user_turns(state),
                                             "step": state.steps_in_turn,
                                             "rapid_refills": rapid_refills,
                                             "kept_replies": kept_replies,
                                             "restored_paths": full,
                                             "seen_through": max(state.history_seqs,
                                                                 default=0)})
        log.info("task %s: compacted through seq %s (%s)", view.task_id, through, reason)
        return view.folded({self.name: self.reduce})

    def _transcript(self, view: TaskView, state: TaskState) -> str | None:
        """Write the whole conversation so far as JSON lines and return the
        file's path; ``None`` when there is nowhere the model could read it."""
        if not self.transcripts or view.env is None or not is_local(view.env):
            return None
        cwd = str(state.config.get("cwd") or view.env.cwd)
        name = file_name_part(view.task_id, "task")
        path = project_dir(cwd, self.home or noeta_home()) / f"{name}.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"".join(msgspec.json.encode(m) + b"\n" for m in state.history))
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

    async def _summarize(self, view: TaskView, request: ModelRequest,
                         to_summarize: list[Message], extra: str
                         ) -> tuple[str, Usage, bool]:
        """The summary text, the calls' usage, and whether the oldest rounds
        had to be dropped for the call to fit."""
        model = self.compaction_model or request.model
        max_tokens = model_limits(model)[1] if self.compaction_model else None
        text = summary.instruction(view.prompt("compaction/summarize", summary.SUMMARIZE_PROMPT),
                                   extra)
        usage = Usage()

        async def once(history: list[Message], ceiling: int | None
                       ) -> ModelResponse | ModelError:
            nonlocal usage
            assert view.complete is not None
            req = summary.summary_request(request, history, text, model=model,
                                          max_tokens=ceiling or max_tokens)
            try:
                outcome = await view.complete(req)
            except ModelError as exc:
                return exc
            usage = usage + outcome.usage
            return outcome

        attempt = to_summarize
        dropped = False
        outcome = await once(attempt, None)
        for _ in range(summary.MAX_SUMMARY_OVERFLOW_RETRIES):
            if not isinstance(outcome, ContextOverflow):
                break
            shorter = summary.drop_oldest_rounds(attempt, summary.overflow_gap(outcome))
            if shorter is None:
                break
            attempt, dropped = shorter, True
            outcome = await once(attempt, None)
        if isinstance(outcome, ModelError) and not isinstance(outcome, ContextOverflow):
            ceiling = min(summary.RETRY_MAX_TOKENS,
                          max_tokens or request.max_tokens or summary.RETRY_MAX_TOKENS)
            outcome = await once(attempt, ceiling)
        note = summary.joined_text(outcome) if isinstance(outcome, ModelResponse) else ""
        failure = summary.summary_failure(outcome, note)
        if failure is not None:
            raise TurnFailed(summary.SUMMARY_FAILED_REASON, failure)
        return note, usage, dropped


__all__ = ["COMMAND", "Compaction", "CompactionState", "command_text"]
