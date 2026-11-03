"""Claude Code's task list: ``TaskCreate`` / ``TaskGet`` / ``TaskUpdate`` /
``TaskList`` (deferred) and the reminder after ten model turns without them.

As in Claude Code 2.1.312, only the models that predate its lean prompt
(:data:`OLDER_MODELS`) and models it does not know (any model that is not a
Claude model, such as DeepSeek) get them; the Claude 5 family gets none.
``all_models = true`` gives every model the tools (Claude Code's
``CLAUDE_CODE_ENABLE_TODO_TOOLS``); ``enabled = false``, the switch every
plugin has, takes them from every model.

The list is a fold of the task's log (:mod:`noeta.plugins.todos.state`), so
it is the same on any worker, survives a compaction, and a task that
continues this conversation (a fork, a rewind) starts with it. Only a task
without a parent gets the tools: noeta's sub-agents run in the background,
and Claude Code's background agents have no task tools.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from noeta.core import models
from noeta.core.extension import ContextPart, Extension, Reminder, TaskView
from noeta.core.tool import Tool
from noeta.core.types import Message, Thinking, ToolUse
from noeta.plugins.todos.state import TodoState, reduce_todos
from noeta.plugins.todos.tools import CREATE, NAME, UPDATE, task_tools

__all__ = ["OLDER_MODELS", "REMINDER", "TURNS", "Todos", "gets_task_tools", "turns_since"]

#: Claude Code 2.1.312's models that get the task tools (its list of the
#: models older than the lean prompt), matched in the id as Claude Code
#: matches its canonical names (``claude-sonnet-4-5-20250929``,
#: ``us.anthropic.claude-haiku-4-5-...``).
OLDER_MODELS = ("claude-3-opus", "claude-3-sonnet", "claude-3-haiku", "claude-3-5-sonnet",
                "claude-3-5-haiku", "claude-3-7-sonnet", "claude-opus-4-0", "claude-opus-4-1",
                "claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-7", "claude-sonnet-4-0",
                "claude-sonnet-4-5", "claude-sonnet-4-6", "claude-haiku-4-5")
#: A Claude model Claude Code knows by name: one not in :data:`OLDER_MODELS`
#: gets no task tools.
_CLAUDE = re.compile(r"claude-(?:\d|opus|sonnet|haiku|fable|mythos)")
#: Model turns (non-thinking content blocks) without a task tool, and since
#: the last reminder, that earn a reminder (Claude Code's 10 and 10).
TURNS = 10
#: Its place in a joined system turn: after the turn notes, before the budget.
ORDER = 72

REMINDER = (
    "The task tools haven't been used recently. If you're working on tasks that would "
    "benefit from tracking progress, consider using TaskCreate to add new tasks and "
    "TaskUpdate to update task status (set to in_progress when starting, completed when "
    "done). Also consider cleaning up the task list if it has become stale. Only use these "
    "if relevant to the current work. This is just a gentle reminder - ignore if not "
    "applicable.\n")
REMINDER_LIST = "\nHere are the existing tasks:\n"
#: The line Claude Code's prompt for these models has on the task tools.
PROMPT_LINE = ("Use TaskCreate to plan and track work. Mark each task completed as soon as "
               "it's done; don't batch.")
_PROMPT_BEFORE = "# Session-specific guidance"


def gets_task_tools(model: str) -> bool:
    """Whether Claude Code 2.1.312 gives ``model`` the task tools: an older
    Claude model, a Bedrock application inference profile, or any model
    that is not a Claude model it knows (an alias resolves first)."""
    real = models.resolve_alias(model).lower()
    if "application-inference-profile" in real or any(m in real for m in OLDER_MODELS):
        return True
    return _CLAUDE.search(real) is None


def turns_since(messages: Sequence[Message], reminder: str) -> tuple[int, int]:
    """Model turns since the last ``TaskCreate`` / ``TaskUpdate`` call, and
    since the last reminder (whose text starts with ``reminder``'s first
    line). A turn is one non-thinking content block of a reply, as Claude
    Code keeps each block of a reply as its own message."""
    head = reminder.split("\n", 1)[0]
    since_use = since_reminder = 0
    used = reminded = False
    for m in reversed(messages):
        if m.role == "assistant":
            for block in reversed(m.content):
                if isinstance(block, Thinking):
                    continue
                if not used and isinstance(block, ToolUse) and block.name in (CREATE, UPDATE):
                    used = True
                if not used:
                    since_use += 1
                if not reminded:
                    since_reminder += 1
        elif not reminded and m.text.startswith(head):
            reminded = True
        if used and reminded:
            break
    return since_use, since_reminder


class Todos(Extension):
    """``all_models`` — offer the tools to every model, not only the ones
    Claude Code gives them to."""

    name = NAME

    def __init__(self, *, all_models: bool = False) -> None:
        self.all_models = all_models

    def initial_state(self) -> TodoState:
        return TodoState()

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return reduce_todos(state, kind, data)

    def active(self, view: TaskView) -> bool:
        """Whether this task gets the task tools."""
        if view.state.parent_id is not None:
            return False
        return self.all_models or gets_task_tools(
            str(view.state.config.get("model") or view.model or ""))

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return task_tools() if self.active(view) else ()

    def context(self, view: TaskView) -> Sequence[ContextPart]:
        if not self.active(view):
            return ()
        return (ContextPart(text=view.prompt(f"{NAME}/prompt_line", PROMPT_LINE),
                            before=_PROMPT_BEFORE),)

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        if not self.active(view) or not view.state.messages or not _offered(view, UPDATE):
            return ()
        text = view.prompt(f"reminder/{NAME}", REMINDER)
        since_use, since_reminder = turns_since(view.state.messages, text)
        if since_use < TURNS or since_reminder < TURNS:
            return ()
        st = view.ext_state(NAME)
        items = st.items if isinstance(st, TodoState) else ()
        if items:
            text += REMINDER_LIST + "\n".join(f"#{i.id}. [{i.status}] {i.subject}"
                                              for i in items)
        return (Reminder(text=text, kind="task_reminder", role="system", order=ORDER),)


def _offered(view: TaskView, name: str) -> bool:
    """Whether the agent's tool lists let ``name`` through."""
    allowed = view.allowed_tools
    return (allowed is None or name in allowed) and name not in view.denied_tools
