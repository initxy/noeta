"""What the terminal app shows of a task: the transcript rendered from what
the task records, live model output streamed into it, approval and question
cards (sub-agents' too), and the status bar. :class:`TranscriptView` is the
base of :class:`~noeta.cli.tui.app.NoetaApp`, which holds this state and
handles input."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.text import Text as RichText
from textual import work
from textual.app import App
from textual.widgets import Markdown, Static
from textual.widgets.markdown import MarkdownStream

from noeta.cli.tui.widgets import (
    ApprovalCard,
    AssistantMessage,
    Notice,
    QuestionCard,
    ToolCall,
    UserMessage,
)
from noeta.core import models, records
from noeta.core.events import (
    Answer,
    ExtensionRecorded,
    InputTaken,
    ModelCalled,
    ModelReplied,
    Recorded,
    Say,
    TaskEnded,
    ToolFinished,
    ToolStarted,
    Waiting,
)
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

log = logging.getLogger("noeta.cli.tui")


def _short_path(path: str, width: int) -> str:
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home):]
    return path if len(path) <= width else "…" + path[-(width - 1):]


class TranscriptView(App[None]):
    """Rendering and following; the state is set up by ``NoetaApp``."""

    local: Any
    model: str
    cwd: str
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
    waiting_question: str
    question_task: str
    question_detail: dict[str, Any]
    question_answers: dict[str, Any]
    shown_requests: set[tuple[str, str]]
    waiting_for: str
    _deltas: asyncio.Queue[Delta]

    if TYPE_CHECKING:
        async def decide(self, call_id: str, choice: str, *, task_id: str = "") -> None: ...

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
                self.waiting_for = f"{who} waits for your approval (y/a/n)"

                async def decide(call_id: str, choice: str,
                                 task_id: str = p.task_id) -> None:
                    await self.decide(call_id, choice, task_id=task_id)

                await transcript.mount(ApprovalCard(
                    p.request_id, card_title(p, self.task_id), card_prompt(p), decide,
                    choices(p.detail)))
            else:
                self.waiting_for = f"{who} waits for your answer"
                self._set_question(p.request_id, p.task_id, p.detail)
                await transcript.mount(QuestionCard(p.detail, who=who))
        transcript.scroll_end(animate=False)
        self.update_status()

    # ------------------------------------------------------------ rendering

    def _on_delta(self, task_id: str, delta: Delta) -> None:
        if task_id == self.task_id and delta.kind == "text" and delta.text:
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
        key = (delta.call_seq, delta.attempt)
        if delta.attempt < self.attempts.get(delta.call_seq, 1):
            return  # the tail of an attempt that failed
        if self.stream is not None and key != self.stream_key:
            same_call = delta.call_seq == self.stream_key[0]
            await self._end_stream(drop=same_call)  # a retry replaces its failed attempt
        if self.stream is None:
            widget = AssistantMessage("")
            await self.query_one("#transcript").mount(widget)
            self.query_one("#transcript").anchor()
            self.stream, self.stream_widget, self.stream_key = \
                Markdown.get_stream(widget), widget, key
        self.attempts[delta.call_seq] = delta.attempt
        self.streamed.add(delta.call_seq)
        self.stream_text += delta.text
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
            await widget.remove()

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
        if isinstance(ev, InputTaken):
            for item in ev.items:
                if isinstance(item.body, Say) and item.body.message.origin in (None, "human"):
                    await transcript.mount(UserMessage(item.body.message.text))
                    self.busy = True
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
                await transcript.mount(AssistantMessage(msg.text))
            self.streamed.discard(self.called)
            for call in msg.tool_uses:
                block = ToolCall(call)
                self.calls[call.id] = block
                await transcript.mount(block)
            self.context_tokens = ev.response.usage.input + ev.response.usage.output
            self._refresh_cost(self.task_id)
        elif isinstance(ev, ToolStarted):
            if ev.call.id in self.calls:
                self.calls[ev.call.id].set_state("running")
        elif isinstance(ev, ToolFinished):
            finished = self.calls.get(ev.result.call_id)
            if finished is not None:
                diff = ev.meta.get("diff") if isinstance(ev.meta.get("diff"), str) else None
                finished.set_result(ev.result.text, is_error=ev.result.is_error, diff=diff)
        elif isinstance(ev, ExtensionRecorded):
            await self._render_record(rec, ev, transcript)
        elif isinstance(ev, Waiting):
            await self._render_waiting(ev, transcript)
        elif isinstance(ev, TaskEnded):
            self.busy = False
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
        self._take_modes(published_modes([rec]))
        # The core's own records by type: a plugin may record the same
        # kind names (the hooks plugin's "retry") for something else.
        core = records.decode(ev)
        if ev.extension == "title" and ev.kind == "title":
            self.sub_title = str(data.get("title") or "")
        elif isinstance(core, records.Retry):
            await self._retried(int(core.attempt or 1))
            await transcript.mount(Notice(f"retrying (attempt {core.attempt}): "
                                          f"{str(core.error or '')[:120]}"))
        elif isinstance(core, records.TurnStopped):
            await self._end_stream()
            self._set_question("", "", {})
            await transcript.mount(Notice("stopped"))
        elif isinstance(core, records.TurnFailed):
            await transcript.mount(Notice(f"turn failed: {failure_text(data)}",
                                          classes="-error"))
        elif isinstance(core, records.Notice):
            # Structured news (a review's findings) reads as a list.
            await transcript.mount(Markdown(core.text, classes="notice")
                                   if core.notice_kind else Notice(core.text))

    async def _render_waiting(self, ev: Waiting, transcript: Any) -> None:
        await self._end_stream()
        self.waiting_for = {"approval": "waiting for your approval (y/a/n)",
                            "question": "waiting for your answer",
                            "children": "waiting for subtasks"}.get(ev.on, "")
        if ev.on == "approval":
            uses = {i: block.call for i, block in self.calls.items()}
            for p in approvals(ev, uses, self.task_id):
                if p.request_id in self.calls:
                    self.calls[p.request_id].set_state("waiting")
                await transcript.mount(ApprovalCard(
                    p.request_id, card_title(p, self.task_id), card_prompt(p),
                    self.decide, choices(p.detail)))
        elif ev.on == "question":
            questions = [k for k, v in ev.detail.items()
                         if isinstance(v, dict) and v.get("on") == "question"]
            for call_id in questions[:1]:  # one at a time: the next line answers it
                self._set_question(call_id, self.task_id, ev.detail[call_id])
                await transcript.mount(QuestionCard(ev.detail[call_id]))
        elif ev.on == "input":
            self.busy = False
            self._set_question("", "", {})

    def _take_modes(self, offered: Modes | None) -> None:
        if offered is not None and offered.cycle:
            self.modes, self.modes_published = offered, True

    def update_status(self) -> None:
        spec = models.find_spec(self.model)
        window = spec.context_window if spec else models.UNKNOWN_MODEL_CONTEXT_WINDOW
        used = f"{self.context_tokens / window:.0%}" if self.context_tokens else "-"
        cost = f" · ${self.cost_usd:.2f}" if self.cost_usd else ""
        state = self.waiting_for or ("working… (Esc to stop)" if self.busy else "ready")
        width = max(self.size.width - len(self.model) - len(state) - 30, 12)
        self.query_one("#status", Static).update(RichText(
            f"{state} · {self.model} · {self.mode or self.permission_mode or 'auto'} mode "
            f"(shift+tab) · context {used}{cost} · {_short_path(self.cwd, width)}"))

    @work(exclusive=True, group="cost", exit_on_error=False)
    async def _refresh_cost(self, task_id: str) -> None:
        """The status bar's cost: the whole tree's (sub-agents too), priced
        per model as ``/cost`` prices it."""
        total = await tree_usage(self.local.client, task_id)
        if task_id == self.task_id:
            self.cost_usd = total.cost_usd
            self.update_status()
