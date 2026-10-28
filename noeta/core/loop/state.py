"""A task's state, folded from its log. No I/O: the same events always give
the same state, which is what makes recovery a matter of re-reading the log."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from noeta.core.events import (
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
from noeta.core.prompts import resolve
from noeta.core.types import Message, StopReason, Text, ToolResult, ToolUse, Usage

Reducer = Callable[[Any, str, Any], Any]

#: The extension name the core itself records under (continuation notes).
CORE = "core"


@dataclass
class Call:
    use: ToolUse
    started: bool = False
    result: ToolResult | None = None
    #: Set while the call waits (``{"on", "keys", "detail"}``).
    parked: dict[str, Any] | None = None
    #: The call as it ran, when an extension rewrote it (``ToolStarted``).
    ran: ToolUse | None = None


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
    #: Every message ever added, in order, never compacted (and its log seq):
    #: what a compaction collapsed stays readable here (``RecallHistory``).
    history: list[Message] = field(default_factory=list)
    history_seqs: list[int] = field(default_factory=list)
    #: Usage of the newest model reply; ``None`` before the first one and
    #: after a compaction (no recorded count describes the collapsed history).
    last_usage: Usage | None = None

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
    #: The newest reply was text cut at the output cap, so the next reply
    #: continues it (its text joins ``answer_parts``).
    continuing: bool = False
    #: Every tool-call id this task has recorded: ids are unique per task.
    call_ids: set[str] = field(default_factory=set)
    #: A call of the open batch ended the turn (``ToolFinished.end_turn``):
    #: the turn's answer once the batch's results land.
    end_turn: str | None = None
    #: A turn-only stop arrived with ``now`` messages: the turn reopens once
    #: the stop has landed, so those messages are answered.
    reopen_after_stop: bool = False
    #: The batch's results carried held messages into the conversation and
    #: no model call has seen them yet: a stop or failure closing the turn
    #: now reopens it, so they are answered.
    held_unseen: bool = False

    cancel_requested: str | None = None
    #: Why the last turn failed (``reason: detail``); cleared by a new turn.
    turn_error: str | None = None
    stop_turn_requested: str | None = None
    #: ``next_turn`` messages taken while a turn was open.
    queued: list[Message] = field(default_factory=list)
    #: Answers to the open batch's approvals and questions, by call id.
    answers: dict[str, Answer] = field(default_factory=dict)
    #: Children this task spawned: id -> {"call_id", "background"}.
    spawned: dict[str, dict[str, Any]] = field(default_factory=dict)
    children: dict[str, ChildEnded | None] = field(default_factory=dict)
    signals: list[TimerFired | External] = field(default_factory=list)
    waiting: Waiting | None = None
    ended: TaskEnded | None = None
    compaction: Compacted | None = None
    #: Prompt override files found when the task first started (name ->
    #: text, see ``noeta.core.prompts``); ``None`` until they were looked for.
    prompt_files: dict[str, str] | None = None

    usage: Usage = field(default_factory=Usage)
    ext: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ queries

    def prompt(self, name: str, default: str) -> str:
        """The text of the prompt ``name`` for this task: ``default`` unless
        an override file (recorded at start) or the config's ``prompts``
        replaces or appends to it."""
        overrides = dict(self.prompt_files or {})
        configured = self.config.get("prompts")
        if isinstance(configured, dict):
            overrides.update({str(k): v for k, v in configured.items() if isinstance(v, str)})
        return resolve(overrides, name, default) if overrides else default

    @property
    def open_calls(self) -> list[Call]:
        return [c for c in self.calls if c.result is None]

    def parked_ready(self, call: Call) -> bool:
        """Whether what a parked call waits for has arrived."""
        p = call.parked or {}
        if p.get("on") == "question":
            return call.use.id in self.answers
        if p.get("on") == "children":
            return all(self.children.get(k) is not None for k in p.get("keys") or [])
        return False

    def live_children(self) -> list[str]:
        """Spawned children that have not reported their end."""
        return [c for c in self.spawned if self.children.get(c) is None]

    @property
    def last_answer(self) -> str:
        return "".join(self.answer_parts)

    def _add(self, message: Message, seq: int) -> None:
        self.messages.append(message)
        self.message_seqs.append(seq)
        self.history.append(message)
        self.history_seqs.append(seq)


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
        state.held_unseen = False
    elif isinstance(ev, ModelReplied):
        resp = ev.response
        state.model_attempts = 0
        state.usage = state.usage + resp.usage
        state.last_usage = resp.usage
        state.last_stop = resp.stop_reason
        state._add(resp.message, rec.seq)
        if state.continuing:
            state.answer_parts.append(resp.message.text)
        else:
            state.answer_parts = [resp.message.text]
        uses = resp.message.tool_uses
        state.continuing = resp.stop_reason == "max_tokens" and not uses
        state.calls = [Call(use=u) for u in uses]
        state.call_ids.update(u.id for u in uses)
        # An answer is for a call of the batch it was asked in, never a later one.
        state.answers = {}
        if not state.calls and resp.stop_reason != "max_tokens":
            _close_turn(state, rec.seq)
    elif isinstance(ev, ToolStarted):
        for c in state.calls:
            if c.use.id == ev.call.id:
                c.started = True
                if ev.call != c.use:
                    c.ran = ev.call
    elif isinstance(ev, ToolFinished):
        for c in state.calls:
            if c.use.id == ev.result.call_id:
                c.result = ev.result
        if ev.end_turn is not None:
            state.end_turn = ev.end_turn
        if state.calls and not state.open_calls:
            results = [c.result for c in state.calls if c.result is not None]
            state._add(Message(role="user", content=list(results)), rec.seq)
            state.calls = []
            state.held_unseen = state.held_unseen or bool(state.held)
            for m in state.held:
                state._add(m, rec.seq)
            state.held = []
            if state.end_turn is not None:
                state.answer_parts = [state.end_turn]
                state.end_turn = None
                _close_turn(state, rec.seq)
    elif isinstance(ev, Compacted):
        _compact(state, ev)
    elif isinstance(ev, Waiting):
        state.waiting = ev
    elif isinstance(ev, TaskEnded):
        state.ended = ev
        state.waiting = None
    elif isinstance(ev, ExtensionRecorded):
        if ev.extension == CORE and ev.kind == "turn_stopped":
            state.stop_turn_requested = None
            _end_turn(state, rec.seq)
        if ev.extension == CORE and ev.kind == "parked":
            for c in state.calls:
                if c.use.id == ev.data["call_id"]:
                    c.parked = {k: ev.data.get(k) for k in ("on", "keys", "detail")}
        if ev.extension == CORE and ev.kind == "spawned":
            for child in ev.data["children"]:
                state.spawned[child["id"]] = child
        if ev.extension == CORE and ev.kind == "prompts":
            data = ev.data if isinstance(ev.data, dict) else {}
            state.prompt_files = {str(k): v for k, v in data.items() if isinstance(v, str)}
        if ev.extension == CORE and ev.kind == "turn_failed":
            data = ev.data if isinstance(ev.data, dict) else {}
            reason, detail = data.get("reason", "failed"), data.get("detail", "")
            state.turn_error = f"{reason}: {detail}" if detail else str(reason)
            _end_turn(state, rec.seq)
        reducer = reducers.get(ev.extension)
        if reducer is not None:
            state.ext[ev.extension] = reducer(state.ext.get(ev.extension), ev.kind, ev.data)
        if ev.message is not None:
            if state.calls:
                state.held.append(ev.message)
            else:
                state._add(ev.message, rec.seq)
    return state


def continued(state: TaskState) -> TaskState:
    """``state`` (another task's, folded) as the start of a task that
    continues its conversation: messages and extension state stay; what
    belongs to that task's run (its log positions, inbox, children, open
    turn) does not. Calls still open there are answered here with
    :data:`CONTINUED_CALL` so the conversation stays well formed."""
    out = TaskState(ext=dict(state.ext), prompt_files=state.prompt_files,
                    compaction=state.compaction, last_usage=state.last_usage,
                    call_ids=set(state.call_ids))
    for m in [*state.messages, *state.held]:
        out._add(m, 0)
    out.history, out.history_seqs = list(state.history), [0] * len(state.history)
    if state.calls:
        results = [c.result or ToolResult(call_id=c.use.id, content=[Text(text=CONTINUED_CALL)],
                                          is_error=True) for c in state.calls]
        out._add(Message(role="user", content=list(results)), 0)
    return out


#: The result a continued task sees for a call its source task still had
#: open: that call ran (or runs) in the source task, not here.
CONTINUED_CALL = ("This call belongs to the conversation this one continues from; it ran "
                  "there, not here, and its result went there.")


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
    said_now = False
    for item in ev.items:
        body = item.body
        if isinstance(body, Say):
            if body.when == "next_turn" and state.turn_open:
                state.queued.append(body.message)
                continue
            said_now = state.turn_open or said_now
            if state.calls:
                state.held.append(body.message)
            else:
                state._add(body.message, seq)
            if not state.turn_open:
                state.steps_in_turn = 0
                state.turn_error = None
            state.turn_open = True
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
    if said_now and state.stop_turn_requested is not None:
        state.reopen_after_stop = True


def _end_turn(state: TaskState, seq: int) -> None:
    """A turn stopped or failed. Messages taken during it that the model has
    not answered (held behind its calls, or sent with the stop) join the
    conversation and open the next turn, as queued messages do."""
    state.calls = []
    state.end_turn = None
    reopen = bool(state.held) or state.reopen_after_stop or state.held_unseen
    for m in state.held:
        state._add(m, seq)
    state.held = []
    state.reopen_after_stop = False
    state.held_unseen = False
    _close_turn(state, seq)
    if reopen and not state.turn_open:
        state.turn_open = True
        state.steps_in_turn = 0


def _close_turn(state: TaskState, seq: int) -> None:
    state.turn_open = False
    state.model_attempts = 0
    state.continuing = False
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
    # A plain user turn, as 0.6 sent it: the summary carries its own framing
    # line, and must not read as a host reminder.
    summary = Message(role="user", content=[Text(text=ev.summary)])
    state.messages = [summary, *ev.kept, *tail]
    state.message_seqs = [0] * (1 + len(ev.kept)) + tail_seqs
    state.compaction = ev
    state.last_usage = None
    # A compaction-and-retry is not a crash loop.
    state.model_attempts = 0
