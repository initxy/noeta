"""L2 crash-recovery attempt scanning + classification.

One Engine step is a loop of decide→act **attempts**, and every attempt's
first durable emit is ``ContextPlanComposed`` (the step-boundary event fold
counts iterations from). The stream therefore already carries an implicit
attempt journal: ``ContextPlanComposed`` is the attempt-start record
(identity = its seq, intent = the plan + the decision events after it).
What a mid-step crash leaves behind is the **last** attempt's partial tail
with no reachable suspend/terminal — the partial-step orphan.

This module holds the two pure pieces of recovery
(``docs/adr/step-attempt-recovery.md``):

* :func:`scan_interrupted_attempt` — find the live wake window's last
  attempt (or ``None`` when the tail is bare / prelude-only and a bare
  re-drive is correct by construction).
* :func:`classify_attempt` — decide whether re-driving the interrupted
  attempt needs a human: "whatever could run without a human approval gate
  may be re-driven without a human". Pure record events are always safe;
  every recorded tool call (finished or not — a re-drive re-decides, so
  finished calls may be re-executed too) must pass the live guard chain
  with ALLOW; a spawned subtask always needs a human.

The seal/re-drive/park state machine that consumes these lives in
``noeta.runtime.worker`` (it needs the lease + release discipline).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from noeta.core.engine import guard_allows_tool_call
from noeta.protocols.decisions import ToolCall
from noeta.protocols.tool_args import resolve_tool_call_arguments


__all__ = [
    "ABANDON_CAP",
    "AttemptClassification",
    "InterruptedAttempt",
    "PendingPark",
    "classify_attempt",
    "scan_interrupted_attempt",
    "scan_pending_park",
]


#: Consecutive seals allowed within one wake window before recovery stops
#: re-driving and parks regardless of classification — the crash-loop
#: backstop, mirroring the dispatcher's ``reclaim_max`` poison-task cap.
ABANDON_CAP = 3


#: Event types that open a wake window / turn. The last of these in the
#: stream is where the live window starts; an interrupted attempt can only
#: exist after it (a ``TaskSuspended`` after them would mean the task is
#: not ``running``, so recovery is never consulted).
_WINDOW_OPENERS = ("TaskStarted", "TaskWoken", "TaskRewound")


#: Tail events that always classify the attempt as needing a human:
#: a spawned child is a real side effect no re-drive may duplicate.
_SPAWN_EVENTS = ("SubtaskSpawned", "BackgroundSubagentStarted")


#: Step-activity events. A live window that carries one of these but NO
#: ``ContextPlanComposed`` is an interrupted **approval execution** — the
#: drive-side ``ResolveApprovalPrelude`` (the one prelude that executes a
#: tool) crashed between the durable ``ToolCallApprovalResolved`` and the
#: step's first plan. The scan anchors the attempt on the FIRST such event
#: so the seal returns the task to its pending-approval state.
_ACTIVITY_EVENTS = (
    "ToolCallApprovalResolved",
    "ToolCallStarted",
    "ToolResultRecorded",
    "ToolCallFinished",
) + _SPAWN_EVENTS


@dataclass(frozen=True, slots=True)
class InterruptedAttempt:
    """The live window's last (interrupted) attempt, as scanned."""

    #: seq of the attempt's anchor event (its ``ContextPlanComposed``, or
    #: for a plan-less approval-execution window the first activity
    #: event); recorded as ``StepAttemptAbandoned.abandoned_from_seq``.
    attempt_start_seq: int
    #: events from the anchor onward (the tail the seal declares dead
    #: history).
    tail: tuple[Any, ...]
    #: ``StepAttemptAbandoned`` count inside the live window — the input
    #: to the :data:`ABANDON_CAP` crash-loop backstop.
    abandon_count: int
    #: True ⇒ anchored on ``ContextPlanComposed`` (the normal mid-step
    #: crash). False ⇒ a plan-less approval-execution window: recovery
    #: always parks (a human is in that loop by definition) and re-suspends
    #: on the window's own wake handle so the ordinary approve verb re-runs
    #: the resolution against the restored pending-approval state.
    anchored_on_plan: bool = True


def scan_interrupted_attempt(events: list[Any]) -> Optional[InterruptedAttempt]:
    """Scan a ``running`` task's stream for an interrupted attempt.

    Returns ``None`` when nothing after the last window opener is step
    activity — the tail is bare or prelude-only (durable command appends
    written at seed time), and running the bare step on top is the correct
    continuation, not a recovery. Callers only invoke this on a folded
    status of ``running`` with no live writer (fresh lease), so a present
    attempt is interrupted by definition: had it finished, a
    suspend/terminal event would have flipped the status.
    """
    boundary = -1
    for i, env in enumerate(events):
        if env.type in _WINDOW_OPENERS:
            boundary = i
    attempt_index: Optional[int] = None
    first_activity: Optional[int] = None
    abandon_count = 0
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
    )


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

    Returns the live window's trailing park-reason seal, or ``None`` when
    the window does not end on one (no seal, an ``auto_redrive`` seal, or
    step activity after the seal — a re-drive already ran).
    ``notice_appended`` keys on a ``MessagesAppended`` after the seal:
    the park itself is the only writer in that window, so any message
    there is its notice.
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
    """Classify an interrupted attempt's tail per the D2 rule.

    ``engine`` / ``task`` feed :func:`guard_allows_tool_call` (the same
    guard chain that gates live execution — permission mode, risk ceiling,
    ``can_use_tool``, skill grants); ``content_store`` dereferences
    offloaded tool arguments. Unknown tools and guard failures classify
    as blockers (fail closed).

    ``event_log`` should be the ``BoundedEventLog`` capped at the
    pre-attempt baseline (the same one the seal folds): the verdict is
    about the state a re-drive would run on, so the interrupted window's
    own events must not dirty the Budget / Repetition counters the guards
    read. ``None`` falls back to the engine's full log (unit tests over a
    hand-built tail).
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
