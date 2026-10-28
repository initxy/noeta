"""The ``Compaction`` extension: Claude Code's auto-compaction and
``/compact``.

Before every model request (mid-turn included) the context is measured as
the newest reply's reported usage plus an estimate of what was appended
since. At the threshold (``window.py``) the conversation is summarized by
the main request itself with the summarize instruction appended. When the
context was under the threshold before the newest tool results, that reply
and its results are kept verbatim after the summary; otherwise everything
is summarized. After the summary come the project instructions (ahead of
it), then the kept messages, then the files read recently, the invoked
skills, the listings and the environment.

Everything a step sends is a pure function of the folded log, so a runner
that restarts mid-step sends the same bytes: a compaction is the core
``Compacted`` event, carrying everything re-attached after it.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgspec

from noeta.core.errors import ContextOverflow, ModelError, TurnDone, TurnFailed
from noeta.core.events import Compacted
from noeta.core.execenv import is_local
from noeta.core.extension import Command, CommandContext, Extension, TaskView
from noeta.core.loop.request import cache_breakpoints
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
from noeta.toolkit.context import noeta_home, project_dir, simulated_state

log = logging.getLogger("noeta.compaction")

#: A compaction within this many user turns of the previous one is a rapid
#: refill; more than this many rapid refills in a row stop auto-compaction.
RAPID_REFILL_TURNS = 3
RAPID_REFILL_LIMIT = 3
THRASHING_REASON = "compaction_thrashing"
THRASHING_DETAIL = (
    "the context filled up again within {turns} turns of the previous compaction, "
    "{limit} times in a row; a file or tool output is probably too large for the context "
    "window. Read it in smaller pieces, or start a new task."
)

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
    #: User turns when the last compaction happened.
    last_turn: int | None = None
    #: Rapid refills in a row, the last compaction included.
    rapid: int = 0


def _user_turns(state: TaskState) -> int:
    return sum(1 for m in state.history if m.role == "user" and m.origin in (None, "human")
               and not any(isinstance(b, ToolResult) for b in m.content))


def _last_reply(messages: Sequence[Message]) -> int:
    return max((i for i, m in enumerate(messages) if m.role == "assistant"), default=-1)


class Compaction(Extension):
    """Keeps a long task inside its model's context window.

    Constructor arguments (a resumed task must be given the same values,
    since they shape the request bytes):

    - ``auto_compact_window``: cap, in tokens, on the window the threshold
      derives from; default ``$CLAUDE_CODE_AUTO_COMPACT_WINDOW``, else the
      model's window.
    - ``auto_compact_percent``: fire at this percentage of the effective
      window when that is lower than the default threshold; default
      ``$CLAUDE_AUTOCOMPACT_PCT_OVERRIDE``.
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
        #: The newest request per task, for the overflow path (which is not
        #: handed one). Only read within the same model step.
        self._last: OrderedDict[str, ModelRequest] = OrderedDict()

    # ------------------------------------------------------------ state

    def initial_state(self) -> CompactionState:
        return CompactionState()

    def reduce(self, state: Any, kind: str, data: Any) -> CompactionState:
        st = state if isinstance(state, CompactionState) else CompactionState()
        if kind != "compacted" or not isinstance(data, Mapping):
            return st
        turn = int(data.get("turn") or 0)
        rapid = st.rapid + 1 if st.last_turn is not None \
            and turn - st.last_turn < RAPID_REFILL_TURNS else 0
        return CompactionState(compactions=st.compactions + 1, last_turn=turn, rapid=rapid)

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

    def _remember(self, task_id: str, request: ModelRequest) -> None:
        self._last[task_id] = request
        self._last.move_to_end(task_id)
        while len(self._last) > 256:
            self._last.popitem(last=False)

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        self._remember(view.task_id, request)
        state = view.state
        n = len(state.messages)
        extras = list(request.messages[n:])
        command = self._command(state)
        if command is not None:
            at, args = command
            if view.complete is not None:
                window = self._window(request.model)
                cut = self._cut(state, request, at, window)
                if self._through(state, cut) is not None:
                    await self._compact(view, state, request, cut, reason="manual",
                                        estimated=self._tokens(state, request, at)[0],
                                        extra=args)
            raise TurnDone()
        if not self.auto or view.complete is None:
            return request
        if state.compaction is not None and state.last_usage is None:
            return request  # the first request after a compaction goes as it is
        window = self._window(request.model)
        tokens, _ = self._tokens(state, request, n)
        if tokens < window.threshold:
            return request
        cut = self._cut(state, request, n, window)
        if self._through(state, cut) is None:
            return request
        ext = self._ext(state)
        if ext.last_turn is not None and _user_turns(state) - ext.last_turn \
                < RAPID_REFILL_TURNS and ext.rapid >= RAPID_REFILL_LIMIT:
            raise TurnFailed(THRASHING_REASON, THRASHING_DETAIL.format(
                turns=RAPID_REFILL_TURNS, limit=RAPID_REFILL_LIMIT))
        new = await self._compact(view, state, request, cut, reason="auto", estimated=tokens)
        return msgspec.structs.replace(request, messages=[*new.messages, *extras],
                                       cache_after=cache_breakpoints(new))

    async def on_context_overflow(self, view: TaskView) -> bool:
        request = self._last.pop(view.task_id, None)
        if request is None or view.complete is None:
            return False
        state = view.state
        n = len(state.messages)
        if self._through(state, n) is None:
            return False
        await self._compact(view, state, request, n, reason="overflow",
                            estimated=self._tokens(state, request, n)[0])
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
        if m.role != "user" or m.origin not in (None, "human"):
            return None
        match = _COMMAND_RE.search(m.text)
        return (at, match.group(1).strip()) if match else None

    @staticmethod
    def _tokens(state: TaskState, request: ModelRequest, n: int) -> tuple[int, int]:
        """``(context, before)``: the context of the first ``n`` messages, and
        of those up to and including the newest reply. Each is the reply's
        reported usage plus the estimate of what came after it, or the
        estimate of the whole when that is larger."""
        prefix = prefix_tokens(request.system, request.tools)
        messages = request.messages[:n]
        last = _last_reply(messages)
        before = prefix + messages_tokens(messages[:last + 1])
        usage = state.last_usage
        if usage is not None and last >= 0:
            before = max(before, usage.input + usage.output)
        since = messages_tokens(messages[last + 1:])
        return max(prefix + messages_tokens(messages), before + since), before

    def _cut(self, state: TaskState, request: ModelRequest, n: int, window: Window) -> int:
        """How many leading messages the summary covers: all ``n``, or, when
        the context was under the threshold before the newest reply's tool
        results, everything before that reply."""
        last = _last_reply(state.messages[:n])
        if last <= 0 or not state.messages[last].tool_uses:
            return n
        if any(m.role == "assistant" for m in state.messages[last + 1:n]):
            return n
        _, before = self._tokens(state, request, n)
        return last if before < window.threshold else n

    @staticmethod
    def _through(state: TaskState, cut: int) -> int | None:
        """The newest log seq among the first ``cut`` messages, or ``None``
        when they hold nothing a previous compaction did not already cover."""
        seqs = [s for s in state.message_seqs[:cut] if s]
        return max(seqs) if seqs else None

    # ------------------------------------------------------------ compacting

    async def _compact(self, view: TaskView, state: TaskState, request: ModelRequest,
                       cut: int, *, reason: str, estimated: int, extra: str = "") -> TaskState:
        """Summarize the first ``cut`` messages, rebuild what follows the
        summary, record; returns the state as it is once the records are
        folded."""
        through = self._through(state, cut)
        assert through is not None
        to_summarize = list(request.messages[:cut])
        note, usage, head_dropped = await self._summarize(view, request, to_summarize, extra)
        kept = any(m.role == "assistant" for m in state.messages[cut:])
        text = summary.summary_message(
            note, tools=summary.tools_used(to_summarize), transcript=self._transcript(view, state),
            head_dropped=head_dropped, kept=kept)
        lead = list(await view.residents(lead=True))
        after: list[Message] = []
        read_tool = (self.read_tool or view.find_tool("Read")) if self.restore else None
        if read_tool is not None and view.env is not None:
            raw_cut = sum(1 for s in state.history_seqs if s <= through)
            restored, records = await restore_files(
                recent_files(state.history, raw_cut), read_tool,
                self._restore_context(view, state))
            after.extend(restored)
            for ext_name, kind, data in records:
                view.record(ext_name, kind, data)
        after.extend(await view.residents())
        view.compacted(Compacted(summary=text, through=through, lead=lead, after=after,
                                 usage=usage))
        view.record(self.name, "compacted", {"reason": reason, "through": through,
                                             "estimated": estimated, "turn": _user_turns(state)})
        log.info("task %s: compacted through seq %s (%s)", view.task_id, through, reason)
        return simulated_state(view, {self.name: self.reduce})

    def _transcript(self, view: TaskView, state: TaskState) -> str | None:
        """Write the whole conversation so far as JSON lines and return the
        file's path; ``None`` when there is nowhere the model could read it."""
        if not self.transcripts or view.env is None or not is_local(view.env):
            return None
        cwd = str(state.config.get("cwd") or view.env.cwd)
        path = project_dir(cwd, self.home or noeta_home()) / f"{view.task_id}.jsonl"
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
            shorter = summary.drop_oldest_rounds(attempt)
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
