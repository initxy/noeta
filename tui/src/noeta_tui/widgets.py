"""The pieces of the screen: the welcome box, messages, tool calls, the
approval and question cards, pickers, the completion menu and the prompt."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Markdown, Static, TextArea

from noeta.core.types import ToolUse
from noeta_tui import render


class UserMessage(Static):
    """A prompt the person sent: ``❯ text`` (``! command`` in bash mode)."""

    DEFAULT_CLASSES = "user-message"

    def __init__(self, text: str, *, marker: str = "❯") -> None:
        self.text = text
        out = Text().append(f"{marker} ", style="bold #fd5db1" if marker == "!" else "dim")
        out.append(text)
        super().__init__(out)


class AssistantMessage(Markdown):
    DEFAULT_CLASSES = "assistant-message"


def bulleted(widget: Any, *, classes: str = "") -> Horizontal:
    """``widget`` after Claude Code's ``●`` bullet."""
    row = Horizontal(Static("●", classes="bullet"), widget, classes=f"row {classes}".strip())
    return row


class Notice(Static):
    DEFAULT_CLASSES = "notice"


class Welcome(Static):
    """The box a task opens with (Claude Code's welcome box)."""

    DEFAULT_CLASSES = "welcome"

    def __init__(self, *, version: str, model: str, cwd: str, task_id: str) -> None:
        out = Text()
        out.append("✻ ", style="bold #d77757")
        out.append("Welcome to noeta", style="bold")
        out.append(f" {version}" if version else "", style="dim")
        out.append("\n\n  /help for help, /status for your current setup\n\n", style="dim")
        out.append(f"  cwd: {cwd}\n  model: {model}" + (f"\n  task: {task_id}" if task_id else ""),
                   style="dim")
        super().__init__(out)


class ToolCall(Vertical):
    """One tool call: ``● Name(argument)`` with its state in the bullet's
    colour, and the ``⎿`` block under it — the latest output while it runs,
    the result folded once done (ctrl+o unfolds every call)."""

    DEFAULT_CLASSES = "tool-call"
    STYLES = {"pending": "dim", "running": "dim", "waiting": "yellow", "done": "green",
              "error": "red"}

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

    @property
    def title(self) -> str:
        return render.tool_header(self.call)

    def compose(self) -> ComposeResult:
        yield self.head
        yield self.body

    def _header(self) -> Text:
        out = Text().append("● ", style=self.STYLES[self.state])
        out.append(render.tool_name(self.call), style="bold")
        arg = self.title[len(render.tool_name(self.call)):]
        out.append(arg)
        return out

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

    def set_result(self, text: str, *, is_error: bool, meta: dict[str, Any] | None = None
                   ) -> None:
        self.set_state("error" if is_error else "done")
        self.rows = render.result_rows(self.call, text, meta or {}, is_error=is_error)
        self.is_diff = isinstance((meta or {}).get("diff"), str) and not is_error
        self.collapsed = True
        self._show()

    def set_expanded(self, expanded: bool) -> None:
        self.expanded = expanded
        if self.rows:
            self._show()

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
            out.append("❯ " if here else "  ", style="bold #b1b9f9")
            out.append(f"{n + 1}. {option.label}", style="bold #b1b9f9" if here else "")
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


#: ``decide(call_id, choice_id)``.
Decide = Callable[[str, str], Awaitable[None]]


class ApprovalCard(Vertical):
    """Asks before a tool runs, as Claude Code's dialog: what would run,
    ``Do you want to proceed?`` and its numbered choices
    (:func:`noeta.host.pending.choices`), also y / a / n; Esc says no."""

    DEFAULT_CLASSES = "approval"
    BINDINGS = [Binding("y", "choose('allow')", show=False),
                Binding("a", "choose('always')", show=False),
                Binding("n", "choose('deny')", show=False)]

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
        yield Static("Esc to cancel", classes="hint")

    def on_mount(self) -> None:
        # A card can be decided before it finishes mounting (a slow event
        # loop): its choices are gone then.
        if not self.done and self.query(Choices):
            self.query_one(Choices).focus()

    async def action_choose(self, choice: str) -> None:
        await self.choose(choice)

    async def action_pick(self, n: int) -> None:
        if 1 <= n <= len(self.options):
            await self.choose(self.options[n - 1]["id"])

    async def choose(self, choice: str) -> None:
        picked = next((o for o in self.options if o["id"] == choice), None)
        if self.done or picked is None:
            return
        self.done = True
        await self.decide(self.call_id, choice)
        await self.query(".hint, Choices").remove()
        await self.mount(Static(Text(f"  ⎿  {picked['label']}", style="dim"),
                                classes="approval-result"))


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
        self._draw()

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
        width = max((len(label) for _, label, _ in self.items), default=0) + 4
        out = Text()
        for n, (_, label, description) in enumerate(self.items[start:start + self.ROWS], start):
            here = n == self.index
            out.append("\n" if n > start else "")
            out.append(("❯ " if here else "  ") + label.ljust(width),
                       style="bold #b1b9f9" if here else "")
            out.append(description[:100], style="#b1b9f9" if here else "dim")
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
                         placeholder='Try "fix the failing test" — / for commands, '
                                     "@ for files, ! for bash, ? for shortcuts")
        self.on_submit_cb = on_submit
        self.on_image = on_image
        self.on_key_cb = on_key

    async def _on_paste(self, event: events.Paste) -> None:
        """A pasted path to an image file (a file dropped on the terminal)
        attaches that image instead of its path."""
        from pathlib import Path

        from noeta_tui.mentions import image_file

        raw = event.text.strip().strip("'\"")
        if self.on_image is not None and raw and "\n" not in raw:
            path = Path(raw).expanduser()
            try:
                image = image_file(path) if path.is_absolute() else None
            except OSError:
                image = None
            if image is not None:
                event.prevent_default()
                event.stop()
                await self.on_image(image, path.name)
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
        if event.key == "ctrl+j":
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        await super()._on_key(event)
