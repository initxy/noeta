"""Claude Code's task list: ``TaskCreate`` / ``TaskGet`` / ``TaskUpdate`` /
``TaskList`` (deferred) and the reminder after ten model turns without them.

As in Claude Code 2.1.312, only the Claude models that predate its lean
prompt (:data:`OLDER_MODELS`, by Claude Code's canonical name) and Bedrock
application inference profiles get them by default; the Claude 5 family and
every other model (DeepSeek, a gateway's routing name) get none.
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
from dataclasses import replace
from typing import Any

from noeta.core import models
from noeta.core.extension import ContextPart, Extension, Reminder, TaskView
from noeta.core.tool import Tool
from noeta.core.types import Message, Thinking, ToolUse
from noeta.plugins.todos.state import TodoState, reduce_todos
from noeta.plugins.todos.tools import CREATE, NAME, UPDATE, task_tools

__all__ = ["OLDER_MODELS", "REMINDER", "TURNS", "Todos", "canonical", "gets_task_tools",
           "turns_since"]

#: Claude Code 2.1.312's models that get the task tools (its list of the
#: models older than the lean prompt), by canonical name (:func:`canonical`).
OLDER_MODELS = ("claude-3-opus", "claude-3-sonnet", "claude-3-haiku", "claude-3-5-sonnet",
                "claude-3-5-haiku", "claude-3-7-sonnet", "claude-opus-4-0", "claude-opus-4-1",
                "claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-7", "claude-sonnet-4-0",
                "claude-sonnet-4-5", "claude-sonnet-4-6", "claude-haiku-4-5")
#: Claude Code's canonical names, checked in its order: the first name the id
#: contains (a pattern: matches) is the model's.
_CANONICAL: tuple[str | tuple[re.Pattern[str], str], ...] = (
    "claude-fable-5-1", "claude-fable-5", "claude-mythos-5-1", "claude-mythos-5",
    "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-opus-4-6", "claude-opus-4-5", "claude-opus-4-1",
    (re.compile(r"claude-opus-4(?!-\d(?!\d))"), "claude-opus-4-0"),
    "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4-5",
    (re.compile(r"claude-sonnet-4(?!-\d(?!\d))"), "claude-sonnet-4-0"),
    "claude-haiku-5-5", "claude-haiku-4-5", "claude-3-7-sonnet", "claude-3-5-sonnet",
    "claude-3-5-haiku", "claude-3-opus", "claude-3-sonnet", "claude-3-haiku")
_DATE = re.compile(r"-\d{8}$")
#: Model turns (non-thinking content blocks) without a task tool, and since
#: the last reminder, that earn a reminder (Claude Code's 10 and 10).
TURNS = 10
#: Its place in a joined system turn: after the turn notes, before the budget.
ORDER = 72
#: The reminder's record kind.
REMINDER_KIND = "task_reminder"

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


def canonical(model: str) -> str:
    """Claude Code's canonical name of ``model`` (its ``kK``): the first of
    its Claude names the id contains, else the id without a date suffix."""
    for entry in _CANONICAL:
        if isinstance(entry, str):
            if entry in model:
                return entry
        elif entry[0].search(model):
            return entry[1]
    return _DATE.sub("", model)


def gets_task_tools(model: str) -> bool:
    """Whether Claude Code 2.1.312 gives ``model`` the task tools: an older
    Claude model or a Bedrock application inference profile (an alias
    resolves first)."""
    name = canonical(models.resolve_alias(model))
    return "application-inference-profile" in name or name in OLDER_MODELS


def _blocks(messages: Sequence[Message]) -> int:
    """Model turns in ``messages``: non-thinking content blocks of replies
    (Claude Code keeps each block of a reply as its own message)."""
    return sum(1 for m in messages if m.role == "assistant"
               for b in m.content if not isinstance(b, Thinking))


def turns_since(messages: Sequence[Message], seqs: Sequence[int],
                reminded_at: int, compacted_through: int | None = None) -> tuple[int, int]:
    """Model turns since the last ``TaskCreate`` / ``TaskUpdate`` call in
    ``messages`` (the conversation as the model sees it, ``seqs`` their log
    seqs), and since the last reminder, given after log seq ``reminded_at``
    (``-1``: never). Neither count reaches back past a compaction, as Claude
    Code counts in the compacted conversation: a reminder the compaction
    through ``compacted_through`` folded away is no longer there, so every
    turn counts, those it kept (at seq 0) too."""
    since_use = 0
    for m in reversed(messages):
        if m.role != "assistant":
            continue
        for block in reversed(m.content):
            if isinstance(block, Thinking):
                continue
            if isinstance(block, ToolUse) and block.name in (CREATE, UPDATE):
                break
            since_use += 1
        else:
            continue
        break
    gone = compacted_through is not None and 0 < reminded_at <= compacted_through
    since_reminder = _blocks(messages) if reminded_at < 0 or gone else _blocks(
        [m for m, s in zip(messages, seqs, strict=True) if s > reminded_at])
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
        if kind == REMINDER_KIND:
            st = state if isinstance(state, TodoState) else TodoState()
            return replace(st, reminded_at=int((data or {}).get("seq", 0)))
        return reduce_todos(state, kind, data)

    def continue_state(self, state: Any) -> Any:
        """The reminder's seq is the source log's; the carried messages sit
        at seq 0, before any new turn."""
        if isinstance(state, TodoState) and state.reminded_at > 0:
            return replace(state, reminded_at=0)
        return state

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
        st = view.ext_state(NAME)
        st = st if isinstance(st, TodoState) else TodoState()
        compaction = view.state.compaction
        since_use, since_reminder = turns_since(
            view.state.messages, view.state.message_seqs, st.reminded_at,
            compaction.through if compaction is not None else None)
        if since_use < TURNS or since_reminder < TURNS:
            return ()
        text = view.prompt(f"reminder/{NAME}", REMINDER)
        items = st.items
        if items:
            text += REMINDER_LIST + "\n".join(f"#{i.id}. [{i.status}] {i.subject}"
                                              for i in items)
        # Where it is given is recorded with it (``reduce``), so the next
        # count needs no text to find it by.
        return (Reminder(text=text, kind=REMINDER_KIND, role="system", order=ORDER,
                         data={"seq": view.state.last_seq}),)


def _offered(view: TaskView, name: str) -> bool:
    """Whether the agent's tool lists let ``name`` through."""
    allowed = view.allowed_tools
    return (allowed is None or name in allowed) and name not in view.denied_tools
