"""Wake conditions and Subtask results.

A ``WakeCondition`` describes what a suspended Task is waiting on. A
matching ``WakeEvent`` (same dataclass shape) delivered through
``Dispatcher.wake`` flips the Task back to ``ready``.

Phase 0 shipped three variants: ``SubtaskCompleted`` (issue 03),
``HumanResponseReceived`` (issue 05 — also used by ``yield_for_human``
and ``require_approval``), and ``TimerFired`` (the
``wait_timer`` Decision branch). ``ExternalEvent`` (the
``wait_external`` Decision branch) landed with the timer poller.

``SubtaskResult`` is the typed payload carried by a ``SubtaskCompleted``
wake event and folded into the parent's ``GovernanceState`` so the
parent Policy can read previously-spawned child outcomes on its next
decide.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal, Optional, Union

from noeta.protocols.canonical import register


@dataclass(frozen=True, slots=True)
class SubtaskResult:
    """Outcome of a subtask, as seen by its parent.

    Phase 0 surfaces only the two terminal kinds (``completed`` /
    ``failed``). The richer envelope (cost, artifacts) lands later.
    """

    status: Literal["completed", "failed"]
    output: Any = None
    error: Optional[str] = None

    __canonical_tag__ = "subtask_result"


@dataclass(frozen=True, slots=True)
class SubtaskCompleted:
    """Subtask wake — condition and event share one type.

    ``subtask_id`` identifies which child the parent is waiting on;
    ``result`` carries the child's terminal outcome on the wake-event
    side. The same dataclass plays two roles:

    * As a **condition** stored in ``task.wake_on`` by Engine when the
      parent spawned the subtask: ``result`` is ``None`` (the parent
      declared what shape it was waiting for, not the answer).
    * As a **wake event** delivered through ``Dispatcher.wake(...)`` by
      :class:`noeta.core.observers.ChildLifecycleObserver` when the
      child terminates: ``result`` is the actual :class:`SubtaskResult`
      observed on the child's terminal envelope.

    **Projection-matching invariant** (domain rule — every Dispatcher
    implementation MUST honour this; see :func:`matches_wake`): wake
    delivery matches a stored condition against an incoming event by
    **identity-field projection**. For :class:`SubtaskCompleted` the
    projection is ``subtask_id``; ``result`` is carried through
    informationally but must not affect matching. Concretely::

        matches_wake(SubtaskCompleted(id=X, result=None),
                     SubtaskCompleted(id=X, result=R_any))   -> True
        matches_wake(SubtaskCompleted(id=X, result=None),
                     SubtaskCompleted(id=Y, result=None))    -> False
    """

    subtask_id: str
    result: Optional[SubtaskResult] = None

    __canonical_tag__ = "subtask_completed"


@dataclass(frozen=True, slots=True)
class HumanResponseReceived:
    """Wake condition: Task suspended waiting on a human (HITL or approval).

    There is no separate ``ApprovalRequested`` event type:
    approval-required Verdicts and direct ``yield_for_human`` decisions
    both suspend on this same wake condition. The ``handle`` is the
    opaque identifier the human-facing channel will use to wake the
    Task (issue 05 ships the carrier shape; the wake-on-handle UI is
    Phase 2).
    """

    handle: str

    __canonical_tag__ = "human_response"


@dataclass(frozen=True, slots=True)
class TimerFired:
    """Wake condition: Task suspended waiting for a timer to elapse.

    Produced by the ``wait_timer`` Decision branch. ``fire_at`` is the
    wall-clock time (epoch seconds) at which the Dispatcher should
    requeue the Task. Phase 0 records the intent; the actual timer
    Worker that delivers the wake event lands with the daemonized
    Worker in Phase 1.
    """

    fire_at: float

    __canonical_tag__ = "timer_fired"


@dataclass(frozen=True, slots=True)
class ExternalEvent:
    """Wake condition: Task suspended waiting on an external event source
    (webhook, bus, operator signal, ...).

    Produced by the ``wait_external`` Decision branch. ``event_kind`` is
    the opaque identifier the external ingress presents through
    ``Dispatcher.wake`` to wake the Task — the projection-matching key
    (see :func:`matches_wake`). The kernel never interprets it; any
    payload the external source carries belongs on the caller's own
    channel (e.g. a recorded message), not on the wake event.
    """

    event_kind: str

    __canonical_tag__ = "external_event"


@dataclass(frozen=True, slots=True)
class SubtaskGroupCompleted:
    """SR2 — N-way fan-out join (all-of barrier). A parent that spawns a
    **group** of N sub-agents in one turn suspends on this condition and is
    woken only when the **distinct member set** has all terminated
    (completed OR failed — wait-all-terminate).

    Like :class:`SubtaskCompleted`, the type plays two roles:

    * As a **condition** in ``task.wake_on``: declares the group identity
      the parent waits on.
    * As a **wake event** delivered by :class:`ChildLifecycleObserver` once
      the distinct member set is satisfied: same shape (identity only — the
      N results are NOT carried here; the parent reads them from the keyed
      ``SubtaskCompleted`` events on its own stream).

    **Projection-matching invariant** (see :func:`matches_wake`): match on
    ``group_id`` only; ``subtask_ids`` rides along for diagnosis / member-
    order result assembly but does NOT affect matching. ``group_id`` is a
    deterministic function of the ordered member ids (:func:`derive_group_id`)
    so it costs no extra ``id_factory`` call and recomputes identically in
    resume.

    ``concurrent`` (fan-out v2) is the
    **per-group opt-in** the drain reads off the folded suspend condition to
    decide whether to run the N members on the wall-clock executor (``True``)
    or the legacy one-at-a-time sequential drain (``None`` = default). It is a
    pure scheduling hint — matching still projects on ``group_id`` only — and
    is **conditionally folded**: ``__canonical_omit_none__`` drops it whenever
    it is ``None``, so every pre-v2 recording and every sequential group keeps
    byte-identical canonical bytes; only an opt-in concurrent group writes the
    one extra ``"concurrent":true`` key.
    """

    group_id: str
    subtask_ids: tuple[str, ...]   # member (spawn) order, canonical-stable
    concurrent: Optional[bool] = None

    __canonical_tag__ = "subtask_group_completed"
    __canonical_omit_none__ = frozenset({"concurrent"})


def derive_group_id(subtask_ids: tuple[str, ...]) -> str:
    """Deterministic ``group_id`` from the ordered member ids
    (no extra ``id_factory`` consumption; recomputes identically in resume).
    """
    digest = hashlib.sha256(":".join(subtask_ids).encode("utf-8")).hexdigest()
    return "g-" + digest[:16]


register("subtask_result", lambda f: SubtaskResult(**f))


def _restore_subtask_group_completed(fields: dict[str, Any]) -> "SubtaskGroupCompleted":
    return SubtaskGroupCompleted(
        group_id=fields["group_id"],
        subtask_ids=tuple(fields["subtask_ids"]),
        # ``.get`` so a pre-v2 body (no ``concurrent`` key, the omit-none
        # default) restores to ``None`` = sequential.
        concurrent=fields.get("concurrent"),
    )


register("subtask_group_completed", _restore_subtask_group_completed)


def _restore_subtask_completed(fields: dict[str, Any]) -> "SubtaskCompleted":
    """Canonical restorer that handles both legacy (no ``result``) and
    new shapes for :class:`SubtaskCompleted`."""
    return SubtaskCompleted(
        subtask_id=fields["subtask_id"],
        result=fields.get("result"),
    )


register("subtask_completed", _restore_subtask_completed)
register("human_response", lambda f: HumanResponseReceived(**f))
register("timer_fired", lambda f: TimerFired(**f))
register("external_event", lambda f: ExternalEvent(**f))


# WakeCondition is open to growth; Phase 0 shipped three variants. Typed as
# a Union so callers can write ``isinstance(wc, SubtaskCompleted)``
# today without breaking when a later phase adds more.
WakeCondition = Union[
    SubtaskCompleted,
    SubtaskGroupCompleted,
    HumanResponseReceived,
    TimerFired,
    ExternalEvent,
]
WakeEvent = WakeCondition


def matches_wake(condition: WakeCondition, event: WakeEvent) -> bool:
    """Return True iff ``event`` satisfies ``condition`` under the
    projection-matching invariant.

    The matching rule is part of the L0 wake domain; every Dispatcher
    implementation MUST route through this helper (or replicate the
    truth table exactly) so adapter-private divergence is impossible.

    Per-variant rules:

    * :class:`SubtaskCompleted` — projects to ``subtask_id``;
      ``result`` is informational and does not affect matching.
    * :class:`SubtaskGroupCompleted` — projects to ``group_id``;
      ``subtask_ids`` is informational and does not affect matching (SR2).
    * :class:`HumanResponseReceived` — projects to ``handle``.
    * :class:`ExternalEvent` — projects to ``event_kind``.
    * :class:`TimerFired` — temporal threshold:
      ``event.fire_at >= condition.fire_at``. An event observed at
      wall-clock T satisfies any timer whose deadline was ``T'`` with
      ``T' <= T``. Equality is the inclusive boundary.

    Cross-variant matches always return ``False`` — a subtask wake
    cannot satisfy a timer condition no matter what its fields are.
    """
    if isinstance(condition, SubtaskCompleted) and isinstance(event, SubtaskCompleted):
        return condition.subtask_id == event.subtask_id
    if isinstance(condition, SubtaskGroupCompleted) and isinstance(event, SubtaskGroupCompleted):
        # SR2: project on group_id; subtask_ids is informational.
        return condition.group_id == event.group_id
    if isinstance(condition, HumanResponseReceived) and isinstance(event, HumanResponseReceived):
        return condition.handle == event.handle
    if isinstance(condition, ExternalEvent) and isinstance(event, ExternalEvent):
        return condition.event_kind == event.event_kind
    if isinstance(condition, TimerFired) and isinstance(event, TimerFired):
        return event.fire_at >= condition.fire_at
    return False
