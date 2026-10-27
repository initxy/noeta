"""The full-screen terminal app. It is a client like any other: it creates or
resumes a task, sends what you type, and renders what the task records."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from rich.text import Text as RichText
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Markdown, Static
from textual.widgets.markdown import MarkdownStream

from noeta import models
from noeta.events import (
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
from noeta.ext.ask_user import next_question, typed_answer
from noeta.ext.permissions.pending import Pending, follow_tree
from noeta.local import LocalApp, UnknownAgent
from noeta.tui.widgets import (
    ApprovalCard,
    AssistantMessage,
    Notice,
    PromptInput,
    QuestionCard,
    ToolCall,
    UserMessage,
    tool_title,
)
from noeta.types import Delta, Usage

log = logging.getLogger("noeta.tui")

HELP = """\
**Commands**  `/new` new task · `/resume` pick a task · `/stop` stop the turn · `/exit` quit
**Keys**  Enter send · Ctrl+J new line · Esc stop the turn · Ctrl+B side panel · y/a/n approvals
"""


def _short_path(path: str, width: int) -> str:
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home):]
    return path if len(path) <= width else "…" + path[-(width - 1):]


class NoetaApp(App[None]):
    CSS_PATH = "style.tcss"
    TITLE = "noeta"
    BINDINGS = [
        Binding("escape", "stop_turn", "Stop", show=False),
        Binding("ctrl+b", "toggle_side", "Side panel", show=False),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(self, local: LocalApp | Any, *, agent: str, model: str, cwd: str,
                 resume: str | None = None, permission_mode: str | None = None) -> None:
        super().__init__()
        self.local = local
        self.agent = agent
        self.model = model
        self.cwd = cwd
        self.resume = resume
        self.permission_mode = permission_mode
        self.task_id = ""
        self.calls: dict[str, ToolCall] = {}
        self.stream: MarkdownStream | None = None
        self.stream_widget: AssistantMessage | None = None
        #: The model call (its ``ModelCalled`` seq) and attempt being streamed.
        self.stream_key = (0, 1)
        #: Model calls whose text was streamed (their reply is not shown again).
        self.streamed: set[int] = set()
        #: Per model call, the attempt still live (earlier ones failed).
        self.attempts: dict[int, int] = {}
        #: The newest model call seen in the log.
        self.called = 0
        #: A delta is taken off the queue but not yet written.
        self._pumping = False
        self.busy = False
        self.usage = Usage()
        self.context_tokens = 0
        #: The question the next input line answers: its call id, the task
        #: asking (the root or a sub-agent), its detail, and answers so far.
        self.waiting_question = ""
        self.question_task = ""
        self.question_detail: dict[str, Any] = {}
        self.question_answers: dict[str, Any] = {}
        #: Sub-agent requests already shown, by (task id, request id).
        self.shown_requests: set[tuple[str, str]] = set()
        #: The task's agent is not one this app has: history only, no input.
        self.read_only = False
        self.waiting_for = ""
        self._deltas: asyncio.Queue[Delta] = asyncio.Queue()
        self._follower: Any = None

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield VerticalScroll(id="transcript")
            with Vertical(id="side"):
                yield Static("[b]Subtasks[/b]", id="side-title")
                yield Static("", id="side-body")
        yield PromptInput(self.submit)
        yield Static("", id="status")

    async def on_mount(self) -> None:
        self.query_one("#side").display = False
        await self.local.start()
        self.local.deltas(self._on_delta)
        self._pump_deltas()
        self._refresh_side()
        if self.resume:
            await self.open_task(self.resume)
        elif self.resume == "":
            await self.pick_task()
        else:
            await self.new_task()
        self.query_one(PromptInput).focus()

    async def on_unmount(self) -> None:
        await self.local.stop()

    # ------------------------------------------------------------ tasks

    async def new_task(self) -> None:
        self.read_only = False
        config: dict[str, Any] = {"cwd": self.cwd, "model": self.model}
        if self.permission_mode:
            config["permission_mode"] = self.permission_mode
        tid = await self.local.client.create(self.agent, None, config=config)
        await self.open_task(tid)

    async def open_task(self, task_id: str) -> None:
        if self._follower is not None:
            self._follower.cancel()
        refused = ""
        try:
            await self.local.adopt(task_id)
        except UnknownAgent as exc:
            refused = str(exc)
        self.read_only = bool(refused)
        self.task_id = task_id
        self.calls.clear()
        self._set_question("", "", {})
        self.shown_requests.clear()
        self.waiting_for = ""
        self.busy = False
        await self._end_stream()
        self.streamed.clear()
        self.attempts.clear()
        self.called = 0
        transcript = self.query_one("#transcript")
        await transcript.remove_children()
        await transcript.mount(Notice(f"noeta · task {task_id[:8]} · /help for commands"))
        if refused:
            await transcript.mount(Notice(f"read-only: {refused}. Its history is shown; "
                                          "/new starts a task here.", classes="-error"))
        self._follower = self.follow(task_id)
        self.update_status()

    async def pick_task(self) -> None:
        rows = await self.local.client.tasks(limit=20)
        roots = [r for r in rows if r.parent_id is None]
        if not roots:
            await self.new_task()
            return
        await self.open_task(roots[0].task_id)
        lines = "\n".join(f"- `{r.task_id}` {r.status}" for r in roots[:10])
        await self.say_notice(f"Resumed the latest task. Others: `/resume <id>`\n{lines}")

    # ------------------------------------------------------------ input

    async def submit(self, text: str) -> None:
        if text.startswith("/"):
            await self.command(text)
            return
        if not self.task_id:
            return
        if self.read_only:
            await self.say_notice("this task is read-only here (its agent is not available)")
            return
        if self.waiting_question:
            await self.answer_question(text)
            return
        await self.local.client.send(self.task_id, text, when="next_turn")
        self.busy = True
        self.update_status()

    async def command(self, text: str) -> None:
        name, _, arg = text[1:].partition(" ")
        if name in ("exit", "quit"):
            self.exit()
        elif name == "new":
            await self.new_task()
        elif name == "resume":
            await (self.open_task(arg.strip()) if arg.strip() else self.pick_task())
        elif name == "stop":
            await self.action_stop_turn()
        elif name == "help":
            await self.query_one("#transcript").mount(Markdown(HELP, classes="notice"))
        else:
            await self.say_notice(f"unknown command /{name} — try /help")

    async def action_stop_turn(self) -> None:
        if self.task_id and self.busy:
            await self.local.client.cancel(self.task_id, "stopped from the terminal",
                                           turn_only=True)

    def action_toggle_side(self) -> None:
        side = self.query_one("#side")
        side.display = not side.display

    async def decide(self, call_id: str, decision: str, always: bool, *,
                     task_id: str = "") -> None:
        self.waiting_for = ""
        await self.local.client.answer(task_id or self.task_id, call_id,
                                       decision, always=always)  # type: ignore[arg-type]
        self.query_one(PromptInput).focus()

    def _set_question(self, call_id: str, task_id: str, detail: dict[str, Any]) -> None:
        self.waiting_question = call_id
        self.question_task = task_id
        self.question_detail = detail
        self.question_answers = {}

    async def answer_question(self, text: str) -> None:
        """The typed line answers the pending question (one line per part of
        a question with several); the answer is sent once all are in."""
        transcript = self.query_one("#transcript")
        await transcript.mount(UserMessage(text))
        answers = typed_answer(self.question_detail, text, self.question_answers)
        nxt = next_question(self.question_detail, answers)
        if nxt is not None:
            self.question_answers = answers
            await self.say_notice(f"next: {nxt.get('question', '')}")
            return
        task_id, call_id = self.question_task or self.task_id, self.waiting_question
        self._set_question("", "", {})
        await self.local.client.answer(task_id, call_id, data={"answers": answers})

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

                async def decide(call_id: str, decision: str, always: bool,
                                 task_id: str = p.task_id) -> None:
                    await self.decide(call_id, decision, always, task_id=task_id)

                await transcript.mount(ApprovalCard(
                    p.request_id, f"[{who}] {p.tool}", p.prompt, decide))
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
        await self.stream.write(delta.text)

    async def _end_stream(self, *, drop: bool = False) -> None:
        if self.stream is not None:
            await self.stream.stop()
            self.stream = None
        if drop and self.stream_widget is not None:
            await self.stream_widget.remove()
            self.streamed.discard(self.stream_key[0])
        self.stream_widget = None

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
            self.usage = self.usage + ev.response.usage
            self.context_tokens = ev.response.usage.input + ev.response.usage.output
        elif isinstance(ev, ToolStarted):
            if ev.call.id in self.calls:
                self.calls[ev.call.id].set_state("running")
        elif isinstance(ev, ToolFinished):
            finished = self.calls.get(ev.result.call_id)
            if finished is not None:
                diff = ev.meta.get("diff") if isinstance(ev.meta.get("diff"), str) else None
                finished.set_result(ev.result.text, is_error=ev.result.is_error, diff=diff)
        elif isinstance(ev, ExtensionRecorded):
            data = ev.data if isinstance(ev.data, dict) else {}
            if ev.kind == "retry":
                await self._retried(int(data.get("attempt") or 1))
                await transcript.mount(Notice(f"retrying (attempt {data.get('attempt')}): "
                                              f"{str(data.get('error', ''))[:120]}"))
            elif ev.kind == "turn_stopped":
                await self._end_stream()
                self._set_question("", "", {})
                await transcript.mount(Notice("stopped"))
            elif ev.kind == "turn_failed":
                reason = ": ".join(str(x) for x in (data.get("reason"), data.get("detail")) if x)
                await transcript.mount(Notice(f"turn failed: {reason}", classes="-error"))
        elif isinstance(ev, Waiting):
            await self._end_stream()
            self.waiting_for = {"approval": "waiting for your approval (y/a/n)",
                                "question": "waiting for your answer",
                                "children": "waiting for subtasks"}.get(ev.on, "")
            if ev.on == "approval":
                for call_id in ev.ids:
                    asked = self.calls.get(call_id)
                    if asked is not None:
                        asked.set_state("waiting")
                    title = tool_title(asked.call) if asked is not None else call_id
                    prompt = str((ev.detail.get(call_id) or {}).get("prompt") or "")
                    await transcript.mount(ApprovalCard(call_id, title, prompt, self.decide))
            elif ev.on == "question":
                questions = [k for k, v in ev.detail.items()
                             if isinstance(v, dict) and v.get("on") == "question"]
                for call_id in questions[:1]:  # one at a time: the next line answers it
                    self._set_question(call_id, self.task_id, ev.detail[call_id])
                    await transcript.mount(QuestionCard(ev.detail[call_id]))
            elif ev.on == "input":
                self.busy = False
                self._set_question("", "", {})
        elif isinstance(ev, TaskEnded):
            self.busy = False
            await transcript.mount(Notice(f"task {ev.status}" + (f": {ev.error}" if ev.error
                                                                 else "")))
        transcript.scroll_end(animate=False)
        self.update_status()

    def update_status(self) -> None:
        spec = models.find_spec(self.model)
        window = spec.context_window if spec else models.UNKNOWN_MODEL_CONTEXT_WINDOW
        used = f"{self.context_tokens / window:.0%}" if self.context_tokens else "-"
        cost = ""
        if spec is not None and spec.input_price_per_mtok and spec.output_price_per_mtok:
            u = self.usage
            usd = (u.uncached * spec.input_price_per_mtok + u.output * spec.output_price_per_mtok
                   + u.cache_read * (spec.cache_read_price_per_mtok or 0)
                   + u.cache_write * (spec.cache_write_price_per_mtok or 0)) / 1e6
            cost = f" · ${usd:.2f}"
        state = self.waiting_for or ("working… (Esc to stop)" if self.busy else "ready")
        width = max(self.size.width - len(self.model) - len(state) - 30, 12)
        self.query_one("#status", Static).update(RichText(
            f"{state} · {self.model} · context {used}{cost} · {_short_path(self.cwd, width)}"))

    @work(exclusive=True, group="side", exit_on_error=False)
    async def _refresh_side(self) -> None:
        while True:
            await asyncio.sleep(2)
            side = self.query_one("#side")
            if not side.display or not self.task_id:
                continue
            try:
                rows = await self.local.client.tasks(parent_id=self.task_id, limit=50)
            except Exception:
                log.exception("listing subtasks failed")
                continue
            body = "\n".join(f"{r.status:8} {r.agent} {r.task_id[-12:]}" for r in rows) \
                or "(none)"
            self.query_one("#side-body", Static).update(RichText(body))
