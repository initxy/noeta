"""The two built-in compose-time reminders.

Each is a pure function of the narrow
:class:`~noeta.context.reminders.ReminderView` projection — no clock, no
randomness, no external fetch — so recomposing a turn yields the same bytes.
The registry *mechanism* stays in ``noeta.context.reminders``; the parent
manifest declares these renders by ``ref``, and the kernel never imports one.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from noeta.context.reminders import ReminderSpec, ReminderView
from noeta.protocols.messages import (
    Message,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)


__all__ = [
    "BUILTIN_REMINDER_PRIORITIES",
    "TODO_TURNS_BETWEEN_REMINDERS",
    "TODO_TURNS_SINCE_WRITE",
    "default_reminder_specs",
    "todo_reminder",
    "todo_reminder_text",
    "read_suggestion_reminder",
]


#: Claude Code's todo-reminder cadence (2.1.300 ``TURNS_SINCE_WRITE`` /
#: ``TURNS_BETWEEN_REMINDERS``). A "turn" is one assistant content block, the
#: unit Claude Code's transcript stores one message per; thinking never counts.
TODO_TURNS_SINCE_WRITE = 10
TODO_TURNS_BETWEEN_REMINDERS = 10

#: The todo control tool's name, as the model sees it.
_TODO_WRITE = "TodoWrite"

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


def todo_reminder_text(todos: Sequence[Mapping[str, object]]) -> str:
    """Claude Code's reminder for one todo list (empty → no list part)."""
    listed = "\n".join(
        f"{i}. [{t.get('status', '')}] {t.get('content', '')}"
        for i, t in enumerate(todos, start=1)
    )
    if not listed:
        return _TODO_REMINDER_HEAD
    return f"{_TODO_REMINDER_HEAD}{_TODO_REMINDER_LIST_HEAD}[{listed}]"


def todo_reminder(view: ReminderView) -> tuple[tuple[int, str], ...]:
    """Where Claude Code's todo reminder stands in the history.

    Claude Code checks before every model request whether ten assistant
    blocks have passed both since the last ``TodoWrite`` call and since the
    last reminder, and if so records a reminder carrying the todo list as it
    is at that moment. This replays that walk over what the model still sees
    (the history past the compaction boundary): every request point — just
    before each assistant message, and the tail — where the counters reached
    the cadence gets the reminder, its list being what the last accepted
    ``TodoWrite`` before that point wrote. Every input is recorded history, so
    a reminder keeps its position and bytes on every later compose.

    Offered only while ``TodoWrite`` is offered.
    """
    if _TODO_WRITE not in view.tool_names:
        return ()
    history = view.history
    start = min(max(0, view.summary_boundary), len(history))
    accepted = _accepted_call_ids(history)
    todos = _todos_before(history, start, accepted)
    since_write = since_reminder = 0
    placements: list[tuple[int, str]] = []
    for index in range(start, len(history) + 1):
        message = history[index] if index < len(history) else None
        if message is not None and message.role != "assistant":
            continue
        if (
            since_write >= TODO_TURNS_SINCE_WRITE
            and since_reminder >= TODO_TURNS_BETWEEN_REMINDERS
        ):
            placements.append((index, todo_reminder_text(todos)))
            since_reminder = 0
        if message is None:
            break
        for block in message.content:
            if isinstance(block, ThinkingBlock):
                continue
            since_reminder += 1
            if isinstance(block, ToolUseBlock) and block.tool_name == _TODO_WRITE:
                since_write = 0
                written = _written_todos(block, accepted)
                if written is not None:
                    todos = written
            else:
                since_write += 1
    return tuple(placements)


def _accepted_call_ids(history: Sequence[Message]) -> set[str]:
    """Call ids whose result succeeded — a rejected ``TodoWrite`` wrote no
    list."""
    return {
        block.call_id
        for message in history
        if message.role == "tool"
        for block in message.content
        if isinstance(block, ToolResultBlock) and block.success
    }


def _written_todos(
    block: ToolUseBlock, accepted: set[str]
) -> Optional[tuple[Mapping[str, object], ...]]:
    if block.call_id not in accepted:
        return None
    todos = block.arguments.get("todos")
    if not isinstance(todos, list):
        return None
    return tuple(t for t in todos if isinstance(t, dict))


def _todos_before(
    history: Sequence[Message], end: int, accepted: set[str]
) -> tuple[Mapping[str, object], ...]:
    """The list the last accepted ``TodoWrite`` in ``history[:end]`` wrote."""
    for message in reversed(history[:end]):
        if message.role != "assistant":
            continue
        for block in reversed(message.content):
            if isinstance(block, ToolUseBlock) and block.tool_name == _TODO_WRITE:
                written = _written_todos(block, accepted)
                if written is not None:
                    return written
    return ()


def read_suggestion_reminder(view: ReminderView) -> Optional[str]:
    """Suggest a different read strategy while compaction is thrashing.

    Live only while ``ContextState.compaction_thrashing`` is latched.
    """
    if not view.compaction_thrashing:
        return None
    return (
        "The context window keeps getting refilled to the limit by what "
        "looks like a single large file or large tool output, so compaction "
        "is spinning without freeing real headroom. Consider a different "
        "reading strategy: read in chunks, read only the relevant section, "
        "or extract the key points once and re-read on demand instead of "
        "pulling the whole large content back into context each time."
    )


#: Name -> priority, chosen so the composed order is todo -> read and spread
#: by 100 so third-party reminders can interleave. The manifest declarations
#: must carry the same numbers. Band 200 is free: it belonged to
#: ``delegation-nudge``, which fired on every step of every task until the
#: first spawn to repeat what main rule 10 and the ``Task`` description
#: already say, and was removed rather than renumbered so the two surviving
#: bands keep their positions.
BUILTIN_REMINDER_PRIORITIES: dict[str, int] = {
    "todo-reminder": 100,
    "read-suggestion": 300,
}


def default_reminder_specs() -> tuple[ReminderSpec, ...]:
    """The two built-in reminders as specs — the impl-side equivalent of
    resolving the manifest, for a caller that already holds this module.
    """
    return (
        ReminderSpec(
            "todo-reminder",
            BUILTIN_REMINDER_PRIORITIES["todo-reminder"],
            place=todo_reminder,
        ),
        ReminderSpec(
            "read-suggestion",
            BUILTIN_REMINDER_PRIORITIES["read-suggestion"],
            read_suggestion_reminder,
        ),
    )
