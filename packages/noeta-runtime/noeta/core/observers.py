"""Child-lifecycle observer: the parent ↔ child handoff, off the Engine's path.

A child's ``TaskCreated`` enqueues it and records the lineage; the child's
terminal event appends ``SubtaskCompleted`` to the parent stream and wakes the
parent. :class:`noeta.protocols.event_log.EventLogSubscriber` pins the
delivery: callbacks run synchronously after the child's append commits and
outside the adapter's writer lock, so the parent's notification is durable
before the child's emit returns (the causal order fold relies on) while the
cross-stream ``system_emit`` can take its own lock without re-entrancy.
Construction replays the persisted log so a child that outlives a process
restart still notifies its parent.
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
    """Wires the parent ↔ child handoff without Engine touching Dispatcher.

    Construction self-subscribes to ``event_log`` and replays the persisted
    log to rebuild its lineage; :meth:`stop` unsubscribes.
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
        # Under the concurrent drain N children of one group terminate on N
        # different OS threads, so this callback runs concurrently. ``_lock``
        # serialises the lineage mutation and the read-count-decide-wake
        # critical section, so two siblings completing at once cannot both
        # observe a full group and double-fire the barrier wake (nor race the
        # lineage dict). ``_group_woken`` is keyed by ``group_id``, NOT
        # ``parent_id`` — a parent spawns a fresh group each turn — and claims
        # the barrier exactly once.
        self._lock = threading.Lock()
        self._group_woken: set[str] = set()
        self._handle = subscribe_with_stop(event_log, self._on_event)
        # A restarted process starts with an empty ``_lineage`` (the live
        # ``TaskCreated`` events are never re-emitted), so without this seed a
        # terminal arriving after the restart is a no-op in ``_on_terminal``
        # and a parent suspended on ``SubtaskCompleted`` /
        # ``SubtaskGroupCompleted`` waits forever. Seeded AFTER subscribing so
        # a live terminal firing during the replay is still caught.
        self._replay_lineage()

    def stop(self) -> None:
        self._handle.stop()

    # -- lineage replay ----------------------------------------------------

    # A child whose stream already carries one of these was notified by the
    # observer that saw it terminate, so it must NOT be re-seeded into
    # ``_lineage`` — that would leak the entry and risk a duplicate parent
    # notification.
    _TERMINAL_TYPES = ("TaskCompleted", "TaskFailed", "TaskCancelled")

    def _replay_lineage(self) -> None:
        """Seed ``_lineage`` from the persisted log for not-yet-terminal children.

        The whole scan runs under ``_lock`` so a concurrent live
        ``TaskCreated`` / terminal event (delivered through ``_on_event``,
        which also takes ``_lock``) serialises cleanly: at worst the same key
        is written twice with the same value, and a terminal event's ``pop``
        is idempotent.
        """
        with self._lock:
            for summary in self._log.list_task_streams():
                parent_id: str | None = None
                is_background = False
                terminal = False
                for env in self._log.read(summary.task_id):
                    if env.type == "TaskCreated":
                        parent_id = getattr(env.payload, "parent_task_id", None)
                        is_background = bool(
                            getattr(env.payload, "background", None)
                        )
                    elif env.type in self._TERMINAL_TYPES:
                        terminal = True
                if parent_id is None or is_background or terminal:
                    continue
                # ``setdefault`` so a live ``_on_task_created`` that raced the
                # replay (and already wrote the same value) is not clobbered.
                self._lineage.setdefault(summary.task_id, parent_id)

    # -- callback --------------------------------------------------------

    def _on_event(self, env: EventEnvelope) -> None:
        if env.type == "TaskCreated":
            self._on_task_created(env)
            return
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
            # parent, or a parent suspended on ``SubtaskCompleted`` /
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
        # A child spawned with ``spawn_subagent(background=True)`` is INVISIBLE
        # to this observer (docs/adr/background-subagent.md). The parent never
        # suspended on it, so the auto-handoff — ``SubtaskCompleted`` on the
        # parent stream + a ``wake`` — would be a phantom completion and a
        # non-matching wake; the background-subagent driver owns that child's
        # whole lifecycle instead. Skipping ``_lineage`` here also makes
        # ``_on_terminal`` a clean no-op for it (the lineage pop misses).
        if getattr(env.payload, "background", None):
            return
        with self._lock:
            self._lineage[env.task_id] = parent_id
        self._dispatcher.enqueue(env.task_id)

    def _on_terminal(self, env: EventEnvelope, result: SubtaskResult) -> None:
        # The lineage pop is the atomic claim of this child, so a duplicate
        # terminal (or a concurrent sibling racing the dict) is a clean no-op
        # the second time.
        with self._lock:
            parent_id = self._lineage.pop(env.task_id, None)
        if parent_id is None:
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
