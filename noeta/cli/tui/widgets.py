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
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Collapsible, Markdown, Static, TextArea

from noeta.core.types import ToolUse

#: Characters of a tool result shown when a call is expanded.
RESULT_PREVIEW_CHARS = 6000


def tool_title(call: ToolUse) -> str:
    i = call.input
    target = (i.get("file_path") or i.get("path") or i.get("command") or i.get("pattern")
              or i.get("url") or i.get("query") or i.get("description") or "")
    target = str(target).splitlines()[0][:100] if target else ""
    return f"{call.name}  {target}".rstrip()


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


Decide = Callable[[str, str, bool], Awaitable[None]]


class ApprovalCard(Vertical):
    """Asks before a tool runs: allow once, always, or deny (y / a / n).
    ``always`` is the "always" choice's text, naming the rule it remembers
    (:func:`noeta.core.pending.always_option`); ``None`` offers none."""

    DEFAULT_CLASSES = "approval"
    BINDINGS = [("y", "choose('allow')", "Allow"), ("a", "choose('always')", "Always"),
                ("n", "choose('deny')", "Deny")]

    def __init__(self, call_id: str, title: str, prompt: str, decide: Decide,
                 always: str | None = "Always allow") -> None:
        super().__init__()
        self.always = always
        self.call_id = call_id
        self.title_text = title
        self.prompt = prompt
        self.decide = decide
        self.done = False

    def compose(self) -> ComposeResult:
        yield Static(f"[b]Allow?[/b] {escape(self.title_text)}")
        if self.prompt and self.prompt != self.title_text:
            yield Static(self.prompt, classes="approval-prompt")
        with Horizontal(classes="approval-buttons"):
            yield Button("Allow (y)", id="allow", variant="success", compact=True)
            if self.always is not None:
                yield Button(f"{escape(self.always)} (a)", id="always", compact=True)
            yield Button("Deny (n)", id="deny", variant="error", compact=True)

    def on_mount(self) -> None:
        self.query_one("#allow", Button).focus()

    async def action_choose(self, choice: str) -> None:
        await self.choose(choice)

    async def choose(self, choice: str) -> None:
        if self.done or (choice == "always" and self.always is None):
            return
        self.done = True
        await self.decide(self.call_id, "deny" if choice == "deny" else "allow",
                          choice == "always")
        self.remove_children(".approval-buttons")
        shown = {"allow": "allowed", "deny": "denied"}.get(choice) or (self.always or "")
        await self.mount(Static(escape(shown), classes="approval-result"))

    @on(Button.Pressed)
    async def _pressed(self, event: Button.Pressed) -> None:
        await self.choose(event.button.id or "deny")


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

    def __init__(self, on_submit: Callable[[str], Awaitable[None]]) -> None:
        super().__init__(id="prompt", soft_wrap=True, compact=True,
                         placeholder="Message noeta — / for commands, Esc to stop")
        self.on_submit_cb = on_submit

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
