"""The full-screen terminal app. It is a client like any other: it creates or
resumes a task, sends what you type, and renders what the task records."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from rich.text import Text as RichText
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Markdown, Static
from textual.widgets.markdown import MarkdownStream

from noeta.cli import mentions
from noeta.cli.tui import slash
from noeta.cli.tui.transcript import TranscriptView
from noeta.cli.tui.widgets import (
    AssistantMessage,
    Notice,
    PromptInput,
    ToolCall,
    UserMessage,
)
from noeta.core.commands import list_commands, run_typed
from noeta.core.events import Waiting
from noeta.core.extension import CommandContext
from noeta.core.types import Delta, Image
from noeta.host.local import LocalApp, UnknownAgent
from noeta.host.pending import answer_choice
from noeta.host.questions import next_question, typed_answer
from noeta.toolkit.permission_modes import (
    MODE_SIGNAL,
    Modes,
    current_mode,
    next_mode,
    published_modes,
)
from noeta.toolkit.signals import RESUMED_SIGNAL

log = logging.getLogger("noeta.cli.tui")

HELP = """\
**Commands**  `/new` or `/clear` new task · `/resume [n|id]` pick a task · `/stop` stop the
turn · `/exit` quit
**Keys**  Enter send · Ctrl+J new line · Esc stop the turn (Esc Esc when idle: rewind) ·
Ctrl+B side panel · Shift+Tab permission mode · Ctrl+V paste an image · 1-9 or y/a/n approvals
**Input**  `@path` attaches a file (or directory, or image); a pasted image file path
attaches the image
"""

#: Seconds between two presses of Esc that open the rewind list.
DOUBLE_ESCAPE = 0.8
#: Seconds the mode key waits for a new task's start to record its mode.
MODE_WAIT = 2.0


class NoetaApp(TranscriptView):
    CSS_PATH = "style.tcss"
    TITLE = "noeta"
    BINDINGS = [
        Binding("escape", "stop_turn", "Stop", show=False),
        Binding("ctrl+b", "toggle_side", "Side panel", show=False),
        Binding("shift+tab", "cycle_mode", "Mode", show=False, priority=True),
        Binding("ctrl+v", "paste_image", "Paste image", show=False),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(self, local: LocalApp | Any, *, agent: str, model: str, cwd: str,
                 resume: str | None = None, permission_mode: str | None = None,
                 settings: Any = None, task_config: dict[str, Any] | None = None) -> None:
        super().__init__()
        #: What each new task's config starts with (default: ``cwd`` and ``model``).
        self.task_config = {"cwd": cwd, "model": model} if task_config is None \
            else task_config
        self.local = local
        #: The front end's settings (``/config`` shows them), when known.
        self.settings = settings
        #: Images pasted for the next message.
        self.images: list[Image] = []
        self._last_escape = 0.0
        #: The roots ``/resume`` listed last, so ``/resume <n>`` picks one.
        self.listed: list[str] = []
        self.agent = agent
        self.model = model
        self.cwd = cwd
        self.resume = resume
        self.permission_mode = permission_mode
        #: The task's permission mode, as its permissions plugin recorded it.
        self.mode = ""
        #: The modes the task's permissions plugin offers (its ``modes`` record).
        self.modes = Modes()
        #: ``modes`` came from the task's record, not the default.
        self.modes_published = False
        self.task_id = ""
        self.calls: dict[str, ToolCall] = {}
        self.stream: MarkdownStream | None = None
        self.stream_widget: AssistantMessage | None = None
        #: The model call (its ``ModelCalled`` seq) and attempt being streamed.
        self.stream_key = (0, 1)
        #: What was written to the stream so far.
        self.stream_text = ""
        #: Model calls whose text was streamed (their reply is not shown again).
        self.streamed: set[int] = set()
        #: Per model call, the attempt still live (earlier ones failed).
        self.attempts: dict[int, int] = {}
        #: The newest model call seen in the log.
        self.called = 0
        #: A delta is taken off the queue but not yet written.
        self._pumping = False
        self.busy = False
        #: What the task tree has cost so far (``host.usage``, as ``/cost``).
        self.cost_usd = 0.0
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
        yield PromptInput(self.submit, self.add_image)
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
        config = dict(self.task_config)
        if self.permission_mode:
            config["permission_mode"] = self.permission_mode
        tid = await self.local.client.create(self.agent, None, config=config)
        await self.open_task(tid, resumed=False)

    async def open_task(self, task_id: str, *, resumed: bool = True) -> None:
        """Show and follow ``task_id``; ``resumed``: an existing task the
        person reopens (its plugins are told)."""
        if self._follower is not None:
            self._follower.cancel()
        refused = ""
        try:
            await self.local.adopt(task_id)
        except UnknownAgent as exc:
            refused = str(exc)
        if resumed and not refused:
            await self.local.client.signal(task_id, RESUMED_SIGNAL)
        self.read_only = bool(refused)
        self.task_id = task_id
        self.calls.clear()
        self._set_question("", "", {})
        self.shown_requests.clear()
        self.waiting_for = ""
        self.busy = False
        self.mode, self.modes, self.modes_published = "", Modes(), False
        await self._end_stream()
        self.streamed.clear()
        self.attempts.clear()
        self.cost_usd = 0.0
        self.called = 0
        transcript = self.query_one("#transcript")
        await transcript.remove_children()
        await transcript.mount(Notice(f"noeta · task {task_id[:8]} · /help for commands"))
        for notice in getattr(self.local, "notices", ()):
            await transcript.mount(Notice(notice))
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
        self.listed = [r.task_id for r in roots[:10]]
        lines = "\n".join(f"{n}. `{r.task_id}` {r.status}" for n, r in enumerate(roots[:10], 1))
        await self.say_markdown(f"Resumed the latest task. Others: `/resume <n>` or "
                                f"`/resume <id>`\n{lines}")

    # ------------------------------------------------------------ input

    async def submit(self, text: str) -> None:
        if text.startswith("/"):
            await self.command(text)
            return
        await self.send(text)

    async def send(self, text: str) -> None:
        if not self.task_id:
            return
        if self.read_only:
            await self.say_notice("this task is read-only here (its agent is not available)")
            return
        if self.waiting_question:
            await self.answer_question(text)
            return
        images, self.images = self.images, []
        message = mentions.attach(text, self.cwd, images) \
            if images or mentions.mentioned(text, self.cwd) else text
        # Busy before the send: the turn may run, and its end be shown,
        # before the send returns.
        self.busy = True
        self.update_status()
        try:
            await self.local.client.send(self.task_id, message, when="next_turn")
        except BaseException:
            self.busy = False
            self.update_status()
            raise

    async def command(self, text: str) -> None:
        name, _, arg = text[1:].partition(" ")
        if name in ("exit", "quit"):
            self.exit()
        elif name == "new":
            await self.new_task()
        elif name == "resume":
            await self.resume_command(arg.strip())
        elif name == "stop":
            await self.action_stop_turn()
        elif name == "help":
            builtin = "\n".join(f"- `/{n}` {h}" for n, h in slash.COMMANDS.items())
            await self.query_one("#transcript").mount(
                Markdown(HELP + builtin + "\n" + await self._plugin_help(), classes="notice"))
        elif not await slash.run(self, name, arg):
            await self._plugin_command(text, name)

    async def resume_command(self, arg: str) -> None:
        if arg.isdigit() and 1 <= int(arg) <= len(self.listed):
            await self.open_task(self.listed[int(arg) - 1])
        elif arg:
            await self.open_task(arg)
        else:
            await self.pick_task()

    async def say_markdown(self, text: str) -> None:
        transcript = self.query_one("#transcript")
        await transcript.mount(Markdown(text, classes="notice"))
        transcript.scroll_end(animate=False)

    def prefill(self, text: str) -> None:
        """Put ``text`` in the input box for the person to edit and send."""
        box = self.query_one(PromptInput)
        box.text = text
        box.move_cursor(box.document.end)
        box.focus()

    async def add_image(self, image: Image, label: str = "") -> None:
        self.images.append(image)
        box = self.query_one(PromptInput)
        box.insert(f"[Image #{len(self.images)}]")
        await self.say_notice(f"image attached{f' ({label})' if label else ''}; it goes with "
                              "the next message")

    async def action_paste_image(self) -> None:
        image = mentions.clipboard_image()
        if image is None:
            await self.say_notice("no image on the clipboard (needs wl-paste, xclip or "
                                  "pngpaste)")
            return
        await self.add_image(image, "clipboard")

    def _agent(self) -> Any:
        """This front end's agent, when it runs one (not attached to a server)."""
        return getattr(getattr(self.local, "runtime", None), "agents", {}).get(self.agent)

    def _command_context(self) -> CommandContext:
        return CommandContext(cwd=self.cwd, task_id=self.task_id or None)

    async def _plugin_help(self) -> str:
        agent = self._agent()
        commands = await list_commands(agent, self._command_context()) if agent else []
        lines = [f"`/{c.name}`" + (f" {c.argument_hint}" if c.argument_hint else "")
                 + (f" — {c.description}" if c.description else "") for c in commands]
        return "**From plugins**\n" + "\n".join(f"- {line}" for line in lines) \
            if lines else ""

    async def _plugin_command(self, text: str, name: str) -> None:
        agent = self._agent()
        try:
            typed = await run_typed(agent.all_extensions(), self._command_context(), text) \
                if agent else None
        except Exception as exc:  # noqa: BLE001 — a plugin's failure is shown, not raised
            await self.say_notice(f"/{name} failed: {exc}")
            return
        if typed is None:
            await self.say_notice(f"unknown command /{name} — try /help")
        elif typed.local:
            await self.say_markdown(typed.text or f"/{name} done")
        elif typed.text:
            await self.send(typed.text)

    async def action_stop_turn(self) -> None:
        if self.task_id and self.busy:
            await self.local.client.cancel(self.task_id, "stopped from the terminal",
                                           turn_only=True)
            return
        now = time.monotonic()
        if now - self._last_escape <= DOUBLE_ESCAPE and self.task_id and not self.read_only:
            self._last_escape = 0.0
            await slash.run(self, "rewind", "")
        else:
            self._last_escape = now

    def action_toggle_side(self) -> None:
        side = self.query_one("#side")
        side.display = not side.display

    async def decide(self, call_id: str, choice: str, *, task_id: str = "") -> None:
        self.waiting_for = ""
        await answer_choice(self.local.client, task_id or self.task_id, call_id, choice)
        self.query_one(PromptInput).focus()

    async def action_cycle_mode(self) -> None:
        """Step to the next permission mode (Claude Code's shift+tab)."""
        if not self.task_id or self.read_only:
            return
        if not self.mode or not self.modes_published:
            await self._modes_from_log()
        current = self.mode or self.permission_mode
        if not current:
            await self.say_notice("the task has not recorded a permission mode yet")
            return
        mode = next_mode(current, self.modes.cycle)
        await self.local.client.signal(self.task_id, MODE_SIGNAL, {"mode": mode})
        self.mode = mode
        self.update_status()

    async def _modes_from_log(self) -> None:
        """Pressed before the follower showed the permissions plugin's
        records: take them from the log, waiting (briefly) for the task's
        start to record them — stepping from a guess could land in
        ``bypassPermissions``."""
        deadline = time.monotonic() + MODE_WAIT
        while True:
            events = await self.local.client.events(self.task_id)
            started = any(isinstance(r.event, Waiting) for r in events)
            mode = current_mode(events)
            if mode or started or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.05)
        self.mode = mode or self.mode
        self._take_modes(published_modes(events))

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
