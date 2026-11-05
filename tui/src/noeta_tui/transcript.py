"""What the terminal app shows of a task: the transcript rendered from what
the task records, live model output streamed into it, approval and question
cards (sub-agents' too), the task list, the spinner and the footer.
:class:`TranscriptView` is the base of :class:`~noeta_tui.app.NoetaApp`,
which holds this state and handles input."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import work
from textual.app import App
from textual.widget import Widget
from textual.widgets import Markdown, Static
from textual.widgets.markdown import MarkdownStream

from noeta.core import models, records
from noeta.core.events import (
    Answer,
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
from noeta.core.loop.state import CONFIG_SIGNAL
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
from noeta_tui import render
from noeta_tui.widgets import (
    ApprovalCard,
    AssistantMessage,
    Notice,
    QuestionCard,
    ReadGroup,
    ToolCall,
    UserMessage,
    bulleted,
)

log = logging.getLogger("noeta_tui")


SERVER_DIRECTORY = "the server's directory"
#: Seconds between two spinner frames.
SPINNER_TICK = 0.12


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
    #: The task list is shown (ctrl+t hides it).
    show_tasks: bool
    #: What the footer says for a moment (``Press Ctrl-C again to exit``).
    flash: str
    #: The effort the task's config names (``None``: the model's default).
    effort: str | None

    if TYPE_CHECKING:
        async def decide(self, call_id: str, choice: str, message: str = "", *,
                         task_id: str = "") -> None: ...

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

    # ------------------------------------------------------------ busy

    def set_busy(self, busy: bool) -> None:
        """A turn starts or ends: the spinner runs while it does."""
        if busy and not self.busy:
            self.turn_started, self.turn_tokens, self.turn_output = time.monotonic(), 0, 0
            self.turn_verb = random.choice(render.VERBS)
        self.busy = busy
        self.update_status()

    def _tick(self) -> None:
        """The spinner's next frame (a timer while the app runs)."""
        found = self.query("#spinner")
        if not found:  # the app is closing
            return
        spinner = found.first(Static)
        show = self.busy and not self.waiting_for
        spinner.display = show
        if show:
            frame = int((time.monotonic() - self.turn_started) / SPINNER_TICK)
            verb = render.active_form(self.todos) or self.turn_verb
            spinner.update(render.spinner_line(frame, verb, time.monotonic() - self.turn_started,
                                               self.turn_tokens))

    async def _turn_done(self) -> None:
        if self.busy and self.turn_started:
            took = render.elapsed_text(time.monotonic() - self.turn_started)
            await self.query_one("#transcript").mount(Notice(
                Text(f"✻ Worked for {took}", style="dim"), classes="worked"))
        self.set_busy(False)

    # ------------------------------------------------------------ rendering

    def _on_delta(self, task_id: str, delta: Delta) -> None:
        if task_id == self.task_id and delta.kind in ("text", "tool") and delta.text:
            self._deltas.put_nowait(delta)

    @work(exclusive=False, group="deltas", exit_on_error=False)
    async def _pump_deltas(self) -> None:
        while True:
            delta = await self._deltas.get()
            self._pumping = True
            try:
                await self._show_delta(delta)
            except Exception:
                log.exception("showing live output failed")
            finally:
                self._pumping = False

    async def _show_delta(self, delta: Delta) -> None:
        if delta.kind == "tool":
            if (block := self.calls.get(delta.call_id)) is not None:
                block.set_progress(delta.text)
            return
        key = (delta.call_seq, delta.attempt)
        if delta.attempt < self.attempts.get(delta.call_seq, 1):
            return  # the tail of an attempt that failed
        if self.stream is not None and key != self.stream_key:
            same_call = delta.call_seq == self.stream_key[0]
            await self._end_stream(drop=same_call)  # a retry replaces its failed attempt
        if self.stream is None:
            widget = AssistantMessage("")
            await self.query_one("#transcript").mount(bulleted(widget))
            self.query_one("#transcript").anchor()
            self.stream, self.stream_widget, self.stream_key = \
                Markdown.get_stream(widget), widget, key
        self.attempts[delta.call_seq] = delta.attempt
        self.streamed.add(delta.call_seq)
        self.stream_text += delta.text
        self.turn_tokens += estimate_tokens(delta.text)
        await self.stream.write(delta.text)

    async def _end_stream(self, *, drop: bool = False) -> None:
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

    @work(exclusive=True, group="follow", exit_on_error=False)
    async def follow(self, task_id: str) -> None:
        try:
            async for rec in follow_tree(self.local.client, task_id):
                if task_id != self.task_id:
                    return
                try:
                    if isinstance(rec, list):
                        await self.show_requests(rec)
                    else:
                        await self.render_event(rec)
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

    async def render_event(self, rec: Recorded) -> None:
        ev = rec.event
        transcript = self.query_one("#transcript")
        if isinstance(ev, TaskCreated):
            self.task_cwd = str(ev.config.get("cwd") or self.task_cwd)
            # What the task runs with: its config's effort, else the model's default.
            self.effort = ev.config.get("effort") or None
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
                        await transcript.mount(UserMessage(text))
                    self.set_busy(True)
                elif isinstance(item.body, External) and item.body.name == CONFIG_SIGNAL \
                        and isinstance(item.body.data, dict) and "effort" in item.body.data:
                    self.effort = item.body.data["effort"] or None
                elif isinstance(item.body, Answer) and \
                        item.body.request_id == self.waiting_question and \
                        self.question_task in ("", self.task_id):
                    self._set_question("", "", {})
        elif isinstance(ev, ModelCalled):
            self.called = rec.seq
        elif isinstance(ev, ModelReplied):
            # Every delta written first (one taken off the queue may still be
            # mounting its widget), or the reply would be shown twice.
            while not self._deltas.empty() or self._pumping:
                await asyncio.sleep(0.01)
            await self._end_stream()
            msg = ev.response.message
            if msg.text.strip() and self.called not in self.streamed:
                await transcript.mount(bulleted(AssistantMessage(msg.text)))
            self.streamed.discard(self.called)
            for call in msg.tool_uses:
                block = ToolCall(call, expanded=self.verbose)
                self.calls[call.id] = block
                if call.name != "Read":
                    await transcript.mount(block)
                    continue
                last = transcript.children[-1] if transcript.children else None
                group = last if isinstance(last, ReadGroup) else ReadGroup(expanded=self.verbose)
                if group is not last:
                    await transcript.mount(group)
                await group.add(block)
            self.context_tokens = ev.response.usage.input + ev.response.usage.output
            # The reply's real count replaces what its stream was estimated at.
            self.turn_output += ev.response.usage.output
            self.turn_tokens = self.turn_output
            self._refresh_cost(self.task_id)
        elif isinstance(ev, ToolStarted):
            if ev.call.id in self.calls:
                self.calls[ev.call.id].set_state("running")
        elif isinstance(ev, ToolFinished):
            finished = self.calls.get(ev.result.call_id)
            if finished is not None:
                finished.set_result(ev.result.text, is_error=ev.result.is_error, meta=ev.meta)
                if ev.note and ev.note != REJECTED_MARKER:
                    await finished.set_note(ev.note)
        elif isinstance(ev, ExtensionRecorded):
            await self._render_record(rec, ev, transcript)
        elif isinstance(ev, Waiting):
            await self._render_waiting(ev, transcript)
        elif isinstance(ev, TaskEnded):
            await self._turn_done()
            self._refresh_cost(self.task_id)
            await transcript.mount(Notice(f"task {ev.status}" + (f": {ev.error}" if ev.error
                                                                 else "")))
        transcript.scroll_end(animate=False)
        self.update_status()

    async def _render_record(self, rec: Recorded, ev: ExtensionRecorded,
                             transcript: Any) -> None:
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
            self.sub_title = str(data.get("title") or "")
        elif ev.extension == "todos":
            render.fold_todos(self.todos, ev.kind, data)
            self._show_todos()
        elif isinstance(core, records.Retry):
            await self._retried(int(core.attempt or 1))
            await transcript.mount(Notice(f"retrying (attempt {core.attempt}): "
                                          f"{str(core.error or '')[:120]}"))
        elif isinstance(core, records.TurnStopped):
            await self._end_stream()
            self._set_question("", "", {})
            await transcript.mount(Notice(Text("  ⎿  Interrupted · what should noeta do "
                                               "instead?", style="red")))
        elif isinstance(core, records.TurnFailed):
            await transcript.mount(Notice(f"turn failed: {failure_text(data)}",
                                          classes="-error"))
        elif isinstance(core, records.Notice):
            # Structured news (a review's findings) reads as a list.
            await transcript.mount(Markdown(core.text, classes="notice")
                                   if core.notice_kind else Notice(core.text))

    def _show_todos(self) -> None:
        tasks = self.query_one("#tasks", Static)
        tasks.update(render.todo_block(self.todos))
        tasks.display = bool(self.todos) and self.show_tasks

    def _show_queued(self) -> None:
        box = self.query_one("#queued", Static)
        box.update(Text("\n".join(f"❯ {q}" for q in self.queued), style="dim"))
        box.display = bool(self.queued)

    async def _render_waiting(self, ev: Waiting, transcript: Any) -> None:
        await self._end_stream()
        self.waiting_for = {"approval": "waiting for your approval",
                            "question": "waiting for your answer",
                            "children": ""}.get(ev.on, "")
        if ev.on == "approval":
            uses = {i: block.call for i, block in self.calls.items()}
            for p in approvals(ev, uses, self.task_id):
                if p.request_id in self.calls:
                    self.calls[p.request_id].set_state("waiting")
                await transcript.mount(ApprovalCard(
                    p.request_id, card_title(p, self.task_id), card_prompt(p),
                    self.decide, choices(p.detail), preview=self._preview(p)))
        elif ev.on == "question":
            questions = [k for k, v in ev.detail.items()
                         if isinstance(v, dict) and v.get("on") == "question"]
            for call_id in questions[:1]:  # one at a time: the next line answers it
                self._set_question(call_id, self.task_id, ev.detail[call_id])
                await transcript.mount(QuestionCard(ev.detail[call_id]))
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
        """The effort the next request runs at, for a model known to take
        one (``None`` for any other)."""
        spec = models.find_spec(self.model)
        if spec is None or not spec.is_reasoning:
            return None
        return self.effort or models.default_effort(self.model)

    def update_status(self) -> None:
        spec = models.find_spec(self.model)
        window = spec.context_window if spec else models.UNKNOWN_MODEL_CONTEXT_WINDOW
        used = f"{self.context_tokens / window:.0%}" if self.context_tokens else "0%"
        cost = f" · ${self.cost_usd:.2f}" if self.cost_usd else ""
        at = self.compacts_at
        left = render.context_left(self.context_tokens, at.threshold, at.window, at.auto) \
            if at is not None else None
        hints = self.query_one("#hints", Static)
        if self.flash:
            hints.update(Text(self.flash, style="dim"))
        elif self.waiting_for:
            hints.update(Text(self.waiting_for, style="yellow"))
        else:
            hints.update(render.mode_hint(self.mode or self.permission_mode or ""))
        width = max(self.size.width // 2 - len(self.model) - 24, 12)
        status = Text(f"{self.model} · ", style="dim")
        status.append_text(left if left is not None else Text(f"context {used}", style="dim"))
        status.append(f"{cost} · {_short_path(self.task_cwd or SERVER_DIRECTORY, width)}",
                      style="dim")
        self.query_one("#status", Static).update(status)
        level = self.effort_level()
        effort = self.query_one("#effort", Static)
        effort.update(render.effort_line(level) if level else "")
        effort.display = level is not None
        self._tick()

    @work(exclusive=True, group="cost", exit_on_error=False)
    async def _refresh_cost(self, task_id: str) -> None:
        """The footer's cost: the whole tree's (sub-agents too), priced per
        model as ``/cost`` prices it."""
        total = await tree_usage(self.local.client, task_id)
        if task_id == self.task_id:
            self.cost_usd = total.cost_usd
            self.update_status()
