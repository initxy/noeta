"""Child-lifecycle observer: the parent ↔ child handoff, off the Engine's path.

A child's ``TaskCreated`` enqueues it (inheriting its parent's queue); the
child's terminal event appends ``SubtaskCompleted`` to the parent stream and
wakes the parent. :class:`noeta.protocols.event_log.EventLogSubscriber` pins
the delivery: callbacks run synchronously after the child's append commits and
outside the adapter's writer lock, so the parent's notification is durable
before the child's emit returns (the causal order fold relies on) while the
cross-stream ``system_emit`` can take its own lock without re-entrancy.

Lineage is **derived from the log, not process memory** (ADR
``worker-queue-routing``): the terminal handler reads the child stream's
``TaskCreated`` to find the parent, and the parent stream to decide whether
this child's handoff was already recorded. That makes the handoff correct in
whichever process commits the terminal, and makes construction a *recovery*
pass — any handoff a crashed process left missing is emitted then.
"""

from __future__ import annotations

import threading
from typing import Any, Optional, Protocol

from noeta.protocols.event_log import EventLogFull, subscribe_with_stop
from noeta.protocols.events import (
    EventEnvelope,
    SubtaskCompletedPayload,
)
from noeta.protocols.wake import (
    SubtaskCompleted,
    SubtaskGroupCompleted,
    SubtaskResult,
)


class _Dispatcher(Protocol):
    def enqueue(
        self, task_id: str, *, parent_task_id: Optional[str] = None
    ) -> None: ...

    def wake(self, task_id: str, wake_event: Any) -> bool: ...


#: A child whose stream carries one of these has finished; the handoff decision
#: is then durable-deduped against the parent stream, never process memory.
_TERMINAL_TYPES = ("TaskCompleted", "TaskFailed", "TaskCancelled")


def _result_of(env: EventEnvelope) -> Optional[SubtaskResult]:
    """The ``SubtaskResult`` a terminal envelope hands the parent, or ``None``
    for a non-terminal envelope."""
    if env.type == "TaskCompleted":
        # Carry a spilled ContentRef (a large answer) through as the result
        # output rather than the inline value, so the parent's
        # SubtaskCompleted stays under the payload cap too; the Engine
        # derefs it when rendering the paired tool_result.
        answer_ref = getattr(env.payload, "answer_ref", None)
        output = (
            answer_ref
            if answer_ref is not None
            else getattr(env.payload, "answer", None)
        )
        return SubtaskResult(status="completed", output=output)
    if env.type == "TaskFailed":
        return SubtaskResult(
            status="failed",
            error=getattr(env.payload, "reason", None),
        )
    if env.type == "TaskCancelled":
        # A child that reaches terminal via cancellation (not a full-tree
        # cascade that also cancels the parent) must STILL notify its
        # parent, or a parent suspended on ``SubtaskCompleted`` /
        # ``SubtaskGroupCompleted`` waits forever on a wake that never
        # fires. ``SubtaskResult`` has no ``cancelled`` status, so surface
        # it as a ``failed`` outcome carrying the cancel reason.
        reason = getattr(env.payload, "reason", None)
        return SubtaskResult(
            status="failed",
            error=f"cancelled: {reason}" if reason else "cancelled",
        )
    return None


class ChildLifecycleObserver:
    """Wires the parent ↔ child handoff without Engine touching Dispatcher.

    Construction self-subscribes to ``event_log`` and then runs a recovery
    pass — emitting the handoff for any already-terminal child the parent
    stream does not record yet; :meth:`stop` unsubscribes.
    """

    def __init__(
        self,
        *,
        event_log: EventLogFull,
        dispatcher: _Dispatcher,
        actor: str = "child_observer",
    ) -> None:
        self._log = event_log
        self._dispatcher = dispatcher
        self._actor = actor
        # Under the concurrent drain N children of one group terminate on N
        # different OS threads, so this callback runs concurrently. ``_lock``
        # serialises the read-count-decide-wake critical section, so two
        # siblings completing at once cannot both observe a full group and
        # double-fire the barrier wake. ``_group_woken`` is keyed by
        # ``group_id``, NOT ``parent_id`` — a parent spawns a fresh group each
        # turn — and claims the barrier exactly once per process; cross-process
        # duplicates are absorbed by wake matching (a stale buffered group
        # wake never matches a fresh group_id).
        self._lock = threading.Lock()
        self._group_woken: set[str] = set()
        self._handle = subscribe_with_stop(event_log, self._on_event)
        # Recovery: a crash between a child's terminal commit and the handoff
        # emit would otherwise strand the parent forever — the terminal event
        # is never re-emitted, so no live callback will ever fire for it
        # again. Subscribe FIRST so a terminal committing during the scan is
        # caught either way (the durable dedupe absorbs the overlap).
        self._recover_pending_handoffs()

    def stop(self) -> None:
        self._handle.stop()

    # -- recovery ----------------------------------------------------------

    def _recover_pending_handoffs(self) -> None:
        """Emit the handoff for every non-background child that is terminal
        but not yet recorded on its parent stream.

        Runs at construction, over the persisted log. The per-child decision
        is the same durable dedupe the live path uses, so re-running it (N
        restarts, N instances) converges instead of stacking duplicates. A
        second instance starting while the committing instance is mid-emit
        can still double-write one handoff — accepted: group barriers count
        distinct members and a stale buffered wake never matches a fresh
        condition (ADR ``worker-queue-routing``).
        """
        for summary in self._log.list_task_streams():
            terminal_env: Optional[EventEnvelope] = None
            for env in self._log.read(summary.task_id):
                if env.type in _TERMINAL_TYPES:
                    terminal_env = env
                    break
            if terminal_env is None:
                continue
            result = _result_of(terminal_env)
            if result is not None:
                self._on_terminal(terminal_env, result)

    # -- callback --------------------------------------------------------

    def _on_event(self, env: EventEnvelope) -> None:
        if env.type == "TaskCreated":
            self._on_task_created(env)
            return
        result = _result_of(env)
        if result is not None:
            self._on_terminal(env, result)

    def _on_task_created(self, env: EventEnvelope) -> None:
        parent_id = getattr(env.payload, "parent_task_id", None)
        if parent_id is None:
            return
        # A child spawned with ``spawn_subagent(background=True)`` is INVISIBLE
        # to this observer (docs/adr/background-subagent.md). The parent never
        # suspended on it, so the auto-handoff — ``SubtaskCompleted`` on the
        # parent stream + a ``wake`` — would be a phantom completion and a
        # non-matching wake; the background-subagent driver owns that child's
        # whole lifecycle instead (``_child_parent`` skips it symmetrically,
        # so ``_on_terminal`` is a clean no-op for it too).
        if getattr(env.payload, "background", None):
            return
        # ``parent_task_id`` routes the child onto its parent's queue, so a
        # task tree runs on the pool that seeded its root.
        self._dispatcher.enqueue(env.task_id, parent_task_id=parent_id)

    def _child_parent(self, child_id: str) -> Optional[str]:
        """The parent this child must notify, from the child's own stream —
        ``None`` for a root task and for a background child."""
        for env in self._log.read(child_id):
            if env.type == "TaskCreated":
                if getattr(env.payload, "background", None):
                    return None
                parent: Optional[str] = getattr(
                    env.payload, "parent_task_id", None
                )
                return parent
        return None

    def _on_terminal(self, env: EventEnvelope, result: SubtaskResult) -> None:
        parent_id = self._child_parent(env.task_id)
        if parent_id is None:
            return
        # Durable dedupe + post-terminal guard, one scan: skip when the parent
        # stream already records this child's handoff (a duplicate terminal, a
        # recovery overlapping the live path), and when the parent itself is
        # terminal (a cascade cancel) — a terminal stream takes no appends and
        # its wake could never be consumed.
        for e in self._log.read(parent_id):
            if (
                e.type == "SubtaskCompleted"
                and getattr(e.payload, "subtask_id", None) == env.task_id
            ):
                return
            if e.type in _TERMINAL_TYPES:
                return
        # Record the child's completion on the parent stream first, keyed by
        # subtask_id — the source of truth for both the single wake and the
        # group result assembly. Emitted OUTSIDE ``_lock``: ``system_emit``
        # notifies subscribers (including this observer's own ``_on_event``)
        # synchronously on this thread, so holding a non-reentrant lock across
        # it would self-deadlock. The EventLog has its own writer lock, and
        # ``SubtaskCompleted`` is not a type ``_on_event`` acts on.
        self._log.system_emit(
            task_id=parent_id,
            type="SubtaskCompleted",
            payload=SubtaskCompletedPayload(
                subtask_id=env.task_id, result=result
            ),
            trace_id=env.trace_id,
            actor=self._actor,
            origin="observer",
        )
        # A parent waiting on a GROUP wakes only once the distinct member set
        # is satisfied (all-of barrier); a single-child wait wakes immediately.
        # The read-count-decide runs under ``_lock`` and claims the barrier via
        # ``_group_woken``, so concurrent siblings fire the group wake exactly
        # once.
        with self._lock:
            wake_on = self._current_wake_on(parent_id)
            if isinstance(wake_on, SubtaskGroupCompleted):
                completed = self._completed_member_ids(
                    parent_id, wake_on.subtask_ids
                )
                if (
                    completed == set(wake_on.subtask_ids)   # distinct membership
                    and wake_on.group_id not in self._group_woken
                ):
                    self._group_woken.add(wake_on.group_id)
                    self._dispatcher.wake(
                        parent_id,
                        SubtaskGroupCompleted(
                            group_id=wake_on.group_id,
                            subtask_ids=wake_on.subtask_ids,
                        ),
                    )
                # else: more members still pending (or already woken) → no wake
                return
            self._dispatcher.wake(
                parent_id,
                SubtaskCompleted(subtask_id=env.task_id, result=result),
            )

    def _current_wake_on(self, parent_id: str) -> Any:
        """The parent's current suspend condition, derived from its stream
        alone (no ContentStore): the last ``TaskSuspended.wake_on`` not yet
        followed by a ``TaskWoken``."""
        wake_on: Any = None
        for e in self._log.read(parent_id):
            if e.type == "TaskSuspended":
                wake_on = getattr(e.payload, "wake_on", None)
            elif e.type == "TaskWoken":
                wake_on = None
        return wake_on

    def _completed_member_ids(
        self, parent_id: str, member_ids: tuple[str, ...]
    ) -> set[str]:
        """Distinct ``subtask_id``s on the parent stream that belong to the
        group. Intersecting with the member set is what stops a duplicate or
        stray completion from falsely satisfying the barrier."""
        members = set(member_ids)
        return {
            e.payload.subtask_id
            for e in self._log.read(parent_id)
            if e.type == "SubtaskCompleted"
            and getattr(e.payload, "subtask_id", None) in members
        }
