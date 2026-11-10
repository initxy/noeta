"""The full-screen terminal app. It is a client like any other: it creates or
resumes a task, sends what you type, and renders what the task records."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from typing import Any

from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static, TextArea

from noeta.core.events import InputTaken, Say, Waiting
from noeta.core.extension import Command, CommandContext
from noeta.core.loop.state import off_names
from noeta.core.types import Delta, Image
from noeta.host.app import FrontEndStart
from noeta.host.local import UnknownAgent
from noeta.host.pending import answer_choice
from noeta.host.questions import next_question, typed_answer
from noeta.host.rewind import RewindPoint
from noeta.host.stream_json import find_latest_task
from noeta.toolkit.interactive import BASH_MODE_KEY, follow
from noeta.toolkit.local_env import local_timezone
from noeta.toolkit.permission_modes import (
    MODE_SIGNAL,
    Modes,
    current_mode,
    next_mode,
    published_modes,
)
from noeta.toolkit.signals import RESUMED_SIGNAL
from noeta_tui import bash_mode, clipboard, files, history, mentions, render, selection, slash, wrap
from noeta_tui.agents import MAIN, AgentsPanel, AgentView, WorkflowView
from noeta_tui.tail import TailScroll
from noeta_tui.transcript import SERVER_DIRECTORY, WORKFLOW_KEY, TranscriptView, _short_path
from noeta_tui.widgets import (
    CleanMarkdown,
    Completions,
    Notice,
    PromptInput,
    QuestionCard,
    ToolCall,
    UserMessage,
    Welcome,
)

log = logging.getLogger("noeta_tui")

wrap.install()

SHORTCUTS = """\
[dim]! for bash mode        double tap esc to clear input     ctrl + c twice to exit
/ for commands         shift + tab to cycle modes        ctrl + v to paste images
@ for file paths       ctrl + o for verbose output       ctrl + b for agents
ctrl + r for history   ctrl + t to toggle tasks          shift + ⏎ for newline
drag to copy           ↑ ↓ for earlier prompts           esc to interrupt
click to expand        /copy for last reply[/dim]"""

#: Seconds between two presses of Esc (or Ctrl+C) that count as a double press.
DOUBLE_PRESS = 0.8
#: Seconds the footer says what was copied.
COPIED_FLASH = 2.0
#: Seconds the mode key waits for a new task's start to record its mode.
MODE_WAIT = 2.0
#: The tasks ``/resume`` offers: the most recently updated roots.
RESUME_ROWS = 10


#: What ``-c`` says, before it exits, when nothing was started here.
NOTHING_TO_CONTINUE = "No conversation found to continue"


class NoetaApp(TranscriptView):
    CSS_PATH = "style.tcss"
    TITLE = "noeta"
    BINDINGS = [
        Binding("escape", "stop_turn", "Stop", show=False),
        Binding("ctrl+b", "agents", "Agents", show=False),
        Binding("shift+tab", "cycle_mode", "Mode", show=False, priority=True),
        Binding("ctrl+v", "paste_image", "Paste image", show=False),
        Binding("ctrl+o", "detailed_transcript", "Transcript", show=False, priority=True),
        Binding("ctrl+t", "toggle_tasks", "Tasks", show=False, priority=True),
        Binding("ctrl+c", "interrupt", "Quit", show=False, priority=True),
        Binding("ctrl+r", "search_history", "History", show=False, priority=True),
        # The transcript's, whatever has the focus (the prompt keeps it).
        Binding("pageup", "page(-1)", show=False, priority=True),
        Binding("pagedown", "page(1)", show=False, priority=True),
    ]

    def __init__(self, local: Any, *, agent: str, model: str, cwd: str,
                 resume: str | None = None, permission_mode: str | None = None,
                 task_config: dict[str, Any] | None = None, on_server: bool = False,
                 start: FrontEndStart | None = None) -> None:
        """``local``: the ``AppAPI`` the app runs on. ``on_server``: it is a
        server's, so its tasks run in the server's directories, not ``cwd``
        on this machine. ``start``: what the ``noeta`` command said (the
        home directory, the settings shown, the version)."""
        # ANSI colours, as the terminal draws them: its background and
        # text colour show through.
        super().__init__(ansi_color=True)
        self.theme = "ansi-light" if light_terminal() else "ansi-dark"
        self.on_server = on_server
        #: What each new task's config starts with (default: ``cwd`` and ``model``).
        self.task_config = {"cwd": cwd, "model": model} if task_config is None \
            else task_config
        self.local = local
        self.start = start or FrontEndStart(agent=agent, cwd=cwd, model=model)
        #: Images pasted for the next message.
        self.images: list[Image] = []
        #: Bash-mode output that goes with the next message.
        self.bash_context: list[str] = []
        self._last_escape = 0.0
        self._last_interrupt = 0.0
        #: The roots ``/resume`` listed last, so ``/resume <n>`` picks one.
        self.listed: list[str] = []
        self.agent = agent
        self.model = model
        self.cwd = cwd
        self.task_cwd = self._default_task_cwd()
        self.resume = resume
        self.permission_mode = permission_mode
        #: The effort the task was given (its config's), ``None``: the model's default.
        self.effort: str | None = None
        #: The task's permission mode, as its permissions plugin recorded it.
        self.mode = ""
        #: The modes the task's permissions plugin offers (its ``modes`` record).
        self.modes = Modes()
        #: ``modes`` came from the task's record, not the default.
        self.modes_published = False
        self.task_id = ""
        self.calls: dict[str, ToolCall] = {}
        self.stream = None
        self.stream_widget = None
        self.thinking = None
        self.thinking_key = (0, 1)
        self.thinking_now = False
        self.agents = {}
        self.workflows = {}
        self.subagents = {}
        self.workflow_calls = {}
        self.listed_agents = []
        self.agents_since = 0.0
        self.early_progress = {}
        #: The names of the images pasted for the next message.
        self.image_names: list[str] = []
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
        self.replied = 0
        self._batch = None
        self._drawn = {}
        self._stopped_here = False
        #: The rewind the screen's task will continue, made on the first
        #: message: the task rewound and the point (``None``: no rewind).
        self.fork_from: tuple[str, RewindPoint] | None = None
        self._record_at = 0.0
        self.turn_started_at = 0.0
        #: A delta is taken off the queue but not yet written.
        self._pumping = False
        self.busy = False
        #: What the task tree has cost so far (``host.usage``, as ``/cost``).
        self.cost_usd = 0.0
        self.context_tokens = 0
        self.compacts_at = None
        #: The question the next input line answers: its call id, the task
        #: asking (the root or a sub-agent), its detail, and answers so far.
        self.waiting_question = ""
        self.question_task = ""
        self.question_detail: dict[str, Any] = {}
        self.question_answers: dict[str, Any] = {}
        #: Sub-agent requests already shown, by (task id, request id).
        self.shown_requests: set[tuple[str, str]] = set()
        self.open_cards = {}
        #: The task's agent is not one this app has: history only, no input.
        self.read_only = False
        self.waiting_for = ""
        self.todos: dict[str, dict[str, Any]] = {}
        self.show_tasks = True
        self.queued: list[str] = []
        self.echoed: list[str] = []
        self.turn_started = 0.0
        self.turn_tokens = 0
        self.turn_output = 0
        self.turn_verb = ""
        self.flash = ""
        self._flash_timer: Timer | None = None
        #: The screen's selection copied last (copied once on release).
        self._copied: dict[Any, Any] | None = None
        #: Prompts sent in this project, oldest first (earlier sessions'
        #: too); ``history_at`` steps through them.
        self._history_file = history.history_file(self.start.home, self.start.cwd)
        self.history: list[str] = history.load(self._history_file)
        self.history_at = len(self.history)
        #: The commands the slash menu offers: plugins' for this task.
        self.plugin_commands: list[Command] = []
        self._deltas: asyncio.Queue[tuple[str, Delta]] = asyncio.Queue()
        self._unshown: Counter[int] = Counter()
        self._follower: Any = None
        #: Held while a new screen's task is created (one task, however many ask).
        self._creating = asyncio.Lock()
        #: Ctrl+R: the prompt filters the history menu.
        self.searching = False
        self.plugins_off = off_names(self.task_config)
        self.ui_notices = {}
        self.ui_widgets = {}
        self._ui_wanted = asyncio.Event()

    # ------------------------------------------------------------ layout

    #: The error that ended the app; the ``noeta`` command reports it once
    #: the terminal is restored (one line and a crash report file).
    crashed: Exception | None = None

    def _handle_exception(self, error: Exception) -> None:
        if self.crashed is None:
            self.crashed = error
        super()._handle_exception(error)

    def _fatal_error(self) -> None:
        # Textual's own traceback shows every frame's locals; the noeta
        # command prints one line instead and keeps the traceback in a file.
        self.bell()
        self._close_messages_no_wait()

    def compose(self) -> ComposeResult:
        yield TailScroll(id="transcript")
        yield Static("", id="spinner")
        yield Static("", id="tasks")
        yield Static("", id="queued")
        yield Vertical(id="overlay")
        yield Static("", id="attachments")
        yield Vertical(id="widgets-above", classes="plugin-widgets")
        with Horizontal(id="prompt-box"):
            yield Static("❯", id="marker")
            yield PromptInput(self.submit, self.add_image, self.prompt_key)
        yield Completions()
        yield Static(SHORTCUTS, id="shortcuts")
        yield Vertical(id="widgets-below", classes="plugin-widgets")
        with Horizontal(id="footer"):
            yield Static("", id="hints")
            yield Static("", id="status")
        yield Static("", id="plugin-status")
        yield AgentsPanel(self.open_agent, self._leave_agents)

    async def on_mount(self) -> None:
        for hidden in ("#agents", "#spinner", "#tasks", "#queued", "#completions",
                       "#shortcuts", "#attachments", ".plugin-widgets", "#plugin-status"):
            self.query_one(hidden).display = False
        await self.local.start()
        self.local.deltas(self._on_delta)
        self._pump_deltas()
        self._fetch_ui()
        self.set_interval(0.12, self._tick)
        if self.start.trust is not None:
            await slash.trust_picker(self, self._open_first)
        else:
            await self._open_first()

    async def _open_first(self) -> None:
        """The task the app starts on: ``--resume``'s, the picked one, the
        newest one started here (``-c``; with none, Claude Code's message and
        exit code 1), or a new one; the command line's prompt is then sent
        as if typed (left in the input while the person picks a task)."""
        prompt = self.start.prompt
        if self.resume:
            await self.open_task(self.resume)
        elif self.resume == "":
            await self.pick_task()
        elif self.start.continue_last and (latest := await find_latest_task(
                self.local.client, str(self.task_config.get("cwd") or self.cwd), self.agent)):
            await self.open_task(latest)
        elif self.start.continue_last:
            self.exit(return_code=1, message=NOTHING_TO_CONTINUE)
            return
        else:
            await self.new_task()
        if self.start.warnings is not None:
            loop = asyncio.get_running_loop()

            def show(line: str, level: int) -> None:  # from whichever thread logs it
                loop.call_soon_threadsafe(self.call_later, self._say_warning, line, level)

            self.start.warnings.show_with(show, shown=list(getattr(self.local, "notices", ())))
        if not self.open_cards:  # a reopened task's open approval keeps the keys
            self.query_one(PromptInput).focus()
        if prompt and self.resume == "":
            self.query_one(PromptInput).text = prompt
        elif prompt:
            await self.submit(prompt)

    def on_resize(self) -> None:
        self.call_after_refresh(self.update_status)  # what fits in the footer changed

    async def on_unmount(self) -> None:
        await self.local.stop()

    # ------------------------------------------------------------ tasks

    async def new_task(self) -> None:
        """An empty screen for a new task, created when first needed
        (:meth:`ensure_task`): a start, ``/clear`` or ``/new`` the person
        types nothing after leaves no empty task behind, as Claude Code saves
        a session only from its first message."""
        await self._clear("")
        self._load_commands()
        self.update_status()

    async def ensure_task(self) -> str:
        """The task shown, created now if the screen is a new one."""
        async with self._creating:
            if self.task_id:
                return self.task_id
            config = dict(self.task_config)
            if self.permission_mode:
                config["permission_mode"] = self.permission_mode
            # The person is at this machine: their dates are in its time zone.
            if "timezone" not in config and (zone := local_timezone()):
                config["timezone"] = zone
            # It runs the person's ``!`` commands (not on a server, where the
            # task's files are): the prompt may suggest one.
            if not self.on_server:
                config[BASH_MODE_KEY] = True
            if self.fork_from is not None:
                # A rewind's conversation: made now, from its first message,
                # so a rewind nobody goes on with leaves no empty task.
                source, point = self.fork_from
                done = await self.local.rewind(source, point, conversation=True, files=False)
                tid: str = done.task_id
                self.fork_from = None
            else:
                tid = str(await self.local.client.create(self.agent, None, config=config))
            await self.local.adopt(tid)
            self.task_id = tid
            self._follower = self.follow(tid)
            self._load_commands()
            return tid

    async def open_task(self, task_id: str, *, resumed: bool = True) -> None:
        """Show and follow the existing ``task_id``; ``resumed``: the person
        reopens it (its plugins are told), not a fork rewind just made."""
        refused = ""
        try:
            await self.local.adopt(task_id)
        except UnknownAgent as exc:
            refused = str(exc)
        if resumed and not refused:
            # A person follows it now, whatever headless run had it before.
            await follow(self.local.client, task_id)
            await self.local.client.signal(task_id, RESUMED_SIGNAL)
        head = await self._clear(task_id, refused=refused, mount=False)
        after = await self.show_log(task_id, head)
        if task_id != self.task_id:  # another task was opened meanwhile
            return
        self._follower = self.follow(task_id, after)
        self._load_commands()
        self.update_status()

    async def open_fork(self, source: str, point: RewindPoint) -> None:
        """A rewind of ``source``'s conversation to before ``point``: its
        conversation up to there shown, on a screen whose task (that
        conversation's continuation) is made when the first message is
        sent (:meth:`ensure_task`)."""
        head = await self._clear("", mount=False)
        self.fork_from = (source, point)
        await self.show_log(point.task_id, head, through=point.seq - 1)
        self._load_commands()
        self.update_status()

    async def _clear(self, task_id: str, *, refused: str = "",
                     mount: bool = True) -> list[Widget]:
        """Forget what was shown and show ``task_id``'s welcome box (``""``:
        a new task's); ``mount=False``: the box is returned instead, to show
        with the task's log."""
        if self._follower is not None:
            self._follower.cancel()
            self._follower = None
        self.read_only = bool(refused)
        self.task_id = task_id
        self.fork_from = None
        self.task_cwd = self._default_task_cwd()
        self.effort = self.task_config.get("effort")
        self.context_tokens = 0
        self.compacts_at = None
        self.calls.clear()
        self.agents.clear()
        self.workflows.clear()
        self.subagents.clear()
        self.workflow_calls.clear()
        self.listed_agents.clear()
        self.agents_since = time.time()  # a resumed task's ended agents are not listed
        self.early_progress.clear()
        self._set_question("", "", {})
        self.shown_requests.clear()
        self.open_cards.clear()
        self.waiting_for = ""
        self.busy = False
        self.mode, self.modes, self.modes_published = "", Modes(), False
        self.plugins_off = off_names(self.task_config)
        await self.clear_ui()
        await self._end_stream()
        self.streamed.clear()
        self.attempts.clear()
        self.cost_usd = 0.0
        self.called = self.replied = 0
        self.todos.clear()
        self._show_todos()
        self.queued.clear()
        self.echoed.clear()
        self._show_queued()
        transcript = self.transcript()
        await transcript.clear()
        head: list[Widget] = [Welcome(version=self.start.version, model=self.model,
                                      cwd=_short_path(self.task_cwd, 72) or SERVER_DIRECTORY,
                                      task_id=task_id)]
        head += [Notice(notice) for notice in getattr(self.local, "notices", ())]
        if refused:
            head.append(Notice(f"read-only: {refused}. Its history is shown; "
                               "/new starts a task here.", classes="-error"))
        if not mount:
            return head
        await transcript.mount_all(head)
        return []

    def _default_task_cwd(self) -> str:
        """The task's directory until its ``TaskCreated`` says: the one new
        tasks are asked to run in (on a server, ``""`` for its own)."""
        return str(self.task_config.get("cwd") or ("" if self.on_server else self.cwd))

    async def pick_task(self) -> None:
        """Open the latest task and offer the others (``/resume``'s picker)."""
        roots = await self.local.client.tasks(roots_only=True, limit=RESUME_ROWS)
        if not roots:
            await self.new_task()
            return
        await self.open_task(roots[0].task_id)
        self.listed = [r.task_id for r in roots]
        await slash.resume_picker(self, roots)

    @work(exclusive=True, group="commands", exit_on_error=False)
    async def _load_commands(self) -> None:
        """The plugins' commands for the slash menu."""
        try:
            self.plugin_commands = list(await self.local.commands(
                self.agent, self._command_context()))
        except Exception:
            log.warning("listing the plugins' commands failed", exc_info=True)
            self.plugin_commands = []

    # ------------------------------------------------------------ input

    async def submit(self, text: str) -> None:
        self.query_one("#shortcuts").display = False
        self.query_one(Completions).hide()
        if not self.history or self.history[-1] != text:
            self.history.append(text)
            history.append(self._history_file, text)
        self.history_at = len(self.history)
        self._set_marker("")
        if text.startswith("/"):
            async with self.failures_shown(text.split()[0]):
                await self.command(text)
            return
        if text.startswith("!") and not self.waiting_question:
            async with self.failures_shown("the command"):
                await bash_mode.run(self, text[1:].strip())
            return
        async with self.failures_shown("sending it"):
            await self.send(text)

    async def send(self, text: str) -> None:
        if self.read_only:
            await self.say_notice("this task is read-only here (its agent is not available)")
            return
        if self.waiting_question:
            await self.answer_question(text)
            return
        images, self.images = self.images, []
        self.agents_since = time.time()  # what ended before this leaves the agents' list
        self.image_names = []
        self._show_attachments()
        context, self.bash_context = self.bash_context, []
        body = "\n\n".join([*context, text]) if context else text
        message = mentions.attach(body, images) if images else body
        if self.busy:
            self.queued.append(body)
            self._show_queued()
        else:  # shown at once, as typed; the task's log confirms it
            self.echoed.append(body)
            transcript = self.transcript()
            await transcript.mount(UserMessage(text))
            transcript.stick()
        # Busy before the send: the turn may run, and its end be shown,
        # before the send returns.
        was_busy = self.busy
        self.set_busy(True)
        try:
            task_id = await self.ensure_task()
            await self.local.client.send(task_id, message, when="next_turn")
        except BaseException:
            if not was_busy:
                self.set_busy(False)
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
        elif not await slash.run(self, name, arg):
            await self._plugin_command(text, name)

    async def resume_command(self, arg: str) -> None:
        if arg.isdigit() and 1 <= int(arg) <= len(self.listed):
            await self.open_task(self.listed[int(arg) - 1])
        elif arg:
            await self.open_task(arg)
        else:
            roots = await self.local.client.tasks(roots_only=True, limit=RESUME_ROWS)
            self.listed = [r.task_id for r in roots]
            await slash.resume_picker(self, roots)

    async def _say_warning(self, line: str, level: int) -> None:
        """A warning noeta logged (its stderr is under this screen)."""
        transcript = self.transcript()
        await transcript.mount(Notice(line, classes="-error" if level >= logging.ERROR
                                      else "-warn"))
        transcript.stick()

    async def say_markdown(self, text: str) -> None:
        transcript = self.transcript()
        await transcript.mount(CleanMarkdown(text, classes="notice"))
        transcript.stick()

    async def say_text(self, text: Text) -> None:
        """Rich text, as a command's output (``  ⎿`` under the command)."""
        transcript = self.transcript()
        await transcript.mount(Static(text, classes="command-output"))
        transcript.stick()

    def prefill(self, text: str) -> None:
        """Put ``text`` in the input box for the person to edit and send."""
        box = self.query_one(PromptInput)
        box.text = text
        box.move_cursor(box.document.end)
        box.focus()

    async def add_image(self, image: Image, label: str = "") -> None:
        self.images.append(image)
        self.image_names.append(label or "image")
        box = self.query_one(PromptInput)
        box.insert(f"[Image #{len(self.images)}] ")
        self._show_attachments()

    def _show_attachments(self) -> None:
        """The images going with the next message, over the prompt."""
        line = Text()
        for n, name in enumerate(self.image_names, 1):
            line.append(f" ▣ Image #{n} ", style=f"bold {render.ACCENT}")
            line.append(f"{name}   ", style="dim")
        if self.image_names:
            line.append("(they go with the next message)", style="dim")
        box = self.query_one("#attachments", Static)
        box.update(line)
        box.display = bool(self.image_names)

    async def action_paste_image(self) -> None:
        # Its programs may take seconds (a clipboard owner that is slow to answer).
        image = await asyncio.to_thread(mentions.clipboard_image)
        if image is None:
            await self.say_notice("no image on the clipboard (on Linux it needs wl-paste or "
                                  "xclip; over ssh, drop the image file on the terminal "
                                  "instead)")
            return
        await self.add_image(image, "clipboard")

    def _command_context(self) -> CommandContext:
        return CommandContext(cwd=self.cwd, task_id=self.task_id or None)

    async def _plugin_command(self, text: str, name: str) -> None:
        try:
            typed = await self.local.run_command(self.agent, self._command_context(), text)
        except Exception as exc:  # noqa: BLE001 — a plugin's failure is shown, not raised
            await self.say_notice(f"/{name} failed: {exc}")
            return
        if typed is None:
            await self.say_notice(f"unknown command /{name} — try /help")
        elif typed.local:
            await self.say_markdown(typed.text or f"/{name} done")
        elif typed.text:
            await self.send(typed.text)

    # ------------------------------------------------------------ the prompt's keys

    def _box(self) -> PromptInput:
        return self.query_one("#prompt", PromptInput)

    def _to_prompt(self) -> PromptInput | None:
        """The prompt, focused, when what has focus takes no typing (the
        transcript after a click, or nothing): what is typed or pasted then
        goes to the prompt, as in Claude Code. A card's choices, the agents
        panel and an input keep their keys."""
        if self.focused is not None and not isinstance(self.focused, VerticalScroll):
            return None
        box = self._box()
        if not box.display or box.disabled:
            return None
        box.focus()
        return box

    def on_key(self, event: events.Key) -> None:
        if event.is_printable and (box := self._to_prompt()) is not None:
            event.stop()
            box._forward_event(events.Key(event.key, event.character))

    def on_paste(self, event: events.Paste) -> None:
        # Forwarded, as the app forwards a terminal's paste: one that is not
        # comes back to the focused widget once more.
        if (box := self._to_prompt()) is not None:
            event.stop()
            box._forward_event(events.Paste(event.text))

    async def prompt_key(self, key: str) -> bool:
        """The prompt's key, first: the completion menu's, history, ``?``."""
        menu = self.query_one(Completions)
        box = self._box()
        if self.searching and key in ("enter", "tab", "escape"):
            picked = menu.current() if key != "escape" else None
            self._end_search(picked if picked is not None else
                             ("" if key == "escape" else box.text))
            return True
        if self.searching and key in ("up", "down"):
            menu.move(-1 if key == "up" else 1)
            return True
        if menu.display:
            if key in ("up", "down"):
                menu.move(-1 if key == "up" else 1)
                return True
            if key in ("tab", "enter"):
                picked = menu.current()
                menu.hide()
                if picked is not None:
                    self._complete(picked)
                    if key == "enter" and picked.startswith("/"):
                        text = box.text.strip()
                        box.clear()
                        await self.submit(text)
                return True
            if key == "escape":
                menu.hide()
                return True
        if key == "question_mark" and not box.text:
            shortcuts = self.query_one("#shortcuts")
            shortcuts.display = not shortcuts.display
            self._sync_footer()
            return True
        panel = self.query_one(AgentsPanel)
        if key == "down" and not box.text and panel.agents:
            panel.focus()  # Claude Code: ↓ from an empty prompt to the agents
            return True
        if key in ("up", "down") and self.history:
            row = box.cursor_location[0]
            last = box.document.line_count - 1
            if (key == "up" and row == 0) or (key == "down" and row == last):
                self._step_history(-1 if key == "up" else 1)
                return True
        return False

    def action_search_history(self) -> None:
        """Ctrl+R: what is typed filters the earlier prompts, newest first;
        Enter puts the picked one in the box, Esc leaves it empty."""
        if not self.history:
            return
        self.searching = True
        self._set_marker("⌕")
        self._show_search(self._box().text)

    def _show_search(self, typed: str) -> None:
        want = typed.lower()
        found = [h for h in reversed(self.history) if want in h.lower()]
        self.query_one(Completions).show([(h, h.splitlines()[0][:100], "") for h in found])

    def _end_search(self, text: str) -> None:
        self.searching = False
        self.query_one(Completions).hide()
        self._set_marker("")
        box = self._box()
        box.text = text
        box.move_cursor(box.document.end)

    def _step_history(self, step: int) -> None:
        self.history_at = max(0, min(len(self.history), self.history_at + step))
        text = self.history[self.history_at] if self.history_at < len(self.history) else ""
        box = self._box()
        box.text = text
        box.move_cursor(box.document.end)

    def _word(self) -> tuple[int, int]:
        """Where the word the cursor is in starts and ends in the box's text."""
        box = self._box()
        text = box.text
        row, column = box.cursor_location
        at = sum(len(line) + 1 for line in text.split("\n")[:row]) + column
        start = max(text.rfind(" ", 0, at), text.rfind("\n", 0, at)) + 1
        ends = [i for i in (text.find(" ", at), text.find("\n", at)) if i != -1]
        return start, min(ends, default=len(text))

    def _complete(self, value: str) -> None:
        """Replace the word the cursor is in with ``value`` (what follows it
        stays), the cursor after it."""
        box = self._box()
        text = box.text
        start, end = self._word()
        rest = text[end:]
        box.text = text[:start] + value + ("" if rest.startswith(" ") else " ") + rest
        at = start + len(value) + 1
        box.move_cursor((box.text.count("\n", 0, at), at - box.text.rfind("\n", 0, at) - 1))

    def _sync_footer(self) -> None:
        """The menu and the shortcuts take the footer's place (and the
        plugins' line and widgets under the prompt)."""
        covered = self.query_one(Completions).display or self.query_one("#shortcuts").display
        self.query_one("#footer").display = not covered
        for under in ("#widgets-below", "#plugin-status", "#widgets-above"):
            box = self.query_one(under)
            box.display = box.has_class("-shown") and (under == "#widgets-above" or not covered)

    @on(TextArea.Changed, "#prompt")
    def _typed(self, event: TextArea.Changed) -> None:
        text = event.text_area.text
        self.query_one("#shortcuts").display = False
        self._sync_footer()
        if self.searching:
            self._show_search(text)
            return
        self._set_marker("!" if text.startswith("!") else "")
        menu = self.query_one(Completions)
        start, end = self._word()
        word = text[start:end]
        if start == 0 and word.startswith("/"):  # the command word, cursor anywhere in it
            menu.show(slash.matches(self, word[1:]))
        elif word.startswith("@") and not self.on_server:
            self._complete_path(word)
        else:
            menu.hide()

    @work(exclusive=True, group="paths", exit_on_error=False)
    async def _complete_path(self, word: str) -> None:
        """The ``@`` menu for ``word``, the tree listed off the event loop
        (a large one takes a while), shown if the word is still being typed."""
        found = await asyncio.to_thread(files.matches, self.task_cwd or self.cwd, word[1:])
        start, end = self._word()
        if self._box().text[start:end] == word:
            self.query_one(Completions).show([(f"@{p}", f"+ {p}", "") for p in found])

    def _set_marker(self, marker: str) -> None:
        bash = marker == "!"
        self.query_one("#marker", Static).update(
            Text(marker or "❯", style=f"bold {render.BASH}" if bash else ""), layout=False)
        self.query_one("#prompt-box").set_class(bash, "-bash")

    # ------------------------------------------------------------ keys

    def action_page(self, step: int) -> None:
        """PageUp / PageDown: a page of what is shown (the transcript, or
        the view in its place)."""
        views = self.query(".agent-log")
        target = views.first() if views else self.transcript()
        if step < 0:
            target.scroll_page_up(animate=False)
        else:
            target.scroll_page_down(animate=False)

    async def action_stop_turn(self) -> None:
        if self.task_id and self.busy:
            # Shown stopped at once; the task's record of it follows, and
            # the keys go on meanwhile (a server's answer takes a while).
            await self.interrupted()
            self._cancel_turn(self.task_id)
            return
        now = time.monotonic()
        double = now - self._last_escape <= DOUBLE_PRESS
        self._last_escape = 0.0 if double else now
        if not double:
            return
        box = self._box()
        if box.text:
            box.clear()  # Claude Code: double Esc clears the input
        elif self.task_id and not self.read_only:
            await slash.run(self, "rewind", "")

    @work(group="stop", exit_on_error=False)
    async def _cancel_turn(self, task_id: str) -> None:
        async with self.failures_shown("stopping"):
            await self.local.client.cancel(task_id, "stopped from the terminal", turn_only=True)

    async def action_interrupt(self) -> None:
        """Ctrl+C: copy the selection (it is then gone), else clear the
        input; on an empty one, twice to quit."""
        if self.copy_selection():
            self.screen.clear_selection()
            return
        box = self._box()
        if box.text:
            box.clear()
            return
        now = time.monotonic()
        if now - self._last_interrupt <= DOUBLE_PRESS:
            self.exit()
            return
        self._last_interrupt = now
        self.show_flash("Press Ctrl-C again to exit", DOUBLE_PRESS)

    def on_text_selected(self, event: events.TextSelected) -> None:
        """The mouse button was released: a selection it made is copied (a
        click selects nothing; one on the scrollbar keeps the selection)."""
        self._copy_new_selection()

    def on_click(self, event: events.Click) -> None:
        if event.chain > 1:  # a double click selected its block, a triple what is around it
            self._copy_new_selection()

    def _copy_new_selection(self) -> None:
        if self.screen.selections is not self._copied:
            self.copy_selection()

    def copy_selection(self) -> bool:
        """The screen's selection to the clipboard, the footer saying so;
        ``False`` when nothing is selected."""
        self._copied = self.screen.selections
        text = selection.selected_text(self.screen)
        if not text:
            return False
        clipboard.copy(self, text)
        self.show_flash(f"Copied {len(text)} character{'' if len(text) == 1 else 's'}",
                        COPIED_FLASH)
        return True

    def show_flash(self, text: str, seconds: float) -> None:
        """``text`` on the footer's left for ``seconds``."""
        if self._flash_timer is not None:
            self._flash_timer.stop()
        self.flash = text
        self.update_status()
        self._flash_timer = self.set_timer(seconds, self._clear_flash)

    def _clear_flash(self) -> None:
        self.flash = ""
        self.update_status()

    def action_agents(self) -> None:
        """Ctrl+B: to the agents' list (back to the prompt when there)."""
        panel = self.query_one(AgentsPanel)
        if panel.has_focus:
            self._leave_agents()
        elif panel.agents:
            panel.focus()

    def _leave_agents(self) -> None:
        self._box().focus()

    async def open_agent(self, key: str) -> None:
        """Show what the picked agent did in the transcript's place (main:
        the transcript again); a workflow's line lists its agents first."""
        if key == MAIN:
            await self.close_agent()
            return
        view: AgentView | WorkflowView
        if key.startswith(WORKFLOW_KEY):
            call = self.workflow_calls.get(key[len(WORKFLOW_KEY):])
            if call is None or call.workflow is None:
                return
            run = call.workflow
            rows = [self._child_row(str(a["child_id"]), str(a.get("label") or a["key"]))
                    for a in run["agents"].values() if a.get("child_id") in self.subagents]
            view = WorkflowView(str(run.get("description") or call.title), rows,
                                on_pick=self.open_agent, on_back=self.close_agent)
        else:
            c = self.subagents.get(key)
            if c is None:
                return
            view = AgentView(key, c["name"] or "agent", kind=c.get("kind", ""), child=c,
                             on_back=self.close_agent)
        await self.query(".agent-view").remove()
        self.query_one("#transcript").display = False
        await self.mount(view, after="#transcript")
        self.query_one(AgentsPanel).viewing = key
        self._show_agents()

    async def close_agent(self) -> None:
        """Back to the transcript from an agent's view."""
        await self.query(".agent-view").remove()
        self.query_one("#transcript").display = True
        self.query_one(AgentsPanel).viewing = MAIN
        self._show_agents()
        self._box().focus()

    async def action_detailed_transcript(self) -> None:
        """Ctrl+O, Claude Code's transcript mode: the whole conversation
        with every result and thought unfolded, in the transcript's place
        (the transcript itself stays folded); again goes back. In a
        sub-agent's view it unfolds (or folds) that view."""
        view = next(iter(self.query(AgentView)), None)
        if view is not None and view.detail:
            await self.close_agent()
            return
        if view is not None:
            view.set_expanded(not view.expanded)
            return
        if not self.task_id or self.query(".agent-view"):
            return
        detail = AgentView(self.task_id, "transcript", detail=True, on_back=self.close_agent)
        self.query_one("#transcript").display = False
        await self.mount(detail, after="#transcript")

    def action_toggle_tasks(self) -> None:
        self.show_tasks = not self.show_tasks
        self._show_todos()

    async def decide(self, call_id: str, choice: str, message: str = "", *,
                     task_id: str = "") -> None:
        """Answer an approval. ``message``: what the person added to it — with
        a no, the reason the model reads in the refused call's result; with a
        yes, words the model reads beside the call's result."""
        self.waiting_for = ""
        task = task_id or self.task_id
        async with self.failures_shown("answering"):
            await answer_choice(self.local.client, task, call_id, choice, message)
        self.update_status()
        self._box().focus()

    async def action_cycle_mode(self) -> None:
        """Step to the next permission mode (Claude Code's shift+tab)."""
        if self.read_only:
            return
        async with self.failures_shown("switching the mode"):
            await self.ensure_task()
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
        a question with several); the answer is sent once all are in. A
        number picks that option."""
        transcript = self.transcript()
        await transcript.mount(UserMessage(text))
        transcript.stick()
        answers = typed_answer(self.question_detail, self._numbered(text),
                               self.question_answers)
        nxt = next_question(self.question_detail, answers)
        if nxt is not None:
            self.question_answers = answers
            await self.say_notice(f"next: {nxt.get('question', '')}")
            return
        task_id, call_id = self.question_task or self.task_id, self.waiting_question
        self._set_question("", "", {})
        for card in self.query(QuestionCard):
            card.add_class("-answered")
        await self.local.client.answer(task_id, call_id, data={"answers": answers})

    def _numbered(self, text: str) -> str:
        """``2`` (or ``1, 3``) as the labels of the current question's options."""
        questions = [q for q in self.question_detail.get("questions") or []
                     if q.get("id") not in self.question_answers]
        if not questions:
            return text
        options = questions[0].get("options") or []
        parts = [p.strip() for p in text.split(",")]
        if not parts or not all(p.isdigit() and 1 <= int(p) <= len(options) for p in parts):
            return text
        return ", ".join(str(options[int(p) - 1].get("label", "")) for p in parts)


def light_terminal() -> bool:
    """The terminal says its background is light (``COLORFGBG``'s last
    part is 7 or 15, as rxvt, Konsole and iTerm2 set it)."""
    import os

    return os.environ.get("COLORFGBG", "").rsplit(";", 1)[-1] in ("7", "15")


def first_prompt(events: list[Any]) -> str:
    """The first thing the person said in a task's log (``/resume`` shows it)."""
    for rec in events:
        ev = rec.event
        if isinstance(ev, InputTaken):
            for item in ev.items:
                if isinstance(item.body, Say) and item.body.message.origin in (None, "human"):
                    return item.body.message.text
    return ""


