"""The pieces of the screen: the welcome box, messages, tool calls, the
approval and question cards, pickers, the completion menu and the prompt."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Markdown, Static, TextArea

from noeta.core.types import ToolUse
from noeta.toolkit.commands import parse_command_message
from noeta_tui import render


class UserMessage(Static):
    """A prompt the person sent: ``❯ text`` (``! command`` in bash mode)."""

    DEFAULT_CLASSES = "user-message"

    def __init__(self, text: str, *, marker: str = "❯") -> None:
        self.text = text
        out = Text().append(f"{marker} ", style=f"bold {render.BASH}" if marker == "!" else "dim")
        typed = parse_command_message(text)  # a prompt command reads as it was typed
        out.append(f"/{typed[0]} {typed[1]}".rstrip() if typed else text)
        super().__init__(out)


class AssistantMessage(Markdown):
    DEFAULT_CLASSES = "assistant-message"


def bulleted(widget: Any, *, classes: str = "") -> Horizontal:
    """``widget`` after Claude Code's ``●`` bullet."""
    row = Horizontal(Static("●", classes="bullet"), widget, classes=f"row {classes}".strip())
    return row


class Notice(Static):
    DEFAULT_CLASSES = "notice"


class Welcome(Horizontal):
    """The box a task opens with: the mark beside the version, a line on
    where to start, the model and the directory."""

    DEFAULT_CLASSES = "welcome"

    def __init__(self, *, version: str, model: str, cwd: str, task_id: str) -> None:
        super().__init__()
        out = Text()
        out.append("noeta", style=f"bold {render.ACCENT}")
        out.append(f"  v{version}" if version else "", style="dim")
        out.append("\n")
        out.append("Welcome back! " if task_id else "Welcome! ")
        out.append("/help for commands · ? for shortcuts", style="dim")
        out.append("\n\n")
        rows = [("model", model), ("cwd", cwd)] + ([("task", task_id)] if task_id else [])
        for n, (key, value) in enumerate(rows):
            out.append("\n" if n else "")
            out.append(f"{key:<6}", style="dim")
            out.append(value)
        self.info = out

    def compose(self) -> ComposeResult:
        yield Static(render.logo(), classes="welcome-logo")
        yield Static(self.info, classes="welcome-info")


class ThinkingBlock(Static):
    """The model's reasoning while it streams: ``∴ Thinking…``, then
    ``∴ Thought for 4s``; ctrl+o shows what it thought."""

    DEFAULT_CLASSES = "thinking"

    def __init__(self, *, expanded: bool = False, started: float = 0.0) -> None:
        super().__init__("")
        self.text = ""
        self.expanded = expanded
        self.started = started
        self.took: float | None = None
        self._draw()

    def add(self, text: str) -> None:
        self.text += text
        if self.expanded:
            self._draw()

    def finish(self, took: float) -> None:
        if self.took is None:
            self.took = took
            self._draw()

    def set_expanded(self, expanded: bool) -> None:
        self.expanded = expanded
        self._draw()

    def _draw(self) -> None:
        out = Text()
        if self.took is None:
            out.append("∴ Thinking…", style="dim italic")
        else:
            out.append(f"∴ Thought for {render.elapsed_text(self.took)}", style="dim italic")
            if not self.expanded and self.text.strip():
                out.append(" (ctrl+o to show)", style="dim")
        if self.expanded and self.text.strip():
            out.append("\n")
            out.append(self.text.strip(), style="dim italic")
        self.update(out)


class ToolCall(Vertical):
    """One tool call: ``● Name(argument)`` with its state in the bullet's
    colour, and the ``⎿`` block under it — the latest output while it runs,
    the result folded once done (ctrl+o unfolds every call)."""

    DEFAULT_CLASSES = "tool-call"
    STYLES = {"pending": "dim", "running": "dim", "waiting": "yellow", "done": "green",
              "error": "red", "background": render.WORKING}

    def __init__(self, call: ToolUse, *, expanded: bool = False) -> None:
        super().__init__()
        self.call = call
        self.state = "pending"
        self.progressed = False
        #: Showing the folded result (``False`` while its output runs).
        self.collapsed = True
        self.expanded = expanded
        self.rows: list[tuple[str, str]] = []
        self.is_diff = False
        self.head = Static(self._header(), classes="tool-head")
        self.body = Static("", classes="tool-body")
        self.body.display = False  # until there is output
        #: A background agent's progress (an ``Agent`` call's child), or ``None``.
        self.child: dict[str, Any] | None = None
        #: A ``Workflow`` call's run (its records folded), or ``None``.
        self.workflow: dict[str, Any] | None = None

    @property
    def title(self) -> str:
        return render.tool_header(self.call)

    def compose(self) -> ComposeResult:
        yield self.head
        yield self.body

    def _header(self, frame: int = -1) -> render.Hanging:
        """``● Name(argument)``; a long one wraps under the name, not the bullet."""
        glyph = render.SPINNER_FRAMES[frame % len(render.SPINNER_FRAMES)] if frame >= 0 else "●"
        out = Text().append(f"{glyph} ", style=self.STYLES[self.state])
        out.append(render.tool_name(self.call), style="bold")
        arg = self.title[len(render.tool_name(self.call)):]
        out.append(arg)
        return render.Hanging(out, 2)

    def set_state(self, state: str) -> None:
        self.state = state
        self.head.update(self._header())
        self.set_class(state == "error", "-error")
        self.set_class(state == "done", "-done")
        if isinstance(self.parent, ReadGroup):
            self.parent.show_head()

    def set_progress(self, text: str) -> None:
        if self.state in ("done", "error"):
            return
        self.body.update(render.progress_block(text))
        self.body.display = True
        self.progressed = True
        self.collapsed = False

    def pulse(self, frame: int) -> None:
        """A frame of the bullet's spinner while its work runs in the background."""
        if self.state == "background":
            self.head.update(self._header(frame))

    def set_result(self, text: str, *, is_error: bool, meta: dict[str, Any] | None = None,
                   at: float | None = None) -> None:
        """``at``: when the result was recorded (wall clock), a background
        agent's start."""
        meta = meta or {}
        if not is_error and self.call.name == "Agent" and meta.get("child_id"):
            # The model's bookkeeping about the launch is not for the person:
            # the agent's progress shows instead.
            self.child = {"id": str(meta["child_id"]), "tools": 0, "tokens": 0, "last": "",
                          "started": at or time.time(), "status": "", "error": "",
                          "answer": "", "took": None, "seq": 0}
            self.set_state("background")
            self.show_agent()
            return
        if not is_error and self.call.name == "Workflow" and meta.get("run_id"):
            if self.workflow is None:
                self.workflow = {"run_id": str(meta["run_id"]), "agents": {}}
            if not self.workflow.get("status"):
                self.set_state("background")
            self.show_workflow()
            return
        self.set_state("error" if is_error else "done")
        self.rows = render.result_rows(self.call, text, meta or {}, is_error=is_error)
        self.is_diff = isinstance((meta or {}).get("diff"), str) and not is_error
        self.collapsed = True
        self._show()

    async def set_note(self, note: str) -> None:
        """What the person added to their yes, under the result, as the
        model reads it beside the result."""
        await self.mount(Static(render.Hanging(Text.assemble(("  ⎿  ", "dim"), note)),
                                classes="tool-note"))

    def set_expanded(self, expanded: bool) -> None:
        self.expanded = expanded
        if self.child is not None:
            self.show_agent()
        elif self.workflow is not None:
            self.show_workflow()
        elif self.rows:
            self._show()

    def show_agent(self) -> None:
        """A background agent's rows: what it is doing and how far it got,
        then how it ended."""
        c = self.child
        if c is None:
            return
        took = c["took"] if c["took"] is not None else time.time() - c["started"]
        stats = render.agent_line(c["tools"], c["tokens"], took)
        rows: list[tuple[str, str]] = []
        if self.expanded:
            rows.append((f"Prompt: {self.call.input.get('prompt', '')}", "dim"))
        if c["status"] == "done":
            rows.append((f"Done · {stats}", ""))
            if self.expanded and c["answer"]:
                rows += [(line, "") for line in str(c["answer"]).splitlines()]
        elif c["status"] in ("failed", "cancelled"):
            what = "Stopped" if c["status"] == "cancelled" else "Failed"
            rows.append((f"{what}{': ' + c['error'] if c['error'] else ''} · {stats}", "red"))
        else:
            rows.append((c["last"] or "Starting…", ""))
            rows.append((stats + " · in the background", "dim"))
        self.body.update(render.result_block(rows))
        self.body.display = True

    def end_agent(self, status: str, *, answer: str = "", error: str = "", tools: int = 0,
                  tokens: int = 0, at: float | None = None) -> None:
        c = self.child
        if c is None or c["status"]:
            return
        c.update(status=status, answer=answer, error=error,
                 tools=max(tools, c["tools"]), tokens=max(tokens, c["tokens"]),
                 took=max(0.0, (at or time.time()) - c["started"]))
        self.set_state("done" if status == "done" else "error")
        self.show_agent()

    def show_workflow(self) -> None:
        run = self.workflow
        if run is None:
            return
        status = run.get("status")
        if status and self.state == "background":
            self.set_state("done" if status == "completed" else "error")
        self.body.update(render.result_block(render.workflow_rows(run, expanded=self.expanded)))
        self.body.display = True

    def _show(self) -> None:
        rows = render.folded(self.rows, diff=self.is_diff, expanded=self.expanded,
                             read=self.call.name == "Read" and self.state == "done")
        self.body.update(render.result_block(rows))
        self.body.display = True


class ReadGroup(Vertical):
    """Reads one after another as one line, Claude Code's ``Read 3 files``;
    ctrl+o shows each call. A single read shows as itself."""

    DEFAULT_CLASSES = "read-group"

    def __init__(self, *, expanded: bool = False) -> None:
        super().__init__()
        self.expanded = expanded
        self.head = Static("", classes="read-head")

    def compose(self) -> ComposeResult:
        yield self.head

    @property
    def calls(self) -> list[ToolCall]:
        return [c for c in self.children if isinstance(c, ToolCall)]

    async def add(self, call: ToolCall) -> None:
        await self.mount(call)
        self.show_head()

    def set_expanded(self, expanded: bool) -> None:
        self.expanded = expanded
        self.show_head()

    def show_head(self) -> None:
        calls = self.calls
        many = len(calls) > 1
        self.head.display = many
        for call in calls:
            call.display = self.expanded or not many
        if not many:
            return
        failed = any(c.state == "error" for c in calls)
        running = any(c.state not in ("done", "error") for c in calls)
        out = Text().append("Reading " if running else "Read ", style="dim")
        out.append(str(len(calls)), style="bold")
        out.append(" files…" if running else " files", style="dim")
        if failed:
            out.append(" (some failed)", style="red")
        if not self.expanded:
            out.append(" (ctrl+o to expand)", style="dim")
        self.head.update(out)


class Choices(Static, can_focus=True):
    """Numbered options, the picked one marked ``❯``: up / down and Enter,
    a digit, or Esc for the cancel option."""

    BINDINGS = [Binding("up", "move(-1)", show=False), Binding("down", "move(1)", show=False),
                Binding("enter", "pick", show=False), Binding("escape", "cancel", show=False),
                *(Binding(str(n), f"number({n})", show=False) for n in range(1, 10))]

    def __init__(self, options: Sequence[Option], on_pick: Callable[[str], Awaitable[None]],
                 *, cancel: str = "", index: int = 0) -> None:
        super().__init__("", classes="choices")
        self.options = list(options)
        self.on_pick = on_pick
        #: The option Esc picks (``""``: Esc picks nothing).
        self.cancel = cancel
        self.index = max(0, min(index, len(self.options) - 1))
        self.update(self._text())

    def _text(self) -> Text:
        out = Text()
        for n, option in enumerate(self.options):
            here = n == self.index
            out.append("\n" if n else "")
            out.append("❯ " if here else "  ", style=f"bold {render.ACCENT}")
            out.append(f"{n + 1}. {option.label}", style=f"bold {render.ACCENT}" if here else "")
            if option.detail:
                out.append(f"  {option.detail}", style="dim")
        return out

    def action_move(self, step: int) -> None:
        if self.options:
            self.index = (self.index + step) % len(self.options)
            self.update(self._text())

    async def action_pick(self) -> None:
        if self.options:
            await self.on_pick(self.options[self.index].id)

    async def action_number(self, n: int) -> None:
        if 1 <= n <= len(self.options):
            self.index = n - 1
            self.update(self._text())
            await self.on_pick(self.options[n - 1].id)

    async def action_cancel(self) -> None:
        await self.on_pick(self.cancel)


@dataclass(frozen=True)
class Option:
    id: str
    label: str
    detail: str = ""


#: ``decide(call_id, choice_id, message)``: ``message`` is what the person
#: added to their yes or no (``""``: nothing).
Decide = Callable[[str, str, str], Awaitable[None]]
#: What the amend line asks for, by the option it amends (Claude Code's).
AMEND_PLACEHOLDERS = {"allow": "and tell noeta what to do next",
                      "deny": "and tell noeta what to do differently"}


class AmendLine(Input):
    """The approval card's amend line: Esc closes it (the choices take the
    keys again) instead of reaching the app's stop."""

    BINDINGS = [Binding("escape", "close", show=False)]

    async def action_close(self) -> None:
        card = next((a for a in self.ancestors if isinstance(a, ApprovalCard)), None)
        if card is not None:
            await card.close_amend()


class ApprovalCard(Vertical):
    """Asks before a tool runs, as Claude Code's dialog: what would run,
    ``Do you want to proceed?`` and its numbered choices
    (:func:`noeta.host.pending.choices`), also y / a / n; Esc says no. Tab
    on Yes or No opens a line to add what noeta should do next (or
    differently) to the answer; Enter sends both, an empty line or Esc
    closes it."""

    DEFAULT_CLASSES = "approval"
    BINDINGS = [Binding("y", "choose('allow')", show=False),
                Binding("a", "choose('always')", show=False),
                Binding("n", "choose('deny')", show=False),
                Binding("tab", "amend", show=False)]

    def __init__(self, call_id: str, title: str, prompt: str, decide: Decide,
                 options: list[dict[str, str]], *, preview: Text | None = None) -> None:
        """``preview``: the diff the call would make (an edit's)."""
        super().__init__()
        self.preview = preview
        self.options = options
        self.call_id = call_id
        self.title_text = title
        self.prompt = prompt
        self.decide = decide
        self.done = False
        #: The option the open amend line adds to (``""``: none open).
        self.amending = ""

    def compose(self) -> ComposeResult:
        yield Static(Text(self.title_text, style="bold"), classes="approval-title")
        if self.prompt and self.prompt != self.title_text:
            yield Static(Text(self.prompt), classes="approval-prompt")
        if self.preview is not None:
            yield Static(self.preview, classes="approval-diff")
        yield Static("Do you want to proceed?", classes="approval-question")
        if self.done:
            return
        deny = next((o["id"] for o in self.options if o["id"] == "deny"), "")
        yield Choices([Option(o["id"], o["label"] + (" (esc)" if o["id"] == deny else ""))
                       for o in self.options], self.choose, cancel=deny)
        amendable = any(o["id"] in AMEND_PLACEHOLDERS for o in self.options)
        yield Static("Esc to cancel" + (" · Tab to amend" if amendable else ""), classes="hint")

    def on_mount(self) -> None:
        # A card can be decided before it finishes mounting (a slow event
        # loop): its choices are gone then.
        if not self.done and self.query(Choices):
            self.query_one(Choices).focus()

    async def action_choose(self, choice: str) -> None:
        if not self.amending:
            await self.choose(choice)

    async def action_amend(self) -> None:
        if self.done or self.amending or not self.query(Choices):
            return
        choices = self.query_one(Choices)
        picked = choices.options[choices.index].id
        if picked not in AMEND_PLACEHOLDERS:
            return
        self.amending = picked
        line = AmendLine(placeholder=AMEND_PLACEHOLDERS[picked], classes="amend")
        await self.mount(line, after=choices)
        line.focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        picked, message = self.amending, event.value.strip()
        await self.close_amend()
        if message:
            await self.choose(picked, message)

    async def close_amend(self) -> None:
        self.amending = ""
        await self.query(AmendLine).remove()
        if not self.done and self.query(Choices):
            self.query_one(Choices).focus()

    async def action_pick(self, n: int) -> None:
        if 1 <= n <= len(self.options):
            await self.choose(self.options[n - 1]["id"])

    async def choose(self, choice: str, message: str = "") -> None:
        picked = next((o for o in self.options if o["id"] == choice), None)
        if self.done or picked is None:
            return
        self.done = True
        await self.decide(self.call_id, choice, message)
        await self.query(".hint, Choices, Input").remove()
        said = f": {message}" if message else ""
        await self.mount(Static(render.Hanging(Text(f"  ⎿  {picked['label']}{said}",
                                                    style="dim")), classes="approval-result"))


class Picker(Vertical):
    """A list to pick from in place of the prompt (``/model``, ``/resume``,
    the rewind list); Esc closes it with nothing picked."""

    DEFAULT_CLASSES = "picker"

    def __init__(self, title: str, options: Sequence[Option],
                 on_pick: Callable[[str], Awaitable[None]], *, hint: str = "",
                 index: int = 0) -> None:
        super().__init__()
        self.title_text = title
        self.options = list(options)
        self.on_pick_cb = on_pick
        self.hint = hint or "Enter to select · Esc to cancel"
        self.index = index

    def compose(self) -> ComposeResult:
        yield Static(Text(self.title_text, style="bold"), classes="picker-title")
        yield Choices(self.options, self._picked, index=self.index)
        yield Static(self.hint, classes="hint")

    def on_mount(self) -> None:
        self.query_one(Choices).focus()

    async def _picked(self, choice: str) -> None:
        await self.remove()
        await self.on_pick_cb(choice)


class QuestionCard(Vertical):
    """The agent asked a question; the next input line answers it (an
    option's number or label picks it)."""

    DEFAULT_CLASSES = "question"

    def __init__(self, detail: dict[str, Any], *, who: str = "") -> None:
        super().__init__()
        self.detail = detail
        self.who = who

    def compose(self) -> ComposeResult:
        asker = escape(f"[{self.who}]") if self.who else "The agent"
        yield Static(f"[b]{asker} asks[/b] [dim](type a number, a label or your own answer; "
                     "one line per question)[/dim]")
        questions = self.detail.get("questions") or []
        if not questions:
            yield Static(json.dumps(self.detail, ensure_ascii=False, indent=2))
        for q in questions:
            yield Static(Text(f"☐ {q.get('header') or ''} {q.get('question', '')}".strip(),
                              style="bold"))
            for n, opt in enumerate(q.get("options") or [], 1):
                line = Text(f"  {n}. {opt.get('label', '')}")
                if opt.get("description"):
                    line.append(f"  {opt['description']}", style="dim")
                yield Static(line, classes="question-option")


class Completions(Static):
    """The menu under the prompt while typing ``/name`` or ``@path``."""

    DEFAULT_CLASSES = "completions"
    #: Rows shown at once.
    ROWS = 8

    def __init__(self) -> None:
        super().__init__("", id="completions")
        #: ``(text it completes to, label, description)``.
        self.items: list[tuple[str, str, str]] = []
        self.index = 0

    def show(self, items: list[tuple[str, str, str]]) -> None:
        self.items, self.index = items, 0
        self.display = bool(items)
        # As tall as it gets, whatever matches: typing does not move the prompt.
        self.styles.height = self.ROWS
        self._draw()
        sync = getattr(self.app, "_sync_footer", None) if self.is_attached else None
        if sync is not None:
            sync()

    def hide(self) -> None:
        self.show([])

    def move(self, step: int) -> None:
        if self.items:
            self.index = (self.index + step) % len(self.items)
            self._draw()

    def current(self) -> str | None:
        return self.items[self.index][0] if self.items else None

    def _draw(self) -> None:
        start = max(0, self.index - self.ROWS + 1)
        width = min(max((len(label) for _, label, _ in self.items), default=0), 32) + 3
        out = Text(no_wrap=True, overflow="ellipsis")
        for n, (_, label, description) in enumerate(self.items[start:start + self.ROWS], start):
            here = n == self.index
            out.append("\n" if n > start else "")
            shown = label if len(label) <= 32 else label[:31] + "…"
            out.append(("❯ " if here else "  ") + shown.ljust(width),
                       style=f"bold {render.ACCENT}" if here else "")
            out.append(" ".join(description.split()), style="" if here else "dim")
        if len(self.items) > self.ROWS:
            out.append(f"  {self.index + 1}/{len(self.items)}", style="dim")
        self.update(out)


#: ``on_key(key) -> handled``: the app's turn at a key before the box's.
KeyHook = Callable[[str], Awaitable[bool]]


class PromptInput(TextArea):
    """Enter sends; ctrl+j (or ``\\`` then Enter) adds a line. The app sees
    each key first (menus, history, modes)."""

    def __init__(self, on_submit: Callable[[str], Awaitable[None]],
                 on_image: Callable[[Any, str], Awaitable[None]] | None = None,
                 on_key: KeyHook | None = None) -> None:
        super().__init__(id="prompt", soft_wrap=True, compact=True,
                         placeholder='Try "fix the failing test"  ·  ? for shortcuts')
        self.on_submit_cb = on_submit
        self.on_image = on_image
        self.on_key_cb = on_key

    async def _on_paste(self, event: events.Paste) -> None:
        """A pasted path to an image file (a file dropped on the terminal)
        attaches that image instead of its path."""
        from pathlib import Path

        from noeta_tui.mentions import image_file

        found: list[tuple[Any, str]] = []
        for raw in dropped_paths(event.text) if self.on_image is not None else []:
            path = Path(raw).expanduser()
            try:
                image = image_file(path) if path.is_absolute() else None
            except OSError:
                image = None
            if image is None:
                found = []
                break
            found.append((image, path.name))
        if found and self.on_image is not None:
            event.prevent_default()
            event.stop()
            for image, name in found:
                await self.on_image(image, name)
            return
        await super()._on_paste(event)

    async def _on_key(self, event: events.Key) -> None:
        if self.on_key_cb is not None and await self.on_key_cb(event.key):
            event.prevent_default()
            event.stop()
            return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            if self.text.endswith("\\"):
                self.replace("\n", (self.document.end[0], self.document.end[1] - 1),
                             self.document.end)
                return
            text = self.text.strip()
            if text:
                self.clear()
                await self.on_submit_cb(text)
            return
        if event.key in ("ctrl+j", "shift+enter", "alt+enter"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        await super()._on_key(event)


def dropped_paths(text: str) -> list[str]:
    """The paths in what a terminal pastes for files dropped on it: one per
    line or separated by spaces, each maybe quoted, with ``\\ `` for a space
    or as a ``file://`` URL."""
    import shlex
    from urllib.parse import unquote, urlparse

    text = text.strip()
    if not text:
        return []
    try:
        parts = shlex.split(text)
    except ValueError:
        return []
    out = []
    for part in parts:
        if part.startswith("file://"):
            part = unquote(urlparse(part).path)
        out.append(part)
    return out
