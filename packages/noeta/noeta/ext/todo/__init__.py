"""The todo list: the ``TodoWrite`` tool and Claude Code's todo reminder.

``TodoWrite`` replaces the whole checklist; the list is durable state
(``todos`` records, folded into ``state.ext["todo"]``) that the reminder and
hosts read. The tool is an ordinary tool, so 0.6's control-tool mixing rules
are reproduced as a gate: a response that carries more than one
``TodoWrite``, an invalid one, or a ``TodoWrite`` beside a tool that must run
alone (``AskUserQuestion``, ``skill``, ...) runs nothing, and every call of
that response reads the same reason. Beside ordinary tools (and ``Task``) the
list is saved and the others run.

The reminder is Claude Code's: before a model request, when ten assistant
content blocks (thinking excluded) have passed both since the last
``TodoWrite`` call and since the last reminder, the reminder is recorded as a
system-origin message carrying the list as it stands. Counting walks what the
model still sees, so after a compaction it starts again at the boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from importlib import resources
from typing import Any

import msgspec

from noeta.ext.base import Deny, Extension, Gate, TaskView
from noeta.tools.base import Tool, ToolContext, ToolOutput, text_output
from noeta.types import Message, ModelRequest, Thinking, ToolUse, user

__all__ = [
    "TODO_TURNS_BETWEEN_REMINDERS",
    "TODO_TURNS_SINCE_WRITE",
    "TODO_WRITE_ACK",
    "TODO_WRITE_TOOL",
    "Todo",
    "TodoWriteTool",
    "reminder_due",
    "todo_reminder_text",
    "validate_todos",
]

TODO_WRITE_TOOL = "TodoWrite"
TODO_WRITE_STATUSES = ("pending", "in_progress", "completed")
#: Claude Code's acknowledgement, verbatim.
TODO_WRITE_ACK = (
    "Todos have been modified successfully. Ensure that you continue to use "
    "the todo list to track your progress. Please proceed with the current "
    "tasks if applicable"
)
_TODO_MAX_ITEMS = 50
_TODO_MAX_CONTENT_LEN = 500
_TODO_MAX_ACTIVE_FORM_LEN = 500

ONCE_PER_RESPONSE = (
    "Nothing in this response ran: TodoWrite may be called only "
    "once per response. Re-issue it as a single call carrying the "
    "whole checklist."
)


def _cannot_share(other: str) -> str:
    return (
        "Nothing in this response ran: TodoWrite cannot share a "
        f"response with {other}, and the "
        "checklist was not saved. Re-issue the other calls in a "
        "separate response."
    )


#: 0.6's control tools other than ``Task``: a response that holds one of
#: these beside ``TodoWrite`` runs nothing.
EXCLUSIVE_TOOLS = ("AskUserQuestion", "skill", "RecallHistory", "structured_output")

#: Claude Code's cadence (2.1.300 ``TURNS_SINCE_WRITE`` /
#: ``TURNS_BETWEEN_REMINDERS``): a "turn" is one assistant content block.
TODO_TURNS_SINCE_WRITE = 10
TODO_TURNS_BETWEEN_REMINDERS = 10

#: Claude Code's ``todo_reminder`` text, verbatim.
_TODO_REMINDER_HEAD = (
    "The TodoWrite tool hasn't been used recently. If you're working on tasks "
    "that would benefit from tracking progress, consider using the TodoWrite "
    "tool to track progress. Also consider cleaning up the todo list if has "
    "become stale and no longer matches what you are working on. Only use it "
    "if it's relevant to the current work. This is just a gentle reminder - "
    "ignore if not applicable.\n"
)
_TODO_REMINDER_LIST_HEAD = "\nHere are the existing contents of your todo list:\n"


def _description() -> str:
    text = (resources.files(__package__) / "todo_write.md").read_text("utf-8")
    return text.rstrip("\n")


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": (
                    "The full checklist (replace-all). Each item: "
                    "{content, status, activeForm}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Imperative form of the task, e.g. 'Run tests'.",
                        },
                        "status": {"type": "string", "enum": list(TODO_WRITE_STATUSES)},
                        "activeForm": {
                            "type": "string",
                            "description": (
                                "Present-continuous form shown while "
                                "in_progress, e.g. 'Running tests'."
                            ),
                        },
                    },
                    "required": ["content", "status", "activeForm"],
                },
            },
        },
        "required": ["todos"],
    }


def validate_todos(arguments: Any) -> tuple[bool, list[dict[str, Any]] | str]:
    """``(True, normalized)`` or ``(False, error)``; never raises."""
    todos = arguments.get("todos") if isinstance(arguments, dict) else None
    if not isinstance(todos, list):
        return False, "todos must be a list"
    if len(todos) > _TODO_MAX_ITEMS:
        return False, f"too many todos (max {_TODO_MAX_ITEMS})"
    normalized: list[dict[str, Any]] = []
    for item in todos:
        if not isinstance(item, dict):
            return False, "each todo must be an object"
        content = item.get("content")
        status = item.get("status")
        active_form = item.get("activeForm")
        if not isinstance(content, str) or not content:
            return False, "each todo needs non-empty string content"
        if len(content) > _TODO_MAX_CONTENT_LEN:
            return False, f"todo content too long (max {_TODO_MAX_CONTENT_LEN})"
        if status not in TODO_WRITE_STATUSES:
            return False, "todo status must be one of " + ", ".join(TODO_WRITE_STATUSES)
        if not isinstance(active_form, str) or not active_form:
            return False, "each todo needs non-empty string activeForm"
        if len(active_form) > _TODO_MAX_ACTIVE_FORM_LEN:
            return False, f"todo activeForm too long (max {_TODO_MAX_ACTIVE_FORM_LEN})"
        normalized.append({"content": content, "status": status, "activeForm": active_form})
    return True, normalized


def todo_reminder_text(todos: Sequence[Mapping[str, object]]) -> str:
    """Claude Code's reminder for one todo list (empty -> no list part)."""
    listed = "\n".join(
        f"{i}. [{t.get('status', '')}] {t.get('content', '')}"
        for i, t in enumerate(todos, start=1)
    )
    if not listed:
        return _TODO_REMINDER_HEAD
    return f"{_TODO_REMINDER_HEAD}{_TODO_REMINDER_LIST_HEAD}[{listed}]"


def _is_reminder(message: Message) -> bool:
    return (message.role == "user" and message.origin == "system"
            and message.text.startswith(_TODO_REMINDER_HEAD))


def reminder_due(messages: Sequence[Message]) -> bool:
    """Whether ten assistant blocks have passed since the last ``TodoWrite``
    call and since the last reminder, counted over ``messages`` (what the
    model sees: after a compaction, from the boundary)."""
    since_write = since_reminder = 0
    for message in messages:
        if message.role != "assistant":
            if _is_reminder(message):
                since_reminder = 0
            continue
        for block in message.content:
            if isinstance(block, Thinking):
                continue
            since_reminder += 1
            if isinstance(block, ToolUse) and block.name == TODO_WRITE_TOOL:
                since_write = 0
            else:
                since_write += 1
    return (since_write >= TODO_TURNS_SINCE_WRITE
            and since_reminder >= TODO_TURNS_BETWEEN_REMINDERS)


class TodoWriteTool:
    """``TodoWrite``: replace the checklist, record it, acknowledge."""

    name = TODO_WRITE_TOOL
    read_only = False
    clearable = False

    def __init__(self, extension: str = "todo") -> None:
        self.extension = extension
        self.description = _description()
        self.input_schema = _schema()

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        ok, result = validate_todos(input)
        if not ok:
            assert isinstance(result, str)
            return text_output(result, is_error=True)
        ctx.record(self.extension, "todos", result)
        return text_output(TODO_WRITE_ACK, todos=result)


class Todo(Extension):
    """The todo list.

    Arguments a host sets:

    * ``reminder`` — record Claude Code's todo reminder at its 10/10 cadence
      (default on; 0.6 always ran it while ``TodoWrite`` was offered).
    * ``exclusive_with`` — tools that may not share a response with
      ``TodoWrite`` (0.6's control tools other than ``Task``).

    State (``state.ext["todo"]``): the current list, a list of
    ``{content, status, activeForm}``.
    """

    name = "todo"

    def __init__(self, *, reminder: bool = True,
                 exclusive_with: Sequence[str] = EXCLUSIVE_TOOLS) -> None:
        self.reminder = reminder
        self.exclusive_with = frozenset(exclusive_with)
        self._tool = TodoWriteTool(self.name)

    def initial_state(self) -> Any:
        return []

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return list(data) if kind == "todos" else state

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return [self._tool]

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        uses = [c.use for c in view.state.calls]
        todo_uses = [u for u in uses if u.name == TODO_WRITE_TOOL]
        if not todo_uses:
            return None
        if len(todo_uses) != 1:
            return Deny(reason=ONCE_PER_RESPONSE)
        ok, result = validate_todos(todo_uses[0].input)
        if not ok:
            return Deny(reason=str(result))
        others = [u for u in uses if u.name in self.exclusive_with]
        if others:
            return Deny(reason=_cannot_share(others[0].name))
        return None

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        state = view.state
        if (not self.reminder
                or TODO_WRITE_TOOL not in {t.name for t in request.tools}
                # A request with an unrecorded tail (the step-limit round)
                # is left alone: a reminder must sit where it is recorded.
                or len(request.messages) != len(state.messages)
                or not reminder_due(state.messages)):
            return request
        message = user(todo_reminder_text(view.ext_state(self.name) or []), origin="system")
        view.record(self.name, "reminder", message=message)
        messages = [*request.messages, message]
        # The same breakpoints the next request (built from the log) gets.
        points = [len(messages) - 1]
        for i in range(len(messages) - 2, -1, -1):
            if messages[i].role == "user":
                points.insert(0, i)
                break
        return msgspec.structs.replace(request, messages=messages, cache_after=points)
