"""The full-screen terminal app. It is a client like any other: it creates or
resumes a task, sends what you type, and renders what the task records."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static, TextArea

from noeta.core.events import InputTaken, Say, Waiting
from noeta.core.extension import Command, CommandContext
from noeta.core.types import Delta, Image
from noeta.host.app import FrontEndStart
from noeta.host.local import UnknownAgent
from noeta.host.pending import answer_choice
from noeta.host.questions import next_question, typed_answer
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
from noeta_tui import bash_mode, files, history, mentions, slash
from noeta_tui.transcript import SERVER_DIRECTORY, TranscriptView, _short_path
from noeta_tui.widgets import (
    Completions,
    Notice,
    PromptInput,
    QuestionCard,
    ReadGroup,
    ToolCall,
    UserMessage,
    Welcome,
)

log = logging.getLogger("noeta_tui")

SHORTCUTS = """\
[dim]! for bash mode        double tap esc to clear input     ctrl + c twice to exit
/ for commands         shift + tab to cycle modes        ctrl + v to paste images
@ for file paths       ctrl + o for verbose output       ctrl + b for subtasks
ctrl + r for history   ctrl + t to toggle tasks          \\⏎ or ctrl + j for newline
                       ↑ ↓ for earlier prompts           esc to interrupt[/dim]"""

#: Seconds between two presses of Esc (or Ctrl+C) that count as a double press.
DOUBLE_PRESS = 0.8
#: Seconds the mode key waits for a new task's start to record its mode.
MODE_WAIT = 2.0


#: What ``-c`` says, before it exits, when nothing was started here.
NOTHING_TO_CONTINUE = "No conversation found to continue"


class NoetaApp(TranscriptView):
    CSS_PATH = "style.tcss"
    TITLE = "noeta"
    BINDINGS = [
        Binding("escape", "stop_turn", "Stop", show=False),
        Binding("ctrl+b", "toggle_side", "Side panel", show=False),
        Binding("shift+tab", "cycle_mode", "Mode", show=False, priority=True),
        Binding("ctrl+v", "paste_image", "Paste image", show=False),
        Binding("ctrl+o", "toggle_verbose", "Verbose", show=False, priority=True),
        Binding("ctrl+t", "toggle_tasks", "Tasks", show=False, priority=True),
        Binding("ctrl+c", "interrupt", "Quit", show=False, priority=True),
        Binding("ctrl+r", "search_history", "History", show=False, priority=True),
    ]

    def __init__(self, local: Any, *, agent: str, model: str, cwd: str,
                 resume: str | None = None, permission_mode: str | None = None,
                 task_config: dict[str, Any] | None = None, on_server: bool = False,
                 start: FrontEndStart | None = None) -> None:
        """``local``: the ``AppAPI`` the app runs on. ``on_server``: it is a
        server's, so its tasks run in the server's directories, not ``cwd``
        on this machine. ``start``: what the ``noeta`` command said (the
        home directory, the settings shown, the version)."""
        super().__init__()
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
        self.compacts_at = None
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
        self.todos: dict[str, dict[str, Any]] = {}
        self.verbose = False
        self.show_tasks = True
        self.queued: list[str] = []
        self.echoed: list[str] = []
        self.turn_started = 0.0
        self.turn_tokens = 0
        self.turn_output = 0
        self.turn_verb = ""
        self.flash = ""
        #: Prompts sent in this project, oldest first (earlier sessions'
        #: too); ``history_at`` steps through them.
        self._history_file = history.history_file(self.start.home, self.start.cwd)
        self.history: list[str] = history.load(self._history_file)
        self.history_at = len(self.history)
        #: The commands the slash menu offers: plugins' for this task.
        self.plugin_commands: list[Command] = []
        self._deltas: asyncio.Queue[Delta] = asyncio.Queue()
        self._follower: Any = None
        #: Held while a new screen's task is created (one task, however many ask).
        self._creating = asyncio.Lock()
        #: Ctrl+R: the prompt filters the history menu.
        self.searching = False

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield VerticalScroll(id="transcript")
            with Vertical(id="side"):
                yield Static("[b]Subtasks[/b]", id="side-title")
                yield Static("", id="side-body")
        yield Static("", id="spinner")
        yield Static("", id="tasks")
        yield Static("", id="queued")
        yield Vertical(id="overlay")
        yield Static("", id="effort")
        with Horizontal(id="prompt-box"):
            yield Static("❯", id="marker")
            yield PromptInput(self.submit, self.add_image, self.prompt_key)
        yield Completions()
        yield Static(SHORTCUTS, id="shortcuts")
        with Horizontal(id="footer"):
            yield Static("", id="hints")
            yield Static("", id="status")

    async def on_mount(self) -> None:
        for hidden in ("#side", "#spinner", "#tasks", "#queued", "#completions",
                       "#shortcuts"):
            self.query_one(hidden).display = False
        await self.local.start()
        self.local.deltas(self._on_delta)
        self._pump_deltas()
        self._refresh_side()
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
        self.query_one(PromptInput).focus()
        if prompt and self.resume == "":
            self.query_one(PromptInput).text = prompt
        elif prompt:
            await self.submit(prompt)

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
            tid: str = await self.local.client.create(self.agent, None, config=config)
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
        await self._clear(task_id, refused=refused)
        self._follower = self.follow(task_id)
        self._load_commands()
        self.update_status()

    async def _clear(self, task_id: str, *, refused: str = "") -> None:
        """Forget what was shown and show ``task_id``'s welcome box (``""``:
        a new task's)."""
        if self._follower is not None:
            self._follower.cancel()
            self._follower = None
        self.read_only = bool(refused)
        self.task_id = task_id
        self.task_cwd = self._default_task_cwd()
        self.effort = self.task_config.get("effort")
        self.context_tokens = 0
        self.compacts_at = None
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
        self.todos.clear()
        self._show_todos()
        self.queued.clear()
        self.echoed.clear()
        self._show_queued()
        transcript = self.query_one("#transcript")
        await transcript.remove_children()
        await transcript.mount(Welcome(version=self.start.version, model=self.model,
                                       cwd=_short_path(self.task_cwd, 1000) or SERVER_DIRECTORY,
                                       task_id=task_id))
        for notice in getattr(self.local, "notices", ()):
            await transcript.mount(Notice(notice))
        if refused:
            await transcript.mount(Notice(f"read-only: {refused}. Its history is shown; "
                                          "/new starts a task here.", classes="-error"))

    def _default_task_cwd(self) -> str:
        """What the footer shows until the task's ``TaskCreated`` says: the
        directory new tasks are asked to run in (on a server, ``""`` for its own)."""
        return str(self.task_config.get("cwd") or ("" if self.on_server else self.cwd))

    async def pick_task(self) -> None:
        """Open the latest task and offer the others (``/resume``'s picker)."""
        rows = await self.local.client.tasks(limit=20)
        roots = [r for r in rows if r.parent_id is None]
        if not roots:
            await self.new_task()
            return
        await self.open_task(roots[0].task_id)
        self.listed = [r.task_id for r in roots[:10]]
        await slash.resume_picker(self, roots[:10])

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
        self.query_one(Completions).hide()
        self.query_one("#shortcuts").display = False
        if not self.history or self.history[-1] != text:
            self.history.append(text)
            history.append(self._history_file, text)
        self.history_at = len(self.history)
        self._set_marker("")
        if text.startswith("/"):
            await self.command(text)
            return
        if text.startswith("!") and not self.waiting_question:
            await bash_mode.run(self, text[1:].strip())
            return
        await self.send(text)

    async def send(self, text: str) -> None:
        if self.read_only:
            await self.say_notice("this task is read-only here (its agent is not available)")
            return
        if self.waiting_question:
            await self.answer_question(text)
            return
        images, self.images = self.images, []
        context, self.bash_context = self.bash_context, []
        body = "\n\n".join([*context, text]) if context else text
        message = mentions.attach(body, images) if images else body
        if self.busy:
            self.queued.append(body)
            self._show_queued()
        else:  # shown at once, as typed; the task's log confirms it
            self.echoed.append(body)
            transcript = self.query_one("#transcript")
            await transcript.mount(UserMessage(text))
            transcript.scroll_end(animate=False)
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
            rows = await self.local.client.tasks(limit=20)
            roots = [r for r in rows if r.parent_id is None][:10]
            self.listed = [r.task_id for r in roots]
            await slash.resume_picker(self, roots)

    async def say_markdown(self, text: str) -> None:
        from textual.widgets import Markdown

        transcript = self.query_one("#transcript")
        await transcript.mount(Markdown(text, classes="notice"))
        transcript.scroll_end(animate=False)

    async def say_text(self, text: Text) -> None:
        """Rich text, as a command's output (``  ⎿`` under the command)."""
        transcript = self.query_one("#transcript")
        await transcript.mount(Static(text, classes="command-output"))
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
        return self.query_one(PromptInput)

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

    @on(TextArea.Changed, "#prompt")
    def _typed(self, event: TextArea.Changed) -> None:
        text = event.text_area.text
        self.query_one("#shortcuts").display = False
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
            menu.show([(f"@{p}", f"+ {p}", "") for p in
                       files.matches(self.task_cwd or self.cwd, word[1:])])
        else:
            menu.hide()

    def _set_marker(self, marker: str) -> None:
        bash = marker == "!"
        self.query_one("#marker", Static).update(Text(marker or "❯",
                                                      style="bold #fd5db1" if bash else ""))
        self.query_one("#prompt-box").set_class(bash, "-bash")

    # ------------------------------------------------------------ keys

    async def action_stop_turn(self) -> None:
        if self.task_id and self.busy:
            await self.local.client.cancel(self.task_id, "stopped from the terminal",
                                           turn_only=True)
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

    async def action_interrupt(self) -> None:
        """Ctrl+C: clear the input; on an empty one, twice to quit."""
        box = self._box()
        if box.text:
            box.clear()
            return
        now = time.monotonic()
        if now - self._last_interrupt <= DOUBLE_PRESS:
            self.exit()
            return
        self._last_interrupt = now
        self.flash = "Press Ctrl-C again to exit"
        self.update_status()
        self.set_timer(DOUBLE_PRESS, self._clear_flash)

    def _clear_flash(self) -> None:
        self.flash = ""
        self.update_status()

    def action_toggle_side(self) -> None:
        side = self.query_one("#side")
        side.display = not side.display

    def action_toggle_verbose(self) -> None:
        self.verbose = not self.verbose
        for call in self.query(ToolCall):
            call.set_expanded(self.verbose)
        for group in self.query(ReadGroup):
            group.set_expanded(self.verbose)

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
        await answer_choice(self.local.client, task, call_id, choice, message)
        self.update_status()
        self._box().focus()

    async def action_cycle_mode(self) -> None:
        """Step to the next permission mode (Claude Code's shift+tab)."""
        if self.read_only:
            return
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
        transcript = self.query_one("#transcript")
        await transcript.mount(UserMessage(text))
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
            self.query_one("#side-body", Static).update(Text(body))


def first_prompt(events: list[Any]) -> str:
    """The first thing the person said in a task's log (``/resume`` shows it)."""
    for rec in events:
        ev = rec.event
        if isinstance(ev, InputTaken):
            for item in ev.items:
                if isinstance(item.body, Say) and item.body.message.origin in (None, "human"):
                    return item.body.message.text
    return ""


