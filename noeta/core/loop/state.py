"""A task's state, folded from its log. No I/O: the same events always give
the same state, which is what makes recovery a matter of re-reading the log."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from noeta.core import records
from noeta.core.events import (
    Answer,
    Cancel,
    ChildEnded,
    Compacted,
    Event,
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
from noeta.core.types import (
    TOOL_LOADED_TEXT,
    Block,
    Message,
    StopReason,
    Text,
    ToolReference,
    ToolResult,
    ToolUse,
    Usage,
    user,
)

Reducer = Callable[[Any, str, Any], Any]


#: The signal that changes a running task's config
#: (``client.signal(task_id, CONFIG_SIGNAL, {"model": ...})``): only the
#: :data:`CHANGEABLE_CONFIG` keys, from the next model call on; a ``None``
#: value removes the key.
CONFIG_SIGNAL = "config"
CHANGEABLE_CONFIG = ("model", "effort", "max_turns", "output_schema", "interactive",
                     "permission_prompt_tool")


@dataclass
class Call:
    use: ToolUse
    started: bool = False
    result: ToolResult | None = None
    #: Set while the call waits (``{"on", "keys", "detail"}``).
    parked: dict[str, Any] | None = None
    #: The call as it ran, when an extension rewrote it (``ToolStarted``).
    ran: ToolUse | None = None
    #: ``ToolStarted`` events of the call: more than one when a crash left
    #: it unfinished and it ran again (``Tool.retry_safe``).
    attempts: int = 0


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
    #: what a compaction collapsed stays readable here (its saved transcript).
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
    #: Consecutive ``ModelCalled`` without a reply (crash/retry loop guard);
    #: a deliberate ``continue`` (output cap, empty reply) starts it again.
    model_attempts: int = 0
    #: The model was asked to go on after an empty reply and has not replied
    #: since, nor has a new message come in: another empty reply ends the turn.
    empty_nudged: bool = False
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
    #: ``ToolFinished.note`` s of the open batch, added after its results.
    notes: list[str] = field(default_factory=list)
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
    #: Timers set and not yet fired or cleared, by name: ``{"at": epoch
    #: seconds, "text": message or "", "seq": the log seq that set it}``. A
    #: due timer fires while the task is idle; its text arrives as a
    #: message, as if sent for the next turn.
    timers: dict[str, dict[str, Any]] = field(default_factory=dict)
    waiting: Waiting | None = None
    ended: TaskEnded | None = None
    compaction: Compacted | None = None
    #: Prompt override files found when the task first started (name ->
    #: text, see ``noeta.core.prompts``); ``None`` until they were looked for.
    prompt_files: dict[str, str] | None = None
    #: The plugins the task last recorded running with
    #: (``noeta.core.registry.pin``); ``None`` until first recorded.
    plugins: list[dict[str, str]] | None = None

    usage: Usage = field(default_factory=Usage)
    #: Tool calls this task finished (a continued task counts its own).
    tool_calls: int = 0
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
        _on_model_replied(state, ev, rec.seq)
    elif isinstance(ev, ToolStarted):
        for c in state.calls:
            if c.use.id == ev.call.id:
                c.started = True
                c.attempts += 1
                if ev.call != c.use:
                    c.ran = ev.call
    elif isinstance(ev, ToolFinished):
        _on_tool_finished(state, ev, rec.seq)
    elif isinstance(ev, Compacted):
        _compact(state, ev)
    elif isinstance(ev, Waiting):
        state.waiting = ev
    elif isinstance(ev, TaskEnded):
        state.ended = ev
        state.waiting = None
    elif isinstance(ev, ExtensionRecorded):
        core = records.decode(ev)
        if core is not None:
            _on_core_record(state, core, rec.seq)
            if isinstance(core, records.Erased):
                for name, reduce in reducers.items():
                    if name in state.ext:
                        state.ext[name] = reduce(state.ext[name], records.ERASED_KIND,
                                                 core.data())
        elif ev.extension == records.CORE:
            raise records.NewerRecord(f"core record of an unknown kind {ev.kind!r}")
        reducer = reducers.get(ev.extension)
        if reducer is not None:
            state.ext[ev.extension] = reducer(state.ext.get(ev.extension), ev.kind, ev.data)
        if ev.message is not None:
            if state.calls:
                state.held.append(ev.message)
            else:
                state._add(ev.message, rec.seq)
    return state


def _on_model_replied(state: TaskState, ev: ModelReplied, seq: int) -> None:
    resp = ev.response
    state.model_attempts = 0
    state.empty_nudged = False
    state.usage = state.usage + resp.usage
    state.last_usage = resp.usage
    state.last_stop = resp.stop_reason
    state._add(resp.message, seq)
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
        _close_turn(state, seq)


def _on_tool_finished(state: TaskState, ev: ToolFinished, seq: int) -> None:
    state.tool_calls += 1
    for c in state.calls:
        if c.use.id == ev.result.call_id:
            c.result = ev.result
    if ev.end_turn is not None:
        state.end_turn = ev.end_turn
    if ev.note:
        state.notes.append(ev.note)
    if state.calls and not state.open_calls:
        results = [c.result for c in state.calls if c.result is not None]
        state._add(_results_message(results, state.notes), seq)
        state.notes = []
        state.calls = []
        state.held_unseen = state.held_unseen or bool(state.held)
        for m in state.held:
            state._add(m, seq)
        state.held = []
        if state.end_turn is not None:
            state.answer_parts = [state.end_turn]
            state.end_turn = None
            _close_turn(state, seq)


def _results_message(results: Sequence[ToolResult], notes: Sequence[str] = ()) -> Message:
    """The user message answering a batch of calls: their results, the
    marker that follows a loaded tool, then each note once."""
    content: list[Any] = list(results)
    if any(isinstance(b, ToolReference) for r in results for b in r.content):
        content.append(Text(text=TOOL_LOADED_TEXT))
    content += [Text(text=n) for n in dict.fromkeys(notes)]
    return Message(role="user", content=content)


def _on_core_record(state: TaskState, core: records.CoreRecord, seq: int) -> None:
    if isinstance(core, records.TurnStopped):
        state.stop_turn_requested = None
        if core.marker:
            # A reply stopped mid-stream keeps what streamed, after the
            # reasoning it finished; the marker (plain text) opens the
            # person's next turn.
            if core.partial:
                content: list[Block] = [*(core.thinking or []), Text(text=str(core.partial))]
                state._add(Message(role="assistant", content=content), seq)
            state._add(Message(role="user", content=[Text(text=str(core.marker))],
                               origin="inline"), seq)
        _end_turn(state, seq)
    elif isinstance(core, records.Parked):
        for c in state.calls:
            if c.use.id == core.call_id:
                c.parked = {"on": core.on, "keys": core.keys, "detail": core.detail}
    elif isinstance(core, records.Spawned):
        for child in core.children:
            state.spawned[child["id"]] = child
    elif isinstance(core, records.Timer):
        if core.name:
            state.timers[core.name] = {"at": core.at, "text": core.text, "seq": seq}
    elif isinstance(core, records.TimerCleared):
        state.timers.pop(core.name, None)
    elif isinstance(core, records.Prompts):
        state.prompt_files = core.files
    elif isinstance(core, records.Plugins):
        state.plugins = core.pins
    elif isinstance(core, records.Continue):
        state.model_attempts = 0
        if core.usage is not None:
            state.usage = state.usage + core.usage
        if core.reason == records.EMPTY_REPLY:
            state.empty_nudged = True
    elif isinstance(core, records.TurnDone):
        if core.usage is not None:
            state.usage = state.usage + core.usage
        if core.reason == records.EMPTY_REPLY:
            state.answer_parts = []  # the turn ended on nothing: it has no answer
        _end_turn(state, seq)
    elif isinstance(core, records.Erased):
        _erase(state, set(core.seqs))
    elif isinstance(core, records.TurnFailed):
        state.turn_error = core.error
        _end_turn(state, seq)


def fold_on_copy(state: TaskState, events: Sequence[Event],
                 reducers: Mapping[str, Reducer]) -> TaskState:
    """``state`` with ``events`` folded in after its last seq, on a copy the
    live state never sees (``state`` itself when there are none)."""
    if not events:
        return state
    # Every container is copied (and each call, which ``apply`` updates in
    # place), so folding on the copy never reaches the live state.
    copies: dict[str, Any] = {}
    for f in dataclasses.fields(state):
        value = getattr(state, f.name)
        if f.name == "calls":
            copies[f.name] = [dataclasses.replace(c) for c in value]
        elif f.name == "timers":
            copies[f.name] = {k: dict(v) for k, v in value.items()}
        elif isinstance(value, (list, dict, set)):
            copies[f.name] = type(value)(value)
    sim = dataclasses.replace(state, **copies)
    for i, event in enumerate(events, 1):
        apply(sim, Recorded(seq=state.last_seq + i, at=0.0, event=event), reducers)
    return sim


def continued(state: TaskState,
              carry: Mapping[str, Callable[[Any], Any]] | None = None) -> TaskState:
    """``state`` (another task's, folded) as the start of a task that
    continues its conversation: messages and extension state
    stay (each extension's through ``carry[name]``, its
    ``Extension.continue_state``); what belongs to that task's run (its log
    positions, inbox, children, timers, open turn) does not. Calls still
    open there are answered here with :data:`CONTINUED_CALL`, right after
    their ``tool_use`` and ahead of messages held behind them, so the
    conversation stays well formed."""
    carry = carry or {}
    ext = {name: carry[name](value) if name in carry else value
           for name, value in state.ext.items()}
    out = TaskState(ext=ext, prompt_files=state.prompt_files,
                    compaction=state.compaction, last_usage=state.last_usage,
                    call_ids=set(state.call_ids))
    for m in state.messages:
        out._add(m, 0)
    out.history, out.history_seqs = list(state.history), [0] * len(state.history)
    if state.calls:
        results = [c.result or ToolResult(call_id=c.use.id, content=[Text(text=CONTINUED_CALL)],
                                          is_error=True) for c in state.calls]
        out._add(_results_message(results), 0)
    for m in state.held:
        out._add(m, 0)
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
            said_now = _say(state, body.message, seq, now=body.when == "now") or said_now
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
            if isinstance(body, External) and body.name == CONFIG_SIGNAL:
                _configure(state, body.data)
            timer = state.timers.pop(body.name, None) if isinstance(body, TimerFired) else None
            if timer and timer["text"]:
                _say(state, user(timer["text"]), seq)
    if said_now and state.stop_turn_requested is not None:
        state.reopen_after_stop = True


def _configure(state: TaskState, data: Any) -> None:
    if not isinstance(data, dict):
        return
    config = dict(state.config)
    for key in CHANGEABLE_CONFIG:
        if key in data:
            if data[key] is None:
                config.pop(key, None)
            else:
                config[key] = data[key]
    state.config = config


def _say(state: TaskState, message: Message, seq: int, *, now: bool = False) -> bool:
    """A message for the agent: queued behind an open turn unless ``now``,
    otherwise added (held behind open calls) and opening a turn. ``True``
    when it was taken into an open turn."""
    if not now and state.turn_open:
        state.queued.append(message)
        return False
    into_open = state.turn_open
    state.empty_nudged = False
    if state.calls:
        state.held.append(message)
    else:
        state._add(message, seq)
    if not state.turn_open:
        state.steps_in_turn = 0
        state.turn_error = None
    state.turn_open = True
    state.waiting = None
    return into_open


def _end_turn(state: TaskState, seq: int) -> None:
    """A turn stopped or failed. Messages taken during it that the model has
    not answered (held behind its calls, or sent with the stop) join the
    conversation and open the next turn, as queued messages do."""
    state.calls = []
    state.end_turn = None
    state.notes = []
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


def _erase(state: TaskState, seqs: set[int]) -> None:
    for messages, at in ((state.messages, state.message_seqs),
                         (state.history, state.history_seqs)):
        # Where the person's message shares its seq with other text (a stop
        # marker released with it), only what follows it is its expansion.
        with_person = {s for m, s in zip(messages, at, strict=True)
                       if s in seqs and m.origin == "human"}
        person_seen: set[int] = set()
        keep = []
        for i, (m, s) in enumerate(zip(messages, at, strict=True)):
            if m.origin == "human":
                person_seen.add(s)
            if not (s in seqs and m.role == "user" and m.origin in ("human", "inline", "memory")
                    and (s not in with_person or s in person_seen)
                    and not any(isinstance(b, ToolResult) for b in m.content)):
                keep.append(i)
        messages[:] = [messages[i] for i in keep]
        at[:] = [at[i] for i in keep]


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
        state.empty_nudged = False


def _compact(state: TaskState, ev: Compacted) -> None:
    keep_from = len(state.message_seqs)
    for i, seq in enumerate(state.message_seqs):
        if seq > ev.through:
            keep_from = i
            break
    tail = state.messages[keep_from:]
    tail_seqs = state.message_seqs[keep_from:]
    # Plain text in the user's channel: the summary carries its own framing
    # line and must not read as a host reminder, nor count as a prompt.
    summary = Message(role="user", content=[Text(text=ev.summary)], origin="inline")
    # What is re-attached joins the kept messages' last turn: ahead of the
    # system turns (per-request notes) they end with.
    k = len(tail)
    while k and tail[k - 1].role == "system":
        k -= 1
    state.messages = [*ev.lead, summary, *ev.kept, *tail[:k], *ev.after, *tail[k:]]
    state.message_seqs = [0] * (len(ev.lead) + 1 + len(ev.kept)) + tail_seqs[:k] \
        + [0] * len(ev.after) + tail_seqs[k:]
    state.compaction = ev
    state.last_usage = None
    # A compaction-and-retry is not a crash loop.
    state.model_attempts = 0
