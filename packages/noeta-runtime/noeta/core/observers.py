"""Child-lifecycle observer.

Subscribes to an EventLog and:

* On ``TaskCreated`` with a ``parent_task_id``: enqueue the child on
  the dispatcher, and remember the parent for later.
* On terminal child events (``TaskCompleted`` / ``TaskFailed``):
  emit ``SubtaskCompleted`` onto the parent stream via
  :meth:`EventLogWriter.system_emit` (cross-stream system write, no
  lease) and wake the parent via :meth:`Dispatcher.wake`.

Delivery semantics are pinned by
:class:`noeta.protocols.event_log.EventLogSubscriber`: callbacks run
synchronously before the originating ``EventLog`` write returns, but
**after** the append is committed and **outside** the adapter writer
lock (issues 15 / 16 / 17 SQLite implementations honour the same
contract). The parent's ``SubtaskCompleted`` is therefore appended
before the child's terminal emit returns to its caller — preserving
the causal ordering fold relies on — while leaving the cross-stream
``system_emit`` free to acquire its own adapter lock without
re-entrancy concerns.

ChildLifecycleObserver is part of the built-in Observer set
alongside :class:`noeta.observers.audit.AuditObserver`,
:class:`noeta.observers.metrics.MetricsObserver`, and
:class:`noeta.observers.fanout.EventFanout`. It is wired up by
:func:`noeta.core.wiring.wire_default_observers` so the parent / child
handoff exists in every default profile without callers needing to
import it directly.
"""

from __future__ import annotations

import threading
from typing import Any, Protocol

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
    def enqueue(self, task_id: str) -> None: ...

    def wake(self, task_id: str, wake_event: Any) -> bool: ...


class ChildLifecycleObserver:
    """Wires parent ↔ child handoff without Engine touching Dispatcher.

    Constructing the observer self-subscribes to ``event_log``; call
    :meth:`stop` to unsubscribe (mainly useful in tests).
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
        # child task_id -> parent task_id; built lazily from TaskCreated.
        self._lineage: dict[str, str] = {}
        # fan-out v2: under the
        # concurrent drain N children of one group terminate on N different
        # OS threads, so this callback runs concurrently. ``_lock`` serialises
        # the lineage mutation and the read-count-decide-wake critical section
        # so two siblings completing at once cannot both observe a full group
        # and double-fire the barrier wake (nor race the lineage dict).
        # ``_group_woken`` (keyed by ``group_id``, NOT ``parent_id`` — a parent
        # spawns a fresh group each turn) claims the barrier exactly once. Under
        # the legacy single-threaded drain the lock is uncontended and the set
        # fires identically to the pre-v2 fullness check (one wake per group).
        self._lock = threading.Lock()
        self._group_woken: set[str] = set()
        self._handle = subscribe_with_stop(event_log, self._on_event)

    def stop(self) -> None:
        self._handle.stop()

    # -- callback --------------------------------------------------------

    def _on_event(self, env: EventEnvelope) -> None:
        if env.type == "TaskCreated":
            self._on_task_created(env)
            return
        if env.type == "TaskCompleted":
            # Carry the spilled ContentRef (a large answer) through as
            # the result output rather than the inline value, so the parent's
            # SubtaskCompleted event stays under the payload cap too; the engine
            # derefs it when rendering the paired tool_result.
            answer_ref = getattr(env.payload, "answer_ref", None)
            output = (
                answer_ref
                if answer_ref is not None
                else getattr(env.payload, "answer", None)
            )
            self._on_terminal(
                env,
                SubtaskResult(status="completed", output=output),
            )
            return
        if env.type == "TaskFailed":
            self._on_terminal(
                env,
                SubtaskResult(
                    status="failed",
                    error=getattr(env.payload, "reason", None),
                ),
            )
            return
        if env.type == "TaskCancelled":
            # A child that reaches terminal via cancellation (not a full-tree
            # cascade that also cancels the parent) must STILL notify its
            # parent — otherwise a parent suspended on ``SubtaskCompleted`` /
            # ``SubtaskGroupCompleted`` waits forever on a wake that never
            # fires. ``SubtaskResult`` has no ``cancelled`` status, so surface
            # it as a ``failed`` outcome carrying the cancel reason.
            reason = getattr(env.payload, "reason", None)
            self._on_terminal(
                env,
                SubtaskResult(
                    status="failed",
                    error=f"cancelled: {reason}" if reason else "cancelled",
                ),
            )

    def _on_task_created(self, env: EventEnvelope) -> None:
        parent_id = getattr(env.payload, "parent_task_id", None)
        if parent_id is None:
            return
        # background sub-agent (docs/adr/background-subagent.md): a child spawned
        # with ``spawn_subagent(background=True)`` is INVISIBLE to this observer.
        # The parent never suspended on it (no barrier), so the auto-handoff this
        # observer performs — record ``SubtaskCompleted`` on the parent stream +
        # ``wake`` the parent — would be a phantom completion + a non-matching wake.
        # The background-subagent driver owns the child's whole lifecycle (enqueue
        # → executor drive → Mechanism-C delivery) instead. Skipping ``_lineage``
        # here also makes ``_on_terminal`` a clean no-op for it (the lineage pop
        # misses → early return), so the child's terminal never touches the parent.
        if getattr(env.payload, "background", None):
            return
        with self._lock:
            self._lineage[env.task_id] = parent_id
        self._dispatcher.enqueue(env.task_id)

    def _on_terminal(self, env: EventEnvelope, result: SubtaskResult) -> None:
        # Lineage pop under the lock: atomic claim of this child so a
        # duplicate terminal (or a concurrent sibling racing the dict) is a
        # clean no-op the second time.
        with self._lock:
            parent_id = self._lineage.pop(env.task_id, None)
        if parent_id is None:
            return
        # Always record the child's completion on the parent stream first
        # (keyed by subtask_id — the source of truth for both the single
        # wake and the SR2 group result assembly). Emitted OUTSIDE ``_lock``:
        # ``system_emit`` notifies subscribers (including this observer's own
        # ``_on_event``) synchronously on this thread, so holding a
        # non-reentrant lock across it would self-deadlock; the EventLog has
        # its own writer lock, and ``SubtaskCompleted`` is not a type
        # ``_on_event`` acts on.
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
        # SR2: if the parent is waiting on a GROUP, wake it only
        # when the distinct member set is satisfied (all-of barrier); else
        # the single-child (SR1) wake fires immediately. The read-count-decide
        # is done under ``_lock`` and claims the barrier via ``_group_woken``
        # so concurrent siblings fire the group wake exactly once.
        with self._lock:
            wake_on = self._current_wake_on(parent_id)
            if isinstance(wake_on, SubtaskGroupCompleted):
                completed = self._completed_member_ids(
                    parent_id, wake_on.subtask_ids
                )
                if (
                    completed == set(wake_on.subtask_ids)   # B1: distinct membership
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
        (no ContentStore needed): the last ``TaskSuspended.wake_on`` not yet
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
        group (B1 — intersection, so duplicate / stray completions cannot
        falsely satisfy the barrier)."""
        members = set(member_ids)
        return {
            e.payload.subtask_id
            for e in self._log.read(parent_id)
            if e.type == "SubtaskCompleted"
            and getattr(e.payload, "subtask_id", None) in members
        }
