"""A task's state, folded from its log. No I/O: the same events always give
the same state, which is what makes recovery a matter of re-reading the log."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from noeta.events import (
    Answer,
    Cancel,
    ChildEnded,
    Compacted,
    ExtensionRecorded,
    External,
    InputTaken,
    ModelCalled,
    ModelReplied,
    Recorded,
    Say,
    TaskCreated,
    TaskEnded,
    TimerFired,
    ToolFinished,
    ToolStarted,
    Waiting,
)
from noeta.types import Message, StopReason, Text, ToolResult, ToolUse, Usage

Reducer = Callable[[Any, str, Any], Any]

#: The extension name the core itself records under (continuation notes).
CORE = "core"


@dataclass
class Call:
    use: ToolUse
    started: bool = False
    result: ToolResult | None = None


@dataclass
class TaskState:
    task_id: str = ""
    agent: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None
    root_id: str | None = None
    last_seq: int = 0
    inbox_read: int = 0

    #: The conversation as the model sees it (after compaction).
    messages: list[Message] = field(default_factory=list)
    #: Log seq that produced each entry of ``messages`` (0 for a summary).
    message_seqs: list[int] = field(default_factory=list)

    #: Calls of the latest reply that are not all answered yet, in order.
    calls: list[Call] = field(default_factory=list)
    #: Messages taken mid-batch; appended once the batch's results land.
    held: list[Message] = field(default_factory=list)
    #: A turn is open from the user message until the model ends it.
    turn_open: bool = False
    steps_in_turn: int = 0
    last_stop: StopReason | None = None
    #: Consecutive ``ModelCalled`` without a reply (crash/retry loop guard).
    model_attempts: int = 0
    #: Texts of the replies that make up the current answer: one reply, or a
    #: reply plus its continuations after hitting the output cap.
    answer_parts: list[str] = field(default_factory=list)
    continuing: bool = False

    cancel_requested: str | None = None
    stop_turn_requested: str | None = None
    #: ``next_turn`` messages taken while a turn was open.
    queued: list[Message] = field(default_factory=list)
    answers: dict[str, Answer] = field(default_factory=dict)
    children: dict[str, ChildEnded | None] = field(default_factory=dict)
    signals: list[TimerFired | External] = field(default_factory=list)
    waiting: Waiting | None = None
    ended: TaskEnded | None = None
    compaction: Compacted | None = None

    usage: Usage = field(default_factory=Usage)
    ext: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ queries

    @property
    def open_calls(self) -> list[Call]:
        return [c for c in self.calls if c.result is None]

    @property
    def last_answer(self) -> str:
        return "".join(self.answer_parts)

    def _add(self, message: Message, seq: int) -> None:
        self.messages.append(message)
        self.message_seqs.append(seq)


def new_state(initial_ext: Mapping[str, Any] | None = None) -> TaskState:
    return TaskState(ext=dict(initial_ext or {}))


def apply(state: TaskState, rec: Recorded, reducers: Mapping[str, Reducer]) -> TaskState:
    """Fold one recorded event into ``state`` (in place) and return it."""
    ev = rec.event
    state.last_seq = rec.seq
    if isinstance(ev, TaskCreated):
        state.agent = ev.agent
        state.config = dict(ev.config)
        state.parent_id = ev.parent_id
        state.root_id = ev.root_id
    elif isinstance(ev, InputTaken):
        _take(state, ev, rec.seq)
    elif isinstance(ev, ModelCalled):
        state.model_attempts += 1
        state.steps_in_turn += 1
        state.waiting = None
    elif isinstance(ev, ModelReplied):
        resp = ev.response
        state.model_attempts = 0
        state.usage = state.usage + resp.usage
        state.last_stop = resp.stop_reason
        state._add(resp.message, rec.seq)
        if state.continuing:
            state.answer_parts.append(resp.message.text)
        else:
            state.answer_parts = [resp.message.text]
        state.continuing = False
        state.calls = [Call(use=u) for u in resp.message.tool_uses]
        if not state.calls and resp.stop_reason != "max_tokens":
            _close_turn(state, rec.seq)
    elif isinstance(ev, ToolStarted):
        for c in state.calls:
            if c.use.id == ev.call.id:
                c.started = True
    elif isinstance(ev, ToolFinished):
        for c in state.calls:
            if c.use.id == ev.result.call_id:
                c.result = ev.result
        if state.calls and not state.open_calls:
            results = [c.result for c in state.calls if c.result is not None]
            state._add(Message(role="user", content=list(results)), rec.seq)
            state.calls = []
            for m in state.held:
                state._add(m, rec.seq)
            state.held = []
    elif isinstance(ev, Compacted):
        _compact(state, ev)
    elif isinstance(ev, Waiting):
        state.waiting = ev
    elif isinstance(ev, TaskEnded):
        state.ended = ev
        state.waiting = None
    elif isinstance(ev, ExtensionRecorded):
        if ev.extension == CORE and ev.kind == "continue":
            state.continuing = True
        if ev.extension == CORE and ev.kind == "turn_stopped":
            state.stop_turn_requested = None
            state.calls = []
            state.held = []
            _close_turn(state, rec.seq)
        reducer = reducers.get(ev.extension)
        if reducer is not None:
            state.ext[ev.extension] = reducer(state.ext.get(ev.extension), ev.kind, ev.data)
        if ev.message is not None:
            if state.calls:
                state.held.append(ev.message)
            else:
                state._add(ev.message, rec.seq)
    return state


def fold(
    records: Iterable[Recorded],
    reducers: Mapping[str, Reducer],
    initial_ext: Mapping[str, Any] | None = None,
    state: TaskState | None = None,
) -> TaskState:
    st = state if state is not None else new_state(initial_ext)
    for rec in records:
        apply(st, rec, reducers)
    return st


def _take(state: TaskState, ev: InputTaken, seq: int) -> None:
    state.inbox_read = max(state.inbox_read, ev.through)
    for item in ev.items:
        body = item.body
        if isinstance(body, Say):
            if body.when == "next_turn" and state.turn_open:
                state.queued.append(body.message)
                continue
            if state.calls:
                state.held.append(body.message)
            else:
                state._add(body.message, seq)
            state.turn_open = True
            state.steps_in_turn = 0 if body.when == "next_turn" else state.steps_in_turn
            state.waiting = None
        elif isinstance(body, Cancel):
            if body.turn_only:
                if state.turn_open:
                    state.stop_turn_requested = body.reason or "stopped"
            else:
                state.cancel_requested = body.reason or "cancelled"
        elif isinstance(body, Answer):
            state.answers[body.request_id] = body
        elif isinstance(body, ChildEnded):
            state.children[body.child_id] = body
        else:
            state.signals.append(body)


def _close_turn(state: TaskState, seq: int) -> None:
    state.turn_open = False
    state.model_attempts = 0
    if state.queued:
        for m in state.queued:
            state._add(m, seq)
        state.queued = []
        state.turn_open = True
        state.steps_in_turn = 0


def _compact(state: TaskState, ev: Compacted) -> None:
    keep_from = len(state.message_seqs)
    for i, seq in enumerate(state.message_seqs):
        if seq > ev.through:
            keep_from = i
            break
    tail = state.messages[keep_from:]
    tail_seqs = state.message_seqs[keep_from:]
    summary = Message(role="user", content=[Text(text=ev.summary)], origin="system")
    state.messages = [summary, *ev.kept, *tail]
    state.message_seqs = [0] * (1 + len(ev.kept)) + tail_seqs
    state.compaction = ev
