"""What the terminal app shows of a task: the transcript rendered from what
the task records, live model output streamed into it, approval and question
cards (sub-agents' too), the task list, the spinner and the footer.
:class:`TranscriptView` is the base of :class:`~noeta_tui.app.NoetaApp`,
which holds this state and handles input."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import AsyncIterator, Collection
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import work
from textual.app import App, ScreenStackError
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import Markdown, Static
from textual.widgets.markdown import MarkdownStream

from noeta.core import models, records
from noeta.core.events import (
    Answer,
    ChildEnded,
    ExtensionRecorded,
    External,
    InputTaken,
    ModelCalled,
    ModelReplied,
    Recorded,
    Say,
    TaskCreated,
    TaskEnded,
    ToolFinished,
    ToolStarted,
    Waiting,
)
from noeta.core.loop.state import CONFIG_SIGNAL, PLUGINS_OFF, off_names
from noeta.core.runner.texts import REJECTED_MARKER
from noeta.core.types import Delta
from noeta.host.feed import failure_text
from noeta.host.pending import (
    Pending,
    approvals,
    card_prompt,
    card_title,
    choices,
    follow_tree,
)
from noeta.host.usage import tree_usage
from noeta.toolkit.permission_modes import Modes, current_mode, published_modes
from noeta.toolkit.plugin_records import TITLE, AutoCompact, auto_compact
from noeta.toolkit.tokens import estimate_tokens
from noeta_tui import plugin_ui, render
from noeta_tui.agents import (
    AgentsPanel,
    Row,
    agent_call_title,
    child_activity,
    new_child,
    stats_text,
    took,
)
from noeta_tui.widgets import (
    ApprovalCard,
    AssistantMessage,
    CleanMarkdown,
    Notice,
    PromptInput,
    QuestionCard,
    ThinkingBlock,
    ToolCall,
    ToolGroup,
    UserMessage,
    bulleted,
)

log = logging.getLogger("noeta_tui")


SERVER_DIRECTORY = "the server's directory"
#: The workflow plugin's records a run's call shows.
WORKFLOW_KINDS = ("run", "agent", "result", "agent_failed", "ended")
#: Seconds between two spinner frames.
SPINNER_TICK = 0.12
#: The agents' list key of a workflow run's line, before its run id.
WORKFLOW_KEY = "workflow:"
#: What the footer says while the task waits on one of its approval cards.
WAITING_APPROVAL = "waiting for your approval"
#: Seconds the plugins' UI is fetched after the task's log advanced: the
#: records of a busy stretch are one fetch.
UI_DEBOUNCE = 0.3


def failure_reason(exc: BaseException) -> str:
    """What an error says: a server's own words for a request it refused
    (its ``{"error": …}``), else the error's text."""
    response = getattr(exc, "response", None)
    try:
        said = response.json().get("error") if response is not None else None
    except (ValueError, AttributeError):  # not a JSON object: the error's own text
        said = None
    return str(said or exc or type(exc).__name__)


def _short_path(path: str, width: int) -> str:
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home):]
    return path if len(path) <= width else "…" + path[-(width - 1):]


class TranscriptView(App[None]):
    """Rendering and following; the state is set up by ``NoetaApp``."""

    local: Any
    on_server: bool
    model: str
    cwd: str
    #: The directory the task runs in, as its ``TaskCreated`` recorded it
    #: ("" when it named none: a server's own).
    task_cwd: str
    permission_mode: str | None
    mode: str
    modes: Modes
    modes_published: bool
    task_id: str
    calls: dict[str, ToolCall]
    stream: MarkdownStream | None
    stream_widget: AssistantMessage | None
    stream_key: tuple[int, int]
    stream_text: str
    streamed: set[int]
    attempts: dict[int, int]
    called: int
    #: The newest model call whose reply is shown: what its stream still
    #: brings (taken before the reply was read from the log) is not shown again.
    replied: int
    _pumping: bool
    busy: bool
    cost_usd: float
    context_tokens: int
    #: When the task's compaction plugin compacts (``None``: not said).
    compacts_at: AutoCompact | None
    waiting_question: str
    question_task: str
    question_detail: dict[str, Any]
    question_answers: dict[str, Any]
    shown_requests: set[tuple[str, str]]
    #: The task's approval cards still open, by request id.
    open_cards: dict[str, ApprovalCard]
    waiting_for: str
    _deltas: asyncio.Queue[Delta]
    #: The task list (the todos plugin's records), by id.
    todos: dict[str, dict[str, Any]]
    #: Every call's result shown in full (ctrl+o).
    verbose: bool
    #: Messages sent while a turn ran, not yet taken by the task.
    queued: list[str]
    #: Messages shown as they were sent, not yet seen in the task's log.
    echoed: list[str]
    #: When the running turn started (``time.monotonic``), and the output
    #: tokens it produced so far.
    turn_started: float
    turn_tokens: int
    turn_output: int
    turn_verb: str
    #: When the record being rendered was written, and the one that started
    #: the running turn (wall clock, as the log has them).
    _record_at: float
    turn_started_at: float
    #: The task list is shown (ctrl+t hides it).
    show_tasks: bool
    #: What the footer says for a moment (``Press Ctrl-C again to exit``).
    flash: str
    #: The effort the task's config names (``None``: the model's default).
    effort: str | None
    #: The reasoning being streamed (shown above the reply), and its model call.
    thinking: ThinkingBlock | None
    thinking_key: tuple[int, int]
    #: The model is streaming reasoning, not text, right now.
    thinking_now: bool
    #: Background agents' calls, by child task id; workflow calls by run id.
    agents: dict[str, ToolCall]
    workflows: dict[str, ToolCall]
    #: A running call's output that came before its call was shown, by call id.
    early_progress: dict[str, str]
    #: Every sub-agent seen (the model's and its workflows'), by child task
    #: id: what it is doing, its counts, how it ended (``agents.new_child``).
    subagents: dict[str, dict[str, Any]]
    #: Every workflow call, by run id (``workflows`` keeps the running ones).
    workflow_calls: dict[str, ToolCall]
    #: The agents' list, in the order they started: child ids, and
    #: ``workflow:<run id>`` for a run.
    listed_agents: list[str]
    #: The list keeps what ended after this (wall clock): the person's last send.
    agents_since: float
    #: The plugins the task runs without (its config's ``plugins_off``, as
    #: last seen or switched here).
    plugins_off: list[str]
    #: The notices the task's plugins showed, by (plugin, key): their text.
    ui_notices: dict[tuple[str, str], str]
    #: The plugins' widgets drawn over (``above``) and under (``below``) the prompt.
    ui_widgets: dict[str, list[str]]
    #: The task's log advanced since its plugins' UI was last fetched.
    _ui_wanted: asyncio.Event
    #: While a task's log is shown at once (:meth:`show_log`): the widgets
    #: rendered from it, not yet mounted; ``None`` when rendering live.
    _batch: list[Widget] | None

    if TYPE_CHECKING:
        async def decide(self, call_id: str, choice: str, message: str = "", *,
                         task_id: str = "") -> None: ...

        async def close_agent(self) -> None: ...

    def _set_question(self, call_id: str, task_id: str, detail: dict[str, Any]) -> None:
        self.waiting_question = call_id
        self.question_task = task_id
        self.question_detail = detail
        self.question_answers = {}

    async def show_requests(self, found: list[Pending]) -> None:
        """Sub-agents' approvals and questions, shown with the sub-agent's
        name and answered on its task."""
        transcript = self.query_one("#transcript")
        for p in found:
            key = (p.task_id, p.request_id)
            if key in self.shown_requests:
                continue
            if p.kind == "question" and self.waiting_question:
                continue  # one question at a time
            self.shown_requests.add(key)
            await self._back_to_transcript()
            who = p.agent or "sub-agent"
            if p.kind == "approval":
                self.waiting_for = f"{who} waits for your approval"

                async def decide(call_id: str, choice: str, message: str = "",
                                 task_id: str = p.task_id) -> None:
                    await self.decide(call_id, choice, message, task_id=task_id)

                await transcript.mount(ApprovalCard(
                    p.request_id, card_title(p, self.task_id), card_prompt(p), decide,
                    choices(p.detail), preview=self._preview(p)))
            else:
                self.waiting_for = f"{who} waits for your answer"
                self._set_question(p.request_id, p.task_id, p.detail)
                await transcript.mount(QuestionCard(p.detail, who=who))
        transcript.scroll_end(animate=False)
        self.update_status()

    async def _add(self, widget: Widget) -> None:
        """``widget`` at the transcript's end (into the batch while a log is
        shown at once)."""
        if self._batch is not None:
            self._batch.append(widget)
        else:
            await self.query_one("#transcript").mount(widget)

    def _last_shown(self) -> Widget | None:
        if self._batch:
            return self._batch[-1]
        shown = self.query_one("#transcript").children
        return shown[-1] if shown else None

    async def _run_to_join(self) -> ToolGroup:
        """The run of calls a quiet call joins: the one the transcript ends
        with, else a new one. The model's thought just before the call
        (shown while it streamed) joins the run first."""
        last = self._last_shown()
        thought = last if isinstance(last, ThinkingBlock) and last.took is not None else None
        if thought is not None:
            await thought.remove()
            last = self._last_shown()
        group = last if isinstance(last, ToolGroup) else None
        if group is None:
            group = ToolGroup(expanded=self.verbose)
            await self._add(group)
        if thought is not None:
            await group.add(thought.copy())
        return group

    # ------------------------------------------------------------ busy

    def _since(self) -> float:
        """Seconds since the record being shown was written: none for one
        shown as it comes, its age for one of a log shown at once."""
        return max(0.0, time.time() - self._record_at) if self._batch is not None else 0.0

    def set_busy(self, busy: bool) -> None:
        """A turn starts or ends: the spinner runs while it does."""
        if busy and not self.busy:
            self.turn_started = time.monotonic() - self._since()
            self.turn_started_at = self._record_at
            self.turn_tokens, self.turn_output = 0, 0
            self.turn_verb = random.choice(render.VERBS)
        self.busy = busy
        self.update_status()

    def _tick(self) -> None:
        """The spinner's next frame (a timer while the app runs)."""
        try:
            spinner = self.query_one("#spinner", Static)
        except (NoMatches, ScreenStackError):  # the app is closing
            return
        frame = int(time.monotonic() / SPINNER_TICK)
        for call in [*self.agents.values(), *self.workflows.values()]:
            call.pulse(frame)
        show = self.busy and not self.waiting_for
        spinner.display = show
        if show:
            frame = int((time.monotonic() - self.turn_started) / SPINNER_TICK)
            verb = render.active_form(self.todos) or self.turn_verb
            spinner.update(render.spinner_line(frame, verb, time.monotonic() - self.turn_started,
                                               self.turn_tokens, thinking=self.thinking_now))

    async def _turn_done(self) -> None:
        if self.busy and self.turn_started:
            # A turn of a log shown at once took what its records say.
            took = render.elapsed_text(self._record_at - self.turn_started_at
                                       if self._batch is not None
                                       else time.monotonic() - self.turn_started)
            await self._add(Notice(Text(f"✻ Worked for {took}", style="dim"), classes="worked"))
        self.set_busy(False)

    # ------------------------------------------------------------ rendering

    def _on_delta(self, task_id: str, delta: Delta) -> None:
        if task_id == self.task_id and delta.kind in ("text", "thinking", "tool") and delta.text:
            self._deltas.put_nowait(delta)

    @work(exclusive=False, group="deltas", exit_on_error=False)
    async def _pump_deltas(self) -> None:
        while True:
            delta = await self._deltas.get()
            self._pumping = True
            try:
                while self._batch is not None:  # shown after the log being shown
                    await asyncio.sleep(0.01)
                await self._show_delta(delta)
            except Exception:
                log.exception("showing live output failed")
            finally:
                self._pumping = False

    async def _show_delta(self, delta: Delta) -> None:
        if delta.kind == "tool":
            if (block := self.calls.get(delta.call_id)) is not None:
                block.set_progress(delta.text)
            else:  # the call runs before its reply is shown: shown with it
                self.early_progress[delta.call_id] = delta.text
            return
        key = (delta.call_seq, delta.attempt)
        if delta.attempt < self.attempts.get(delta.call_seq, 1) or delta.call_seq <= self.replied:
            return  # the tail of an attempt that failed, or of a reply shown
        transcript = self.query_one("#transcript")
        self.turn_tokens += estimate_tokens(delta.text)
        if delta.kind == "thinking":
            self.thinking_now = True
            if self.thinking is not None and self.thinking_key != key:
                await self._end_thinking(drop=self.thinking_key[0] == key[0])
            if self.thinking is None:
                self.thinking = ThinkingBlock(expanded=self.verbose, started=time.monotonic())
                self.thinking_key = key
                await transcript.mount(self.thinking)
                self._follow_end()
            self.thinking.add(delta.text)
            return
        self.thinking_now = False
        await self._end_thinking()
        if self.stream is not None and key != self.stream_key:
            same_call = delta.call_seq == self.stream_key[0]
            await self._end_stream(drop=same_call)  # a retry replaces its failed attempt
        if self.stream is None:
            widget = AssistantMessage("")
            await transcript.mount(bulleted(widget))
            self._follow_end()
            self.stream, self.stream_widget, self.stream_key = \
                Markdown.get_stream(widget), widget, key
        self.attempts[delta.call_seq] = delta.attempt
        self.streamed.add(delta.call_seq)
        self.stream_text += delta.text
        await self.stream.write(delta.text)

    def _follow_end(self) -> None:
        """Keep the transcript at its end as live output grows — once it
        fills the screen: Textual's anchor on a shorter one pushes it to the
        bottom of the screen."""
        transcript = self.query_one("#transcript")
        if transcript.max_scroll_y > 0:
            transcript.anchor()
        else:
            transcript.scroll_end(animate=False)

    async def _end_thinking(self, *, drop: bool = False) -> None:
        block, self.thinking = self.thinking, None
        self.thinking_now = False
        if block is None:
            return
        if drop:
            await block.remove()
        else:
            block.finish(time.monotonic() - block.started)

    async def _end_stream(self, *, drop: bool = False) -> None:
        await self._end_thinking(drop=drop)
        # Taken off the app before the first await: the delta pump and the
        # follower both end streams, and the other may start the next one
        # (the retry's) meanwhile.
        stream, widget, text = self.stream, self.stream_widget, self.stream_text
        self.stream, self.stream_widget, self.stream_text = None, None, ""
        if drop and widget is not None:
            self.streamed.discard(self.stream_key[0])
        if stream is not None:
            try:
                await stream.stop()
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                # Textual's stream task was cancelled before it first ran
                # (a loaded loop): the cancel surfaces here and what was
                # written never reached the widget.
                if not drop and widget is not None:
                    await widget.update(text)
        if drop and widget is not None:
            row = widget.parent  # the bullet's row
            await (row if isinstance(row, Widget) else widget).remove()

    async def _retried(self, failed_attempt: int) -> None:
        """A model call's attempt failed: what it streamed is dropped."""
        call = self.called
        self.attempts[call] = max(self.attempts.get(call, 1), failed_attempt + 1)
        if self.stream_widget is not None and self.stream_key[0] == call \
                and self.stream_key[1] <= failed_attempt:
            await self._end_stream(drop=True)

    async def show_log(self, task_id: str) -> int:
        """What ``task_id``'s log holds now, shown at once: its records
        rendered into widgets first, mounted in one go (one by one, Textual
        lays the screen out again for each). The seq of its last record, for
        :meth:`follow` to go on after."""
        try:
            found = await self.local.client.events(task_id)
        except Exception:  # the follower reads it then, and says so if it cannot
            log.warning("reading the log of %s failed", task_id, exc_info=True)
            return 0
        batch: list[Widget] = []
        self._batch = batch
        try:
            for rec in found:
                try:
                    await self.render_event(rec)
                except Exception:
                    log.exception("rendering %s failed", type(rec.event).__name__)
        finally:
            self._batch = None
        if task_id != self.task_id:
            return 0
        transcript = self.query_one("#transcript")
        with self.batch_update():
            await transcript.mount_all(batch)
            transcript.scroll_end(animate=False)
        # Kept at the end as what comes next (a warning that wraps) grows it.
        self.call_after_refresh(self._follow_end)
        self._refresh_cost(task_id)
        self._ui_wanted.set()
        self.update_status()
        return found[-1].seq if found else 0

    @work(exclusive=True, group="follow", exit_on_error=False)
    async def follow(self, task_id: str, after: int = 0) -> None:
        """Render what ``task_id`` records after ``after``, as it comes."""
        try:
            async for rec in follow_tree(self.local.client, task_id, after):
                if task_id != self.task_id:
                    return
                try:
                    if isinstance(rec, list):
                        await self.show_requests(rec)
                    else:
                        await self.render_event(rec)
                        self._ui_wanted.set()
                except Exception:
                    log.exception("rendering %s failed", type(getattr(rec, "event", rec)).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("following task %s failed", task_id)
            await self.say_notice(f"lost the task's event stream: {exc} — /resume {task_id}")
            return
        await self.say_notice("the task has ended")

    async def say_notice(self, text: str) -> None:
        await self.query_one("#transcript").mount(Notice(text))

    @contextlib.asynccontextmanager
    async def failures_shown(self, what: str) -> AsyncIterator[None]:
        """Around what the person asked for (a line sent, a key, a pick): a
        failure — the server unreachable or refusing, a command's own error
        — is shown as ``<what> failed: …``; raised, it would end the app."""
        try:
            yield
        except Exception as exc:
            log.warning("%s failed", what, exc_info=True)
            await self.say_notice(f"{what} failed: {failure_reason(exc)}")

    async def render_event(self, rec: Recorded) -> None:
        ev = rec.event
        self._record_at = rec.at
        if isinstance(ev, TaskCreated):
            self.task_cwd = str(ev.config.get("cwd") or self.task_cwd)
            # What the task runs with: its config's effort, else the model's default.
            self.effort = ev.config.get("effort") or None
            self.plugins_off = off_names(ev.config)
        elif isinstance(ev, InputTaken):
            for item in ev.items:
                if isinstance(item.body, Say) and item.body.message.origin in (None, "human"):
                    text = item.body.message.text
                    if text in self.queued:
                        self.queued.remove(text)
                        self._show_queued()
                    if text in self.echoed:
                        self.echoed.remove(text)  # shown when it was sent
                    else:
                        await self._add(UserMessage(text))
                    self.set_busy(True)
                elif isinstance(item.body, External) and item.body.name == CONFIG_SIGNAL \
                        and isinstance(item.body.data, dict):
                    if "effort" in item.body.data:
                        self.effort = item.body.data["effort"] or None
                    if PLUGINS_OFF in item.body.data:
                        self.plugins_off = off_names(item.body.data)
                elif isinstance(item.body, ChildEnded):
                    self._agent_ended(item.body, rec.at)
                elif isinstance(item.body, Answer):
                    await self._close_cards([item.body.request_id], item.body)
                    if item.body.request_id == self.waiting_question and \
                            self.question_task in ("", self.task_id):
                        self._set_question("", "", {})
        elif isinstance(ev, ModelCalled):
            await self._close_cards()
            self.called = rec.seq
            if not self.busy:  # a turn nobody typed: an agent's notice woke the task
                self.set_busy(True)
        elif isinstance(ev, ModelReplied):
            # Every delta written first (one taken off the queue may still be
            # mounting its widget), or the reply would be shown twice. A log
            # shown at once holds replies only: its deltas wait for it.
            while self._batch is None and (not self._deltas.empty() or self._pumping):
                await asyncio.sleep(0.01)
            await self._end_stream()
            self.replied = self.called
            msg = ev.response.message
            if msg.text.strip() and self.called not in self.streamed:
                await self._add(bulleted(AssistantMessage(msg.text)))
            self.streamed.discard(self.called)
            for call in msg.tool_uses:
                block = ToolCall(call, expanded=self.verbose, cwd=self.task_cwd)
                self.calls[call.id] = block
                if (early := self.early_progress.pop(call.id, None)) is not None:
                    block.set_progress(early)
                if not render.call_kind(call):
                    await self._add(block)
                    continue
                await (await self._run_to_join()).add(block)
            self.context_tokens = ev.response.usage.input + ev.response.usage.output
            # The reply's real count replaces what its stream was estimated at.
            self.turn_output += ev.response.usage.output
            self.turn_tokens = self.turn_output
            if self._batch is None:  # a log shown at once is priced once
                self._refresh_cost(self.task_id)
        elif isinstance(ev, ToolStarted):
            await self._close_cards([ev.call.id])
            if ev.call.id in self.calls:
                self.calls[ev.call.id].set_state("running")
        elif isinstance(ev, ToolFinished):
            await self._close_cards([ev.result.call_id])
            finished = self.calls.get(ev.result.call_id)
            if finished is not None:
                finished.set_result(ev.result.text, is_error=ev.result.is_error, meta=ev.meta,
                                    at=rec.at)
                if finished.child is not None and not finished.child["status"]:
                    self.agents[finished.child["id"]] = finished
                    self._track_agent(finished)
                    self._watch_agents()
                if finished.workflow is not None:
                    self._track_workflow(finished, rec.at)
                if ev.note and ev.note != REJECTED_MARKER:
                    await finished.set_note(ev.note)
        elif isinstance(ev, ExtensionRecorded):
            await self._render_record(rec, ev)
        elif isinstance(ev, Waiting):
            await self._render_waiting(ev)
        elif isinstance(ev, TaskEnded):
            await self._close_cards()
            await self._turn_done()
            if self._batch is None:
                self._refresh_cost(self.task_id)
            await self._add(Notice(f"task {ev.status}" + (f": {ev.error}" if ev.error else "")))
        if self._batch is None:
            self.query_one("#transcript").scroll_end(animate=False)
            self.update_status()

    async def _render_record(self, rec: Recorded, ev: ExtensionRecorded) -> None:
        data = ev.data if isinstance(ev.data, dict) else {}
        mode = current_mode([rec])
        if mode:
            self.mode = mode
        self.compacts_at = auto_compact([rec]) or self.compacts_at
        self._take_modes(published_modes([rec]))
        # The core's own records by type: a plugin may record the same
        # kind names (the hooks plugin's "retry") for something else.
        core = records.decode(ev)
        if ev.extension == TITLE and ev.kind == "title":
            self.sub_title = render.clean(str(data.get("title") or ""))
        elif ev.extension == "workflow" and ev.kind in WORKFLOW_KINDS:
            self._workflow_record(ev.kind, data, rec.at)
        elif ev.extension == "todos":
            render.fold_todos(self.todos, ev.kind, data)
            self._show_todos()
        elif isinstance(core, records.Retry):
            await self._retried(int(core.attempt or 1))
            await self._add(Notice(f"retrying (attempt {core.attempt}): "
                                   f"{str(core.error or '')[:120]}"))
        elif isinstance(core, records.TurnStopped):
            await self._close_cards()
            await self._end_stream()
            self._set_question("", "", {})
            await self._add(Notice(Text("  ⎿  Interrupted · what should noeta do instead?",
                                        style="red")))
        elif isinstance(core, records.TurnFailed):
            await self._add(Notice(f"turn failed: {failure_text(data)}", classes="-error"))
        elif isinstance(core, records.Notice) and core.notice_kind == "workflow" \
                and str((core.notice_data or {}).get("run_id")) in self.workflows:
            return  # shown in the run's call
        elif isinstance(core, records.Notice):
            # Structured news (a review's findings) reads as a list.
            await self._add(CleanMarkdown(core.text, classes="notice")
                            if core.notice_kind else Notice(core.text))

    def _agent_ended(self, ended: ChildEnded, at: float) -> None:
        call = self.agents.pop(ended.child_id, None)
        if call is not None:
            call.end_agent(ended.status, answer=ended.answer, error=ended.error,
                           tools=ended.tool_calls, tokens=ended.output_tokens, at=at)
        self._end_child(ended.child_id, ended.status, error=ended.error, at=at,
                        tools=ended.tool_calls, tokens=ended.output_tokens)

    def _end_child(self, child_id: str, status: str, *, error: str = "", at: float | None = None,
                   tools: int = 0, tokens: int = 0) -> None:
        c = self.subagents.get(child_id)
        if c is None:
            return
        if not c["status"]:
            at = at or time.time()
            c.update(status=status, error=error, took=max(0.0, at - c["started"]))
        c["ended_at"] = c["ended_at"] or at or time.time()
        c["tools"], c["tokens"] = max(tools, c["tools"]), max(tokens, c["tokens"])

    def _track_agent(self, call: ToolCall) -> None:
        """An ``Agent`` call's agent, into the agents' list."""
        c = call.child
        if c is None:
            return
        c.setdefault("ended_at", None)
        c["name"], c["kind"] = agent_call_title(call.call)
        self.subagents[c["id"]] = c
        if c["id"] not in self.listed_agents:
            self.listed_agents.append(c["id"])

    def _track_workflow(self, call: ToolCall, at: float | None = None) -> None:
        run = call.workflow
        if run is None:
            return
        rid = str(run["run_id"])
        run.setdefault("started", at or time.time())
        self.workflow_calls[rid] = call
        if not run.get("status"):
            self.workflows[rid] = call
        if WORKFLOW_KEY + rid not in self.listed_agents:
            self.listed_agents.append(WORKFLOW_KEY + rid)

    @work(exclusive=True, group="agents", exit_on_error=False)
    async def _watch_agents(self) -> None:
        """While background agents run: what each is doing, read from its
        own log (the tools it called, what it wrote) once a second."""
        while running := {cid: c for cid, c in self.subagents.items() if not c["status"]}:
            for child_id, c in running.items():
                call = self.agents.get(child_id)
                try:
                    events = await self.local.client.events(child_id, c["seq"])
                except Exception:
                    log.debug("reading agent %s failed", child_id, exc_info=True)
                    continue
                for r in events:
                    c["seq"] = r.seq
                    ev = r.event
                    if isinstance(ev, ToolStarted):
                        c["last"] = render.tool_header(ev.call, 80, cwd=self.task_cwd)
                    elif isinstance(ev, ToolFinished):
                        c["tools"] += 1
                    elif isinstance(ev, ModelReplied):
                        c["tokens"] += ev.response.usage.output
                        said = ev.response.message.text.strip()
                        if said:  # what it said last, as Claude Code's list shows
                            c["last"] = said.splitlines()[0]
                if not c["status"] and call is not None:
                    call.show_agent()
            self.update_status()
            await asyncio.sleep(1.0)

    def _workflow_record(self, kind: str, data: dict[str, Any], at: float | None = None) -> None:
        """Fold one of the workflow plugin's records into its run's call."""
        rid = str(data.get("run_id") or "")
        if kind == "run":
            call = self.calls.get(str(data.get("call_id") or ""))
            if call is None:
                return
            call.workflow = {"run_id": rid, "description": data.get("description") or "",
                             "agents": {}}
            self._track_workflow(call, at)
            return
        call = self.workflows.get(rid)
        if call is None or call.workflow is None:
            return
        run = call.workflow
        key = str(data.get("key") or "")
        if kind == "agent":
            child_id = str(data.get("child_id") or "")
            run["agents"][key] = {"key": key, "label": data.get("label"),
                                  "phase": data.get("phase"), "state": "running",
                                  "child_id": child_id}
            if child_id:
                c = new_child(child_id, name=str(data.get("label") or key), started=at)
                c["kind"] = str(data.get("phase") or "")
                self.subagents[child_id] = c
                self._watch_agents()
        elif kind in ("result", "agent_failed") and key in run["agents"]:
            agent = run["agents"][key]
            agent["state"] = "done" if kind == "result" else "failed"
            self._end_child(str(agent.get("child_id") or ""),
                            "done" if kind == "result" else "failed",
                            error=str(data.get("error") or ""), at=at)
        elif kind == "ended":
            run["status"], run["error"] = data.get("status"), data.get("error") or ""
            run["ended_at"] = at or time.time()
            self.workflows.pop(rid, None)
        call.show_workflow()

    def background_agents(self) -> int:
        """Agents running in the background: the model's and its workflows'."""
        running = len(self.agents)
        for call in self.workflows.values():
            running += sum(1 for a in (call.workflow or {}).get("agents", {}).values()
                           if a.get("state") == "running")
        return running

    def _child_row(self, child_id: str, name: str = "") -> Row:
        c = self.subagents[child_id]
        text, style = child_activity(c)
        state = "running" if not c["status"] else "done" if c["status"] == "done" else "failed"
        return Row(child_id, name or c["name"] or "agent", activity=text, activity_style=style,
                   stats=stats_text(took(c), c["tokens"]), state=state)

    def _workflow_row(self, rid: str) -> Row | None:
        call = self.workflow_calls.get(rid)
        run = call.workflow if call is not None else None
        if run is None or call is None:
            return None
        agents = list(run.get("agents", {}).values())
        children = [self.subagents[a["child_id"]] for a in agents
                    if a.get("child_id") in self.subagents]
        done = sum(1 for a in agents if a.get("state") == "done")
        status = run.get("status")
        state = "" if not status else "done" if status == "completed" else "failed"
        if state == "failed":
            activity = "Stopped" if status == "killed" else "Failed"
            activity += f": {run['error']}" if run.get("error") else ""
            style = "red"
        elif state == "done":
            activity, style = "Done", "dim"
        else:
            last = next((c["last"] for c in reversed(children) if not c["status"] and c["last"]),
                        "")
            activity = f"{done}/{len(agents)} agents done" + (f" · {last}" if last else "")
            style = "dim"
        end = run.get("ended_at") or time.time()
        tokens = sum(int(c["tokens"]) for c in children)
        name = str(run.get("description") or call.title)
        return Row(WORKFLOW_KEY + rid, name, extra=f"(+{len(agents)})" if agents else "",
                   activity=activity, activity_style=style,
                   stats=stats_text(max(0.0, end - float(run.get("started") or end)), tokens),
                   state=state or "running")

    def agent_rows(self) -> list[Row]:
        """The agents' list: what runs, and what ended since the person last sent."""
        rows: list[Row] = []
        for key in self.listed_agents:
            if key.startswith(WORKFLOW_KEY):
                row = self._workflow_row(key[len(WORKFLOW_KEY):])
                call = self.workflow_calls.get(key[len(WORKFLOW_KEY):])
                ended = (call.workflow or {}).get("ended_at") if call is not None else None
                running = call is not None and not (call.workflow or {}).get("status")
            else:
                c = self.subagents.get(key)
                row = self._child_row(key) if c is not None else None
                ended = c["ended_at"] if c is not None else None
                running = c is not None and not c["status"]
            if row is not None and (running or (ended or 0) >= self.agents_since):
                rows.append(row)
        return rows

    def _show_agents(self) -> None:
        try:
            panel = self.query_one("#agents", AgentsPanel)
        except (NoMatches, ScreenStackError):  # the app is closing
            return
        panel.show(self.agent_rows())

    def _show_todos(self) -> None:
        tasks = self.query_one("#tasks", Static)
        tasks.update(render.todo_block(self.todos))
        tasks.display = bool(self.todos) and self.show_tasks

    def _show_queued(self) -> None:
        box = self.query_one("#queued", Static)
        box.update(render.plain("\n".join(f"❯ {q}" for q in self.queued), "dim"))
        box.display = bool(self.queued)

    async def _back_to_transcript(self) -> None:
        """What waits on the person is in the transcript: an agent's view
        in its place closes."""
        if self.screen.query_children(".agent-view"):
            await self.close_agent()

    async def _close_cards(self, ids: Collection[str] | None = None,
                           answer: Answer | None = None, *, keep: Collection[str] = ()) -> None:
        """The task's approval cards whose requests are no longer open
        (``ids``; ``None``: all but ``keep``) keep no choices: a reopened
        task's old approvals, one answered in another window, one its turn
        left behind. Only a request still open takes an answer."""
        closing = [rid for rid in self.open_cards
                   if (ids is None or rid in ids) and rid not in keep]
        focused = False
        for rid in closing:
            card = self.open_cards.pop(rid)
            focused = await card.close(card.label(answer)) or focused
        if closing and not self.open_cards and self.waiting_for == WAITING_APPROVAL:
            self.waiting_for = ""
        if focused:
            self.query_one("#prompt", PromptInput).focus()

    async def _render_waiting(self, ev: Waiting) -> None:
        await self._close_cards(keep=ev.ids if ev.on == "approval" else ())
        await self._end_stream()
        if ev.on in ("approval", "question"):
            await self._back_to_transcript()
        self.waiting_for = {"approval": WAITING_APPROVAL,
                            "question": "waiting for your answer",
                            "children": ""}.get(ev.on, "")
        if ev.on == "approval":
            uses = {i: block.call for i, block in self.calls.items()}
            for p in approvals(ev, uses, self.task_id):
                if p.request_id in self.calls:
                    self.calls[p.request_id].set_state("waiting")
                if p.request_id in self.open_cards:
                    continue  # asked again (another worker took the task over): shown
                card = ApprovalCard(p.request_id, card_title(p, self.task_id), card_prompt(p),
                                    self.decide, choices(p.detail), preview=self._preview(p))
                self.open_cards[p.request_id] = card
                await self._add(card)
        elif ev.on == "question":
            questions = [k for k, v in ev.detail.items()
                         if isinstance(v, dict) and v.get("on") == "question"]
            for call_id in questions[:1]:  # one at a time: the next line answers it
                self._set_question(call_id, self.task_id, ev.detail[call_id])
                await self._add(QuestionCard(ev.detail[call_id]))
        elif ev.on == "input":
            self._set_question("", "", {})
            await self._turn_done()

    def _preview(self, p: Pending) -> Text | None:
        """The diff an edit's approval shows, against the file on this
        machine (a server's files are not here)."""
        if p.tool not in ("Edit", "Write"):
            return None
        current = None
        if not self.on_server and (path := str(p.input.get("file_path") or "")):
            target = Path(self.task_cwd or self.cwd, path).expanduser()
            try:
                current = target.read_text("utf-8") if target.is_file() else None
            except (OSError, UnicodeDecodeError):
                current = None
        rows = render.edit_preview(p.tool, p.input, current)
        return render.rows_text(rows) if rows else None

    def _take_modes(self, offered: Modes | None) -> None:
        if offered is not None and offered.cycle:
            self.modes, self.modes_published = offered, True

    def effort_level(self) -> str | None:
        """The effort the next request asks for: the task's, whatever the
        model, else the model's default for a catalogued reasoning model;
        ``None`` when none is sent (the model's own default applies)."""
        if self.effort:
            return self.effort
        spec = models.find_spec(self.model)
        return models.default_effort(self.model) if spec is not None and spec.is_reasoning \
            else None

    def effort_said(self) -> str:
        """The effort as ``/status`` and the ``/effort`` picker say it."""
        level = self.effort_level()
        if level is None:
            return "none sent, the model's own default applies"
        return level if self.effort else f"{level}, the model's default"

    def update_status(self) -> None:
        """The footer: on the left the mode (or what waits on the person, or
        a passing word) and the agents working in the background; on the
        right the model and its effort, how full the context is and the
        cost. The directory is a plugin's to show (``project_status``)."""
        if self._batch is not None:  # shown once the log is
            return
        spec = models.find_spec(self.model)
        window = spec.context_window if spec else models.UNKNOWN_MODEL_CONTEXT_WINDOW
        at = self.compacts_at
        left = render.context_left(self.context_tokens, at.threshold, at.window, at.auto) \
            if at is not None else None
        hints = Text()
        if self.flash:
            hints.append(self.flash, style="dim")
        elif self.waiting_for:
            hints.append(self.waiting_for, style="yellow")
        else:
            hints.append_text(render.mode_hint(self.mode or self.permission_mode or ""))
        running = self.background_agents()
        if running and not self.flash:
            hints.append("  ·  ", style="dim")
            hints.append(f"◆ {running} agent{'s' if running > 1 else ''} running",
                         style=render.WORKING)
            hints.append(" · ↓ to view", style="dim")
        self._show_agents()
        self.query_one("#hints", Static).update(hints)
        sep = ("  ·  ", "dim")
        status = Text(self.model, style="dim")
        level = self.effort_level()
        if level:
            status.append(f" {render.EFFORT_GLYPHS.get(level, '●')} {level}", style="dim")
        status.append(*sep)
        status.append_text(left if left is not None
                           else render.context_bar(self.context_tokens / window))
        # The cost only when it fits beside the left side.
        room = self.size.width - 6 - len(hints.plain)
        if self.cost_usd and len(status.plain) + 12 <= room:
            status.append(*sep)
            status.append(f"${self.cost_usd:.2f}", style="dim")
        self.query_one("#status", Static).update(status)
        self._tick()

    @work(exclusive=True, group="plugin-ui", exit_on_error=False)
    async def _fetch_ui(self) -> None:
        """While the app runs: the task's plugins' UI fetched again
        :data:`UI_DEBOUNCE` after its log advanced. A failure is logged and
        what was shown stays."""
        while True:
            await self._ui_wanted.wait()
            await asyncio.sleep(UI_DEBOUNCE)
            self._ui_wanted.clear()  # what came meanwhile is in this fetch
            task_id = self.task_id
            if not task_id:
                continue
            try:
                items = await self.local.ui(task_id)
                if task_id == self.task_id:
                    await self.show_ui(items)
            except Exception:
                log.warning("fetching the plugins' UI of %s failed", task_id, exc_info=True)

    async def show_ui(self, items: list[Any]) -> None:
        """What the task's plugins show now: status items under the footer,
        widgets over and under the prompt (redrawn only when they changed), a
        notice in the transcript when its text is new."""
        items = plugin_ui.keyed(i for i in items if isinstance(i, dict))
        status = self.query_one("#plugin-status", Static)
        statuses = [i for i in items if i.get("type") == "status"]
        status.update(plugin_ui.status_line(statuses))
        status.set_class(bool(statuses), "-shown")
        for where in ("above", "below"):
            box = self.query_one(f"#widgets-{where}")
            widgets = [i for i in items if i.get("type") == "widget"
                       and (i.get("placement") == "below") == (where == "below")]
            drawn = [str(w.get("markdown") or "") for w in widgets]
            if drawn != self.ui_widgets.get(where, []):
                self.ui_widgets[where] = drawn
                await box.remove_children()
                await box.mount_all(CleanMarkdown(m, classes="plugin-widget") for m in drawn)
                box.set_class(bool(drawn), "-shown")
        transcript = self.query_one("#transcript")
        for i in items:
            key, text = (str(i.get("plugin") or ""), str(i.get("key") or "")), \
                str(i.get("text") or "")
            if i.get("type") != "notice" or not text or self.ui_notices.get(key) == text:
                continue
            self.ui_notices[key] = text
            level = str(i.get("level") or "")
            await transcript.mount(Notice(f"{key[0]}: {text}", classes=f"-{level}"
                                          if level in ("warn", "error") else ""))
            transcript.scroll_end(animate=False)
        self._sync_footer()

    async def clear_ui(self) -> None:
        """Nothing of a plugin shown (another task's screen)."""
        self.ui_notices.clear()
        self._ui_wanted.clear()
        await self.show_ui([])

    if TYPE_CHECKING:
        def _sync_footer(self) -> None: ...

    @work(exclusive=True, group="cost", exit_on_error=False)
    async def _refresh_cost(self, task_id: str) -> None:
        """The footer's cost: the whole tree's (sub-agents too), priced per
        model as ``/cost`` prices it."""
        total = await tree_usage(self.local.client, task_id)
        if task_id == self.task_id:
            self.cost_usd = total.cost_usd
            self.update_status()
