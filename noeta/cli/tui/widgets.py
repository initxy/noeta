"""The pieces of the transcript: messages, tool calls, approval and question
cards, notices."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text as RichText
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Collapsible, Markdown, Static, TextArea

from noeta.core.types import ToolUse
from noeta.host import feed

#: Characters of a tool result shown when a call is expanded.
RESULT_PREVIEW_CHARS = 6000


def tool_title(call: ToolUse) -> str:
    return feed.tool_title(call, limit=100, gap="  ")


class UserMessage(Static):
    DEFAULT_CLASSES = "user-message"

    def __init__(self, text: str) -> None:
        super().__init__(RichText(text))


class AssistantMessage(Markdown):
    DEFAULT_CLASSES = "assistant-message"


class Notice(Static):
    DEFAULT_CLASSES = "notice"


class ToolCall(Collapsible):
    """One tool call: a one-line title with its state; expand for the result."""

    ICONS = {"pending": "○", "running": "◐", "done": "●", "error": "✗", "waiting": "?"}

    def __init__(self, call: ToolUse) -> None:
        self.call = call
        self.state = "pending"
        self.body = Static("", classes="tool-body")
        super().__init__(self.body, title=self._label(), collapsed=True, classes="tool-call")

    def _label(self) -> str:
        return f"{self.ICONS[self.state]} {tool_title(self.call)}"

    def set_state(self, state: str) -> None:
        self.state = state
        self.title = self._label()
        self.set_class(state == "error", "-error")
        self.set_class(state == "done", "-done")

    def set_result(self, text: str, *, is_error: bool, diff: str | None = None) -> None:
        self.set_state("error" if is_error else "done")
        if diff:
            self.body.update(Syntax(diff, "diff", theme="ansi_dark", word_wrap=True))
            self.collapsed = False
        else:
            shown = text if len(text) <= RESULT_PREVIEW_CHARS else \
                text[:RESULT_PREVIEW_CHARS] + "\n…"
            self.body.update(RichText(shown))
            if is_error:
                self.collapsed = False


#: ``decide(call_id, choice_id)``.
Decide = Callable[[str, str], Awaitable[None]]


class ApprovalCard(Vertical):
    """Asks before a tool runs, with Claude Code's choices
    (:func:`noeta.host.pending.choices`: Yes, don't ask again, switch mode,
    No), picked by number, or y / a / n for yes / always / no."""

    DEFAULT_CLASSES = "approval"
    BINDINGS = [("y", "choose('allow')", "Yes"), ("a", "choose('always')", "Always"),
                ("n", "choose('deny')", "No"),
                *((str(n), f"pick({n})", "") for n in range(1, 10))]

    def __init__(self, call_id: str, title: str, prompt: str, decide: Decide,
                 options: list[dict[str, str]]) -> None:
        super().__init__()
        self.options = options
        self.call_id = call_id
        self.title_text = title
        self.prompt = prompt
        self.decide = decide
        self.done = False

    def compose(self) -> ComposeResult:
        yield Static(f"[b]{escape(self.title_text)}[/b]")
        if self.prompt and self.prompt != self.title_text:
            yield Static(self.prompt, classes="approval-prompt")
        yield Static("Do you want to proceed?", classes="approval-prompt")
        if self.done:
            return
        with Vertical(classes="approval-buttons"):
            for n, option in enumerate(self.options, 1):
                variant = {"allow": "success", "deny": "error"}.get(option["id"], "default")
                yield Button(f"{n}. {escape(option['label'])}", id=f"option-{n}",
                             variant=variant, compact=True)  # type: ignore[arg-type]

    def on_mount(self) -> None:
        # A card can be decided before it finishes mounting (a slow event
        # loop): its buttons are gone then, and an error here ends the app.
        if not self.done:
            self.query_one("#option-1", Button).focus()

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
        self.remove_children(".approval-buttons")
        await self.mount(Static(escape(picked["label"]), classes="approval-result"))

    @on(Button.Pressed)
    async def _pressed(self, event: Button.Pressed) -> None:
        n = int((event.button.id or "option-0").rsplit("-", 1)[1])
        await self.action_pick(n)


class QuestionCard(Vertical):
    """The agent asked a question; the next input line is the answer."""

    DEFAULT_CLASSES = "question"

    def __init__(self, detail: dict[str, Any], *, who: str = "") -> None:
        super().__init__()
        self.detail = detail
        self.who = who

    def compose(self) -> ComposeResult:
        asker = escape(f"[{self.who}]") if self.who else "The agent"
        yield Static(f"[b]{asker} asks[/b] (type your answer below; an option's label "
                     "picks it, one line per question)")
        questions = self.detail.get("questions") or []
        if questions:
            for q in questions:
                yield Static(f"• {q.get('question', '')}")
                for opt in q.get("options") or []:
                    yield Static(f"    - {opt.get('label', '')}: {opt.get('description', '')}",
                                 classes="question-option")
        else:
            yield Static(json.dumps(self.detail, ensure_ascii=False, indent=2))


class PromptInput(TextArea):
    """Enter sends; ctrl+j adds a line."""

    def __init__(self, on_submit: Callable[[str], Awaitable[None]],
                 on_image: Callable[[Any, str], Awaitable[None]] | None = None) -> None:
        super().__init__(id="prompt", soft_wrap=True, compact=True,
                         placeholder="Message noeta — / for commands, @ for files, Esc to stop")
        self.on_submit_cb = on_submit
        self.on_image = on_image

    async def _on_paste(self, event: Any) -> None:
        """A pasted path to an image file (a file dropped on the terminal)
        attaches that image instead of its path."""
        from pathlib import Path

        from noeta.cli.mentions import image_file

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

    async def _on_key(self, event: Any) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
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
