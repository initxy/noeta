"""Crash-recovery scanning and classification of interrupted step attempts.

One Engine step is a loop of decide→act attempts whose first durable emit is
``ContextPlanComposed``, so the stream is already an attempt journal and a
mid-step crash is recognisable as the last attempt's partial tail with no
reachable suspend or terminal. Only the pure decisions live here — locate that
tail, read back the tool batch it recorded, and judge whether re-driving it
needs a human; the resume / seal / re-drive / park state machine that
consumes them lives in ``noeta.runtime.worker``, which owns the lease
discipline (``docs/adr/032-step-attempt-recovery.md``,
``docs/adr/065-crash-recovery-keeps-finished-tool-calls.md``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from noeta.core.engine import guard_allows_tool_call
from noeta.core.fold import messages_from_appended
from noeta.protocols.decisions import ToolCall
from noeta.protocols.errors import ContentNotFound
from noeta.protocols.messages import ToolResultBlock, ToolUseBlock
from noeta.protocols.tool import ToolResult
from noeta.protocols.tool_args import resolve_tool_call_arguments


__all__ = [
    "ABANDON_CAP",
    "AttemptClassification",
    "BatchCompletion",
    "INTERRUPTED_CALL_ERROR",
    "InterruptedAttempt",
    "NOT_STARTED_CALL_ERROR",
    "PendingPark",
    "RecordedBatch",
    "classify_attempt",
    "plan_batch_completion",
    "scan_interrupted_attempt",
    "scan_pending_park",
    "scan_recorded_batch",
]


#: Crash recoveries allowed within one wake window — seals, plus resumed
#: tool batches that lost a call to a crash — before recovery stops
#: continuing on its own and parks regardless of classification: the
#: crash-loop backstop.
ABANDON_CAP = 3


#: The result a call gets when it started but its result was never recorded
#: (a worker crash, or a forced stop that abandoned the step): it may have
#: run to completion, partly, or not at all.
INTERRUPTED_CALL_ERROR = (
    "Interrupted before its result was recorded, so its outcome is unknown — "
    "it may have fully, partly, or not applied. Check its effect before "
    "running it again."
)

#: The result a call gets when the batch was interrupted before it started
#: and recovery cannot run it on its own (a control or routing tool, or a
#: name no tool answers to).
NOT_STARTED_CALL_ERROR = (
    "Not run: the batch was interrupted before this call started. Issue it "
    "again if it is still needed."
)


#: Event types that open a wake window / turn. The last one in the stream is
#: where the live window starts; an interrupted attempt can only exist after
#: it, because a ``TaskSuspended`` past them would mean the task is not
#: ``running`` and recovery would never be consulted.
_WINDOW_OPENERS = ("TaskStarted", "TaskWoken", "TaskRewound")


#: Tail events that always classify the attempt as needing a human: a spawned
#: child is a real side effect no re-drive may duplicate.
_SPAWN_EVENTS = ("SubtaskSpawned", "BackgroundSubagentStarted")


#: Step-activity events. A live window carrying one of these but NO
#: ``ContextPlanComposed`` is an interrupted **approval execution**: the
#: drive-side ``ResolveApprovalPrelude`` (the one prelude that executes a
#: tool) crashed between the durable ``ToolCallApprovalResolved`` and the
#: step's first plan. The scan anchors on the FIRST such event so the seal
#: returns the task to its pending-approval state.
_ACTIVITY_EVENTS = (
    "ToolCallApprovalResolved",
    "ToolCallStarted",
    "ToolResultRecorded",
    "ToolCallFinished",
) + _SPAWN_EVENTS


@dataclass(frozen=True, slots=True)
class InterruptedAttempt:
    """The live window's last (interrupted) attempt, as scanned."""

    #: seq of the attempt's anchor event; recorded as
    #: ``StepAttemptAbandoned.abandoned_from_seq``.
    attempt_start_seq: int
    #: events from the anchor onward — the tail the seal declares dead history.
    tail: tuple[Any, ...]
    #: ``StepAttemptAbandoned`` count inside the live window — an input to the
    #: :data:`ABANDON_CAP` crash-loop backstop.
    abandon_count: int
    #: True ⇒ anchored on ``ContextPlanComposed`` (the normal mid-step crash).
    #: False ⇒ a plan-less approval-execution window: recovery always parks (a
    #: human is in that loop by definition) and re-suspends on the window's own
    #: wake handle so the ordinary approve verb re-runs the resolution against
    #: the restored pending-approval state.
    anchored_on_plan: bool = True
    #: Earlier live attempts in the window that kept their tool batch across
    #: a crash which cut a call short (a ``ToolCallStarted`` with no recorded
    #: result) — the other input to the :data:`ABANDON_CAP` backstop, since
    #: such a recovery leaves no seal behind.
    resumed_count: int = 0


def scan_interrupted_attempt(events: list[Any]) -> Optional[InterruptedAttempt]:
    """Scan a ``running`` task's stream for an interrupted attempt.

    Returns ``None`` when nothing after the last window opener is step
    activity — the tail is bare or prelude-only, and running the bare step
    on top is the correct continuation, not a recovery. Callers only invoke
    this on a folded status of ``running`` with no live writer (fresh
    lease), so a present attempt is interrupted by definition: had it
    finished, a suspend/terminal event would have flipped the status.
    """
    boundary = -1
    for i, env in enumerate(events):
        if env.type in _WINDOW_OPENERS:
            boundary = i
    attempt_index: Optional[int] = None
    first_activity: Optional[int] = None
    abandon_count = 0
    live_start = boundary + 1
    for i in range(boundary + 1, len(events)):
        env = events[i]
        if env.type == "ContextPlanComposed":
            attempt_index = i
        elif env.type == "StepAttemptAbandoned":
            abandon_count += 1
            # A seal closes everything before it — an anchor seen so far
            # belongs to sealed dead history, not the live tail.
            attempt_index = None
            first_activity = None
            live_start = i + 1
        elif first_activity is None and env.type in _ACTIVITY_EVENTS:
            first_activity = i
    if attempt_index is None and first_activity is None:
        return None
    if attempt_index is not None:
        anchor, on_plan = attempt_index, True
    else:
        anchor, on_plan = first_activity, False  # type: ignore[assignment]
    return InterruptedAttempt(
        attempt_start_seq=events[anchor].seq,
        tail=tuple(events[anchor:]),
        abandon_count=abandon_count,
        anchored_on_plan=on_plan,
        resumed_count=_resumed_attempts(events[live_start:anchor]),
    )


def _resumed_attempts(events: list[Any]) -> int:
    """How many attempts in ``events`` (a live, unsealed stretch) hold a
    ``ToolCallStarted`` with no recorded result — each one is a crash its
    tool batch was resumed across."""
    count = 0
    open_calls: set[str] = set()
    for env in events:
        if env.type == "ContextPlanComposed":
            count += bool(open_calls)
            open_calls = set()
        elif env.type == "ToolCallStarted":
            open_calls.add(env.payload.call_id)
        elif env.type in ("ToolResultRecorded", "ToolCallFinished"):
            open_calls.discard(env.payload.call_id)
    return count + bool(open_calls)


@dataclass(frozen=True, slots=True)
class PendingPark:
    """A park whose seal landed but whose completion did not."""

    #: the trailing park-reason ``StepAttemptAbandoned`` envelope.
    seal: Any
    #: True ⇒ the park's system notice is already on the stream (the crash
    #: hit between the notice and the suspend), so completing the park must
    #: not append it a second time.
    notice_appended: bool


def scan_pending_park(events: list[Any]) -> Optional[PendingPark]:
    """Detect a park whose seal is durable but whose completion is not.

    The recovery park is three writes — seal → system notice →
    ``TaskSuspended`` — and only the first is fenced by re-entry: a crash
    after the seal leaves folded status ``running`` with a clean
    :func:`scan_interrupted_attempt` (the seal reset the window), which
    reads exactly like the benign crash-between-seal-and-re-drive shape.
    The seal's ``reason`` disambiguates: a park reason (anything but
    ``auto_redrive``) is a durable "do not re-drive" decision, so the
    worker must finish the park — never run the bare step over it.

    ``notice_appended`` keys on a ``MessagesAppended`` after the seal: the
    park itself is the only writer in that window, so any message there is
    its notice.
    """
    boundary = -1
    for i, env in enumerate(events):
        if env.type in _WINDOW_OPENERS:
            boundary = i
    seal: Optional[Any] = None
    notice = False
    for env in events[boundary + 1:]:
        if env.type == "StepAttemptAbandoned":
            seal, notice = env, False
        elif seal is not None:
            if (
                env.type == "ContextPlanComposed"
                or env.type in _ACTIVITY_EVENTS
            ):
                seal = None   # a re-drive ran after the seal
            elif env.type == "MessagesAppended":
                notice = True
    if seal is not None and seal.payload.reason != "auto_redrive":
        return PendingPark(seal=seal, notice_appended=notice)
    return None


@dataclass(frozen=True, slots=True)
class AttemptClassification:
    """Verdict over one interrupted attempt's tail."""

    #: True ⇒ the attempt may be sealed + re-driven with no human.
    safe: bool
    #: Human-readable blockers (tool names + completion state / spawns),
    #: rendered into the park notice so operator and model both see what
    #: may have partially applied.
    blockers: tuple[str, ...]


def classify_attempt(
    tail: tuple[Any, ...],
    *,
    engine: Any,
    task: Any,
    content_store: Any,
    event_log: Any = None,
) -> AttemptClassification:
    """Decide whether re-driving an interrupted attempt needs a human.

    The rule is "whatever could run without a human approval gate may be
    re-driven without a human": every recorded tool call must pass the live
    guard chain with ALLOW — finished or not, since a re-drive re-decides
    and may re-execute a finished call — and a spawned subtask always
    blocks. Unknown tools and guard failures classify as blockers (fail
    closed).

    ``event_log`` should be the ``BoundedEventLog`` capped at the
    pre-attempt baseline (the same one the seal folds): the verdict is
    about the state a re-drive would run on, so the interrupted window's
    own events must not dirty the Budget / Repetition counters the guards
    read. ``None`` falls back to the engine's full log.
    """
    finished: set[str] = set()
    for env in tail:
        if env.type == "ToolCallFinished":
            finished.add(env.payload.call_id)
    blockers: list[str] = []
    for env in tail:
        if env.type in _SPAWN_EVENTS:
            blockers.append("spawned a subtask")
            continue
        if env.type != "ToolCallStarted":
            continue
        payload = env.payload
        call = ToolCall(
            tool_name=payload.tool_name,
            arguments=resolve_tool_call_arguments(payload, content_store),
            call_id=payload.call_id,
        )
        if not guard_allows_tool_call(engine, task, call, event_log=event_log):
            state = (
                "completed" if payload.call_id in finished else "interrupted"
            )
            blockers.append(f"{payload.tool_name} ({state})")
    return AttemptClassification(
        safe=not blockers, blockers=tuple(blockers)
    )


@dataclass(frozen=True, slots=True)
class RecordedBatch:
    """The tool batch an interrupted attempt decided on, as the stream
    recorded it."""

    #: the assistant message's tool calls, in the order the model made them.
    tool_uses: tuple[ToolUseBlock, ...]
    #: True ⇒ the batch's tool-result message already landed; the attempt
    #: only lost what came after it, so nothing is left to answer.
    results_present: bool
    #: call ids with a ``ToolCallStarted`` after the decision.
    started: frozenset[str]
    #: call id → its ``ToolResultRecorded`` payload: the outcome is known.
    recorded: Mapping[str, Any]
    #: call id → the reason its ``ToolCallDenied`` recorded.
    denied: Mapping[str, str]


def scan_recorded_batch(
    tail: tuple[Any, ...], content_store: Any
) -> Optional[RecordedBatch]:
    """Read back the tool batch an interrupted attempt recorded, or ``None``
    when recovery should seal and re-drive it instead.

    A batch is kept when its assistant message (the ``tool_use`` blocks) is
    on the stream and the batch got far enough to matter: a call started,
    or the result message landed. A decision with nothing started has run
    nothing, so re-deciding it is free of side effects and the attempt
    re-drives as before. A spawn anywhere in the tail also returns ``None``
    — a child is a side effect the classifier parks on.
    """
    tool_uses: tuple[ToolUseBlock, ...] = ()
    after = -1
    results_present = False
    for i, env in enumerate(tail):
        if env.type in _SPAWN_EVENTS:
            return None
        if env.type != "MessagesAppended":
            continue
        for message in messages_from_appended(env, content_store):
            uses = tuple(
                b for b in message.content if isinstance(b, ToolUseBlock)
            )
            if message.role == "assistant" and uses:
                tool_uses, after, results_present = uses, i, False
            elif message.role == "tool" and tool_uses:
                results_present = True
    if not tool_uses:
        return None
    started: set[str] = set()
    recorded: dict[str, Any] = {}
    denied: dict[str, str] = {}
    for env in tail[after + 1:]:
        if env.type == "ToolCallStarted":
            started.add(env.payload.call_id)
        elif env.type == "ToolResultRecorded":
            recorded[env.payload.call_id] = env.payload
        elif env.type == "ToolCallDenied":
            denied[env.payload.call_id] = env.payload.reason
    if not results_present and not started:
        return None
    return RecordedBatch(
        tool_uses=tool_uses,
        results_present=results_present,
        started=frozenset(started),
        recorded=recorded,
        denied=denied,
    )


@dataclass(frozen=True, slots=True)
class BatchCompletion:
    """How each call of a kept batch gets its result."""

    #: calls whose result the stream recorded, rebuilt for re-rendering.
    recorded: tuple[tuple[ToolCall, ToolResult], ...]
    #: calls answered without running: interrupted, denied, or not runnable.
    answered: tuple[ToolResultBlock, ...]
    #: calls that never started and run now, past the live guard chain.
    pending: tuple[ToolCall, ...]


def plan_batch_completion(
    batch: RecordedBatch,
    *,
    tool_named: Callable[[str], Any],
    content_store: Any,
) -> BatchCompletion:
    """Sort a kept batch's calls by what the stream knows about each.

    A recorded result is reused, never recomputed. A call that started
    without a recorded result gets :data:`INTERRUPTED_CALL_ERROR` and never
    runs again. A recorded denial keeps its reason. A call that never
    started runs now when a plain tool answers to its name; a control or
    routing tool (``route_call``) was translated by the Policy, which this
    path does not re-enter, so such a call gets
    :data:`NOT_STARTED_CALL_ERROR` instead.
    """
    recorded: list[tuple[ToolCall, ToolResult]] = []
    answered: list[ToolResultBlock] = []
    pending: list[ToolCall] = []
    for use in batch.tool_uses:
        call = ToolCall(
            tool_name=use.tool_name,
            arguments=dict(use.arguments),
            call_id=use.call_id,
        )
        payload = batch.recorded.get(use.call_id)
        if payload is not None:
            recorded.append((call, _recorded_result(payload, content_store)))
            continue
        error: Optional[str]
        if use.call_id in batch.started:
            error = INTERRUPTED_CALL_ERROR
        elif use.call_id in batch.denied:
            error = batch.denied[use.call_id] or "denied"
        else:
            tool = tool_named(use.tool_name)
            runnable = (
                tool is not None and getattr(tool, "route_call", None) is None
            )
            error = None if runnable else NOT_STARTED_CALL_ERROR
        if error is None:
            pending.append(call)
        else:
            answered.append(
                ToolResultBlock(
                    call_id=use.call_id, output="", success=False, error=error
                )
            )
    return BatchCompletion(
        recorded=tuple(recorded),
        answered=tuple(answered),
        pending=tuple(pending),
    )


def _recorded_result(payload: Any, content_store: Any) -> ToolResult:
    """The ``ToolResult`` a ``ToolResultRecorded`` describes. The output
    body is the JSON the runtime encoded (raw bytes for a bytes output); a
    body the store no longer holds leaves the summary to speak for it."""
    output: Any = None
    try:
        body = content_store.get(payload.output_ref)
    except ContentNotFound:
        body = None
    if body is not None:
        try:
            output = json.loads(body)
        except ValueError:
            output = body
    return ToolResult(
        success=bool(payload.success), output=output, summary=payload.summary
    )
