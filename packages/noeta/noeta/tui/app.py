"""The full-screen terminal app. It is a client like any other: it creates or
resumes a task, sends what you type, and renders what the task records."""

from __future__ import annotations

import asyncio
import contextlib
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
    ExtensionRecorded,
    InputTaken,
    ModelReplied,
    Recorded,
    Say,
    TaskEnded,
    ToolFinished,
    ToolStarted,
    Waiting,
)
from noeta.local import LocalApp
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
        self.streamed_text = False
        #: A delta is taken off the queue but not yet written.
        self._pumping = False
        self.busy = False
        self.usage = Usage()
        self.context_tokens = 0
        self.waiting_question = ""
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
        config: dict[str, Any] = {"cwd": self.cwd, "model": self.model}
        if self.permission_mode:
            config["permission_mode"] = self.permission_mode
        tid = await self.local.client.create(self.agent, None, config=config)
        await self.open_task(tid)

    async def open_task(self, task_id: str) -> None:
        if self._follower is not None:
            self._follower.cancel()
        self.task_id = task_id
        self.calls.clear()
        transcript = self.query_one("#transcript")
        await transcript.remove_children()
        await transcript.mount(Notice(f"noeta · task {task_id[:8]} · /help for commands"))
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
        if self.waiting_question:
            await self.local.client.answer(self.task_id, self.waiting_question, data=text)
            self.waiting_question = ""
            await self.query_one("#transcript").mount(UserMessage(text))
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

    async def decide(self, call_id: str, decision: str, always: bool) -> None:
        self.waiting_for = ""
        await self.local.client.answer(self.task_id, call_id, decision,  # type: ignore[arg-type]
                                       always=always)
        self.query_one(PromptInput).focus()

    # ------------------------------------------------------------ rendering

    def _on_delta(self, task_id: str, delta: Delta) -> None:
        if task_id == self.task_id and delta.kind == "text" and delta.text:
            self._deltas.put_nowait(delta)

    @work(exclusive=False, group="deltas")
    async def _pump_deltas(self) -> None:
        while True:
            delta = await self._deltas.get()
            self._pumping = True
            try:
                if self.stream is None:
                    widget = AssistantMessage("")
                    await self.query_one("#transcript").mount(widget)
                    self.query_one("#transcript").anchor()
                    self.stream = Markdown.get_stream(widget)
                self.streamed_text = True
                await self.stream.write(delta.text)
            finally:
                self._pumping = False

    async def _end_stream(self) -> None:
        if self.stream is not None:
            await self.stream.stop()
            self.stream = None

    @work(exclusive=True, group="follow")
    async def follow(self, task_id: str) -> None:
        async for rec in self.local.client.watch(task_id):
            if task_id != self.task_id:
                return
            with contextlib.suppress(Exception):
                await self.render_event(rec)
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
        elif isinstance(ev, ModelReplied):
            # Every delta written first (one taken off the queue may still be
            # mounting its widget), or the reply would be shown twice.
            while not self._deltas.empty() or self._pumping:
                await asyncio.sleep(0.01)
            await self._end_stream()
            msg = ev.response.message
            if msg.text.strip() and not self.streamed_text:
                await transcript.mount(AssistantMessage(msg.text))
            self.streamed_text = False
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
            if ev.kind == "retry":
                await transcript.mount(Notice(f"retrying (attempt {ev.data.get('attempt')}): "
                                              f"{ev.data.get('error', '')[:120]}"))
            elif ev.kind == "turn_stopped":
                await self._end_stream()
                await transcript.mount(Notice("stopped"))
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
                self.waiting_question = ev.ids[0] if ev.ids else ""
                await transcript.mount(QuestionCard(ev.detail))
            else:
                self.busy = False
                failed = ev.detail.get("turn_failed")
                if failed:
                    await transcript.mount(Notice(f"turn failed: {failed}", classes="-error"))
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

    @work(exclusive=True, group="side")
    async def _refresh_side(self) -> None:
        while True:
            await asyncio.sleep(2)
            side = self.query_one("#side")
            if not side.display or not self.task_id:
                continue
            rows = await self.local.client.tasks(parent_id=self.task_id, limit=50)
            body = "\n".join(f"{r.status:8} {r.agent} {r.task_id[-12:]}" for r in rows) \
                or "(none)"
            self.query_one("#side-body", Static).update(RichText(body))
