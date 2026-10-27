"""The task log's event vocabulary and the inbox's message kinds.

Only the runner that holds a task's claim appends events. Everyone else posts
to the task's inbox; the runner records what it took with ``InputTaken``.
Extensions never add event types: they record ``ExtensionRecorded``.
"""

from __future__ import annotations

from typing import Any, Literal, Union

import msgspec

from noeta.types import Message, ModelResponse, Struct, ToolResult, ToolUse, Usage

# --------------------------------------------------------------------------
# Inbox
# --------------------------------------------------------------------------


class Say(Struct, tag="message", tag_field="kind"):
    """A message for the agent. ``now`` is taken at the next step boundary,
    mid-turn; ``next_turn`` waits until the current turn has ended."""

    message: Message
    when: Literal["now", "next_turn"] = "next_turn"


class Cancel(Struct, tag="cancel", tag_field="kind"):
    """End the task, or with ``turn_only`` just stop the current turn (the
    task then waits for the next message)."""

    reason: str = ""
    turn_only: bool = False


class Answer(Struct, tag="answer", tag_field="kind"):
    """A reply to something the task is waiting on: an approval (``allow`` /
    ``deny``, optionally ``always``) or a question (``data``)."""

    request_id: str
    decision: Literal["allow", "deny"] | None = None
    always: bool = False
    data: Any = None


class ChildEnded(Struct, tag="child_ended", tag_field="kind"):
    child_id: str
    status: Literal["done", "failed", "cancelled"]
    answer: str = ""
    error: str = ""


class TimerFired(Struct, tag="timer", tag_field="kind"):
    name: str


class External(Struct, tag="external", tag_field="kind"):
    name: str
    data: Any = None


InboxKind = Union[Say, Cancel, Answer, ChildEnded, TimerFired, External]


class InboxItem(Struct):
    seq: int
    at: float
    body: InboxKind


# --------------------------------------------------------------------------
# Task log events
# --------------------------------------------------------------------------


class TaskCreated(Struct, tag="TaskCreated", tag_field="type"):
    agent: str
    config: dict[str, Any] = msgspec.field(default_factory=dict)
    parent_id: str | None = None
    root_id: str | None = None


class InputTaken(Struct, tag="InputTaken", tag_field="type"):
    """The inbox items the runner took, in inbox order. ``through`` is the last
    inbox seq taken; everything up to it is consumed exactly once."""

    items: list[InboxItem]
    through: int


class ModelCalled(Struct, tag="ModelCalled", tag_field="type"):
    model: str
    request_hash: str
    attempt: int = 1


class ModelReplied(Struct, tag="ModelReplied", tag_field="type"):
    response: ModelResponse


class ToolStarted(Struct, tag="ToolStarted", tag_field="type"):
    call: ToolUse


class ToolFinished(Struct, tag="ToolFinished", tag_field="type"):
    result: ToolResult
    #: Data for front ends (a diff, an exit code); not sent to the model.
    meta: dict[str, Any] = msgspec.field(default_factory=dict)


class Compacted(Struct, tag="Compacted", tag_field="type"):
    """History up to and including event ``through`` is replaced by
    ``summary`` (plus whatever ``kept`` re-attaches) when building requests."""

    summary: str
    through: int
    kept: list[Message] = msgspec.field(default_factory=list)
    usage: Usage = msgspec.field(default_factory=Usage)


WaitOn = Literal["input", "approval", "question", "children", "timer", "external"]


class Waiting(Struct, tag="Waiting", tag_field="type"):
    on: WaitOn
    #: approval / question: request ids; children: child task ids.
    ids: list[str] = msgspec.field(default_factory=list)
    until: float | None = None
    detail: dict[str, Any] = msgspec.field(default_factory=dict)


class TaskEnded(Struct, tag="TaskEnded", tag_field="type"):
    status: Literal["done", "failed", "cancelled"]
    answer: str = ""
    error: str = ""


class ExtensionRecorded(Struct, tag="ExtensionRecorded", tag_field="type"):
    """An extension's own durable entry, folded by that extension. With
    ``message`` the core also appends the message to the conversation (a
    reminder, a notice, a recalled memory)."""

    extension: str
    kind: str
    data: Any = None
    message: Message | None = None


Event = Union[
    TaskCreated,
    InputTaken,
    ModelCalled,
    ModelReplied,
    ToolStarted,
    ToolFinished,
    Compacted,
    Waiting,
    TaskEnded,
    ExtensionRecorded,
]


class Recorded(Struct):
    """An event as stored: its position in the task log and when it landed."""

    seq: int
    at: float
    event: Event


def event_type(event: Event) -> str:
    return type(event).__struct_config__.tag  # type: ignore[return-value]
