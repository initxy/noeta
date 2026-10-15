"""Claude Code's todo reminder: when it appears, what it says, and that it
stays put.

Claude Code adds "The TodoWrite tool hasn't been used recently…" once ten
assistant blocks have passed since the last ``TodoWrite`` and since the last
reminder, and records it in the transcript. noeta derives the same placements
from the recorded history on every compose, so a reminder that appeared at the
tail keeps its position and bytes afterwards — the composed prefix before the
newest turn never changes.
"""

from __future__ import annotations

from typing import Any

from noeta.builtins.reminders.impl import (
    TODO_TURNS_BETWEEN_REMINDERS,
    TODO_TURNS_SINCE_WRITE,
    todo_reminder,
    todo_reminder_text,
)
from noeta.client.parts import default_reminder_specs
from noeta.context.composer import ThreeSegmentComposer
from noeta.context.reminders import ReminderRegistry, ReminderSpec, ReminderView
from noeta.protocols.messages import (
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.task import Task, TaskState
from noeta.storage.memory import InMemoryContentStore

_TODO_SCHEMA = {"type": "function", "function": {"name": "TodoWrite", "parameters": {}}}
_LIST = [
    {"content": "plan", "status": "completed", "activeForm": "Planning"},
    {"content": "build", "status": "in_progress", "activeForm": "Building"},
]


def _user(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)])


def _turn(i: int) -> list[Message]:
    """One tool round: an assistant block and its result."""
    return [
        Message(
            role="assistant",
            content=[ToolUseBlock(call_id=f"c{i}", tool_name="Read", arguments={})],
        ),
        Message(
            role="tool",
            content=[ToolResultBlock(call_id=f"c{i}", output="x", success=True)],
        ),
    ]


def _todo_write(call_id: str, todos: list[dict[str, Any]], *, ok: bool = True) -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(call_id=call_id, tool_name="TodoWrite", arguments={"todos": todos})
            ],
        ),
        Message(
            role="tool",
            content=[ToolResultBlock(call_id=call_id, output="ok", success=ok)],
        ),
    ]


def _view(history: list[Message], **kw: Any) -> ReminderView:
    return ReminderView(history=tuple(history), tool_names=("Read", "TodoWrite"), **kw)


def test_cadence_is_claude_codes() -> None:
    assert TODO_TURNS_SINCE_WRITE == 10
    assert TODO_TURNS_BETWEEN_REMINDERS == 10


def test_text_is_claude_codes() -> None:
    assert todo_reminder_text(_LIST) == (
        "The TodoWrite tool hasn't been used recently. If you're working on tasks "
        "that would benefit from tracking progress, consider using the TodoWrite "
        "tool to track progress. Also consider cleaning up the todo list if has "
        "become stale and no longer matches what you are working on. Only use it "
        "if it's relevant to the current work. This is just a gentle reminder - "
        "ignore if not applicable.\n"
        "\nHere are the existing contents of your todo list:\n"
        "[1. [completed] plan\n2. [in_progress] build]"
    )
    assert todo_reminder_text([]).endswith("ignore if not applicable.\n")


def test_fires_every_ten_blocks_without_a_todo_write() -> None:
    history = [_user("go")]
    for i in range(25):
        history += _turn(i)
    placements = todo_reminder(_view(history))
    # Before the 11th and 21st assistant message: indices 1 + 2*10, 1 + 2*20.
    assert [index for index, _ in placements] == [21, 41]
    assert all(text == todo_reminder_text([]) for _, text in placements)


def test_nothing_before_ten_blocks_and_nothing_without_the_tool() -> None:
    history = [_user("go")]
    for i in range(9):
        history += _turn(i)
    assert todo_reminder(_view(history)) == ()
    for i in range(9, 20):
        history += _turn(i)
    assert todo_reminder(ReminderView(history=tuple(history), tool_names=("Read",))) == ()


def test_todo_write_resets_and_its_list_rides_the_reminder() -> None:
    history = [_user("go")]
    for i in range(8):
        history += _turn(i)
    history += _todo_write("w1", _LIST)
    for i in range(8, 18):
        history += _turn(i)
    placements = todo_reminder(_view(history))
    assert placements == ((len(history), todo_reminder_text(_LIST)),)


def test_a_rejected_todo_write_resets_but_writes_no_list() -> None:
    history = [_user("go"), *_todo_write("w1", _LIST)]
    history += _todo_write("w2", [{"content": "bad", "status": "?"}], ok=False)
    for i in range(10):
        history += _turn(i)
    assert todo_reminder(_view(history)) == ((len(history), todo_reminder_text(_LIST)),)


def test_thinking_blocks_do_not_count() -> None:
    history = [_user("go")]
    for i in range(10):
        turn = _turn(i)
        turn[0] = Message(
            role="assistant",
            content=[ThinkingBlock(text="hm"), *turn[0].content],
        )
        history += turn
    assert [i for i, _ in todo_reminder(_view(history))] == [len(history)]


def test_counting_restarts_at_the_compaction_boundary() -> None:
    history = [_user("go"), *_todo_write("w1", _LIST)]
    for i in range(30):
        history += _turn(i)
    boundary = 41
    placements = todo_reminder(_view(history, summary_boundary=boundary))
    # Ten blocks past the boundary; the list comes from the collapsed prefix.
    assert placements[0] == (boundary + 20, todo_reminder_text(_LIST))


def _composer() -> ThreeSegmentComposer:
    return ThreeSegmentComposer(
        system_prompt="sys",
        tools={},
        content_store=InMemoryContentStore(),
        control_action_schemas=[_TODO_SCHEMA],
        reminders=ReminderRegistry(default_reminder_specs()),
    )


def test_a_placed_reminder_keeps_its_place_and_bytes() -> None:
    composer = _composer()
    task = Task(task_id="t", state=TaskState())
    task.runtime.messages.append(_user("go"))
    for i in range(10):
        task.runtime.messages.extend(_turn(i))

    first = composer.compose(task).segments[2].content
    reminder = first[-1]
    assert reminder.origin == "system"
    assert reminder.content == [TextBlock(text=todo_reminder_text([]))]
    assert reminder not in task.runtime.messages

    task.runtime.messages.extend(_turn(10))
    second = composer.compose(task).segments[2].content
    # Everything the first request carried is the second request's prefix.
    assert second[: len(first)] == first
    assert sum(m.origin == "system" for m in second) == 1


def test_registry_orders_placements_by_index() -> None:
    registry = ReminderRegistry(
        (
            ReminderSpec("b", 2, place=lambda v: ((5, "b5"), (1, "b1"))),
            ReminderSpec("a", 1, place=lambda v: ((5, "a5"),)),
            ReminderSpec("tail", 0, lambda v: "tail"),
        )
    )
    view = ReminderView()
    assert registry.place_all(view) == [(1, "b1"), (5, "a5"), (5, "b5")]
    assert registry.render_all(view) == ["tail"]


def test_spec_takes_exactly_one_of_render_and_place() -> None:
    import pytest

    with pytest.raises(ValueError):
        ReminderSpec("x", 1)
    with pytest.raises(ValueError):
        ReminderSpec("x", 1, lambda v: None, place=lambda v: ())
