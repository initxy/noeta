"""Background sub-agent registry — executor-driven, non-blocking spawn.

docs/adr/background-subagent.md. The runtime accelerator behind
``spawn_subagent(background=True)``: it owns the live, in-process table of
background sub-agents (per session) and submits each one's subtree to the
shared fan-out executor (:func:`noeta.execution.subtask_drain._global_executor`)
so it runs CONCURRENTLY with the parent's continuing turn — the parent never
suspends on it.

Unlike :class:`noeta.runtime.background_shell.ProcessRegistry` (a host process
table for the no-Policy background *command*), a background sub-agent IS a
durable Task with its own Policy + EventLog. So this registry is far thinner:
no ``Popen`` / PID / output buffer / conservative PID recovery — the child's
own EventLog is the authoritative record, and a crash-recovery scan re-drives a
non-terminal child straight from that log (and re-delivers a terminal one whose
turn-boundary notice was lost). The registry holds only the live ``Future``
bookkeeping (the per-session cap + which children are still in flight); it is
NEVER persisted, so it has zero effect on the recorded record — exactly the
property the :class:`~noeta.runtime.cancellation.CancellationRegistry` and the
process registry share.

The authoritative durable trace is the ``BackgroundSubagent{Started,Delivered}``
event pair on the parent stream (plus the child's own ``Task*`` stream); this
registry just makes "launch one, drive it, deliver when done" O(1) and live.

Thread-safe: ``launch`` arrives on the engine's drive thread, the drive runs on
an executor worker, the done-callback fires on whichever thread completes the
future, and ``recover`` runs on the host-startup thread — so a
:class:`threading.Lock` guards the in-flight table.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future
from typing import Any, Callable, Optional

from noeta.execution.subtask_drain import (
    DrainHost,
    _drive_member_to_terminal,
    _global_executor,
)
from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.errors import TaskCancellationRequested
from noeta.protocols.event_log import EventLogFull
from noeta.protocols.events import TaskCancelledPayload


_log = logging.getLogger(__name__)


__all__ = [
    "DEFAULT_MAX_BACKGROUND_SUBAGENTS_PER_SESSION",
    "BackgroundSubagentRegistry",
]


#: Per-session background sub-agent concurrency cap (mirrors the background-shell
#: ``DEFAULT_MAX_BACKGROUND_JOBS_PER_SESSION`` job cap). ``capacity`` REJECTS the
#: next launch over this ceiling (it does NOT queue — a clear "let one finish"
#: refusal is more direct for an agent than an invisible wait). v1 default;
#: ``HostConfig`` overrides it. A runtime accelerator: the count is recomputed
#: from the live table, never the log, so a rejected launch writes no event.
DEFAULT_MAX_BACKGROUND_SUBAGENTS_PER_SESSION = 8


#: ``(parent_task_id) -> DrainHost`` — build the delegation host for a parent's
#: tree (the resolver's ``_build_drain_host``). The registry drives ONE
#: background child on it, holding only that child's lease.
BuildHostFn = Callable[[str], DrainHost]

#: ``(parent_task_id, child_task_id) -> None`` — the delivery hook fired once a
#: background child reaches terminal (or its drive raised). The resolver wires
#: its Mechanism-C delivery here; it MUST NOT block (it hands off to a daemon
#: drive thread, mirroring the background-shell ``on_background_exit`` contract).
DeliverFn = Callable[[str, str], None]


class BackgroundSubagentRegistry:
    """Live table of in-flight background sub-agents + their drive futures."""

    def __init__(
        self,
        *,
        event_log: EventLogFull,
        content_store: ContentStore,
        dispatcher: Dispatcher,
        build_host: BuildHostFn,
        deliver: DeliverFn,
        max_per_session: int = DEFAULT_MAX_BACKGROUND_SUBAGENTS_PER_SESSION,
    ) -> None:
        self._event_log = event_log
        self._content_store = content_store
        self._dispatcher = dispatcher
        self._build_host = build_host
        self._deliver = deliver
        self._max_per_session = max_per_session
        self._lock = threading.Lock()
        # session-root task id -> set of in-flight background child ids.
        self._inflight: dict[str, set[str]] = {}

    # -- capacity (pre-flight) --------------------------------------------

    def capacity(self, parent_task_id: str) -> Optional[str]:
        """Return a rejection reason when the session is at its background cap,
        else ``None``. Checked by the handler BEFORE any durable write so an
        over-cap launch leaves no trace (reject, don't queue)."""
        with self._lock:
            running = len(self._inflight.get(parent_task_id, ()))
        if running >= self._max_per_session:
            return (
                f"too many background sub-agents "
                f"({running}/{self._max_per_session}) running for this session; "
                "let one finish before starting another"
            )
        return None

    # -- launch ------------------------------------------------------------

    def launch(self, *, parent_task_id: str, child_task_id: str) -> None:
        """Enqueue + submit a freshly-created background child to the executor.

        Non-blocking: registers the child, enqueues it on the dispatcher (the
        ``ChildLifecycleObserver`` skipped it — its genesis is ``background=True``),
        builds the parent's :class:`DrainHost`, and submits the drive to the
        shared bounded pool. The drive runs concurrently with the parent's turn;
        the done-callback hands the child to the delivery hook."""
        with self._lock:
            self._inflight.setdefault(parent_task_id, set()).add(child_task_id)
        self._submit(parent_task_id, child_task_id)

    def _submit(self, parent_task_id: str, child_task_id: str) -> None:
        # The child was created with background=True, so the observer did NOT
        # enqueue it — the registry must, or the targeted child-lease in
        # ``_descend_to_child`` would find nothing to lease. ``reserved=True``
        # keeps it targeted-lease-only: the executor's ``_descend_to_child``
        # (which seeds its goal) is the sole claimant, so a resident-worker
        # pool's untargeted poll cannot steal the unseeded child out from under
        # it and drive it with an empty message history.
        self._dispatcher.enqueue(child_task_id, reserved=True)
        host = self._build_host(parent_task_id)
        future = _global_executor().submit(
            _drive_member_to_terminal, host, child_task_id
        )
        future.add_done_callback(
            lambda f: self._on_done(f, parent_task_id, child_task_id)
        )

    def _on_done(
        self, future: "Future[Any]", parent_task_id: str, child_task_id: str
    ) -> None:
        """Executor done-callback: drop the in-flight entry, then deliver.

        Runs on the worker thread once the drive settles. A drive that raised
        (e.g. the background child hit an unsupported mid-flight HITL/approval,
        or was cancelled) is logged but STILL delivered — the delivery hook reads
        the child's REAL terminal state from its own EventLog and renders the
        right notice (or drops it for a cancelled / non-terminal child). The
        delivery hook must not block (it offloads to a daemon drive thread).

        ONE drive outcome is special-cased: a ``TaskCancellationRequested`` (the
        session was cancelled / closed, so the child's ``cancel_check`` aborted
        its drive). The foreground concurrent-group path marks such a member
        terminal via ``_emit_child_cancelled``; the background path must do the
        same here, or the child stays a non-terminal orphan — and a later
        crash-recovery scan (``_child_is_terminal`` → False) would re-drive a
        cancelled child to completion. We write the child's own ``TaskCancelled``
        and skip delivery (the session is being torn down — nothing to push)."""
        with self._lock:
            kids = self._inflight.get(parent_task_id)
            if kids is not None:
                kids.discard(child_task_id)
                if not kids:
                    self._inflight.pop(parent_task_id, None)
        exc = future.exception()
        if isinstance(exc, TaskCancellationRequested):
            self._mark_child_cancelled(child_task_id)
            return
        if exc is not None:
            _log.warning(
                "background sub-agent %s drive raised: %r", child_task_id, exc
            )
        try:
            self._deliver(parent_task_id, child_task_id)
        except Exception:  # noqa: BLE001 — a background backstop never crashes
            _log.warning(
                "background sub-agent %s delivery failed",
                child_task_id,
                exc_info=True,
            )

    def _mark_child_cancelled(self, child_task_id: str) -> None:
        """Write a terminal ``TaskCancelled`` on a cancelled background child's
        OWN stream (mirrors ``subtask_drain._emit_child_cancelled``).

        Idempotent: skips a child with no genesis yet, or one that already
        reached a terminal (it finished a hair before the cancel landed). No
        lease — an observer-style ``system_emit``; the aborted drive already
        released the child's lease (``_descend_to_child`` fails it before
        re-raising), so this does not race the Engine's single RuntimeState
        writer."""
        events = list(self._event_log.read(child_task_id))
        if not events or any(
            env.type in ("TaskCancelled", "TaskCompleted", "TaskFailed")
            for env in events
        ):
            return
        self._event_log.system_emit(
            task_id=child_task_id,
            type="TaskCancelled",
            payload=TaskCancelledPayload(reason="parent-cancelled", cascade=True),
            actor="cancel-cascade",
            origin="system",
            trace_id=events[0].trace_id,
        )

    # -- session teardown --------------------------------------------------

    def forget_session(self, parent_task_id: str) -> None:
        """Drop a session's in-flight tracking (cancel / close cascade).

        The drive itself is torn down cooperatively by the ``cancel_check`` the
        ``DrainHost`` threads into each child step (the control-plane ``cancel`` /
        ``close`` marks the cancellation registry); this just frees the table so
        the per-session cap is restored. Idempotent."""
        with self._lock:
            self._inflight.pop(parent_task_id, None)

    # -- crash recovery ----------------------------------------------------

    def recover(self) -> list[str]:
        """Re-drive / re-deliver background sub-agents orphaned by a host crash.

        A restart loses this in-memory table; the event log holds each parent's
        ``BackgroundSubagentStarted`` and (for delivered ones) a matching
        ``BackgroundSubagentDelivered``. For every Started WITHOUT a Delivered:

        * the child's own stream is **non-terminal** → re-enqueue + re-submit the
          drive. ``_descend_to_child`` is resume-safe (it skips re-seeding the
          goal when the child already has messages), so the child continues from
          its own EventLog and delivers normally when it finishes.
        * the child's own stream is **terminal** (it finished, but the crash lost
          the turn-boundary notice) → re-deliver directly (no re-drive).

        Runs ONCE at live host startup (mirrors
        ``ProcessRegistry.recover_orphans``); it is a startup side effect, never
        re-derived from the log on a resume that folds it. Requires the event log
        to expose the task-stream index (live InMemory / Sqlite do); a non-index
        test double recovers nothing. Returns the recovered child ids."""
        index = self._event_log
        if not hasattr(index, "list_task_streams"):
            return []
        recovered: list[str] = []
        for summary in index.list_task_streams():  # type: ignore[attr-defined]
            for parent_id, child_id in self._undelivered(summary.task_id):
                if self._child_is_terminal(child_id):
                    # finished but the notice was lost — re-deliver only.
                    self._safe_deliver(parent_id, child_id)
                else:
                    with self._lock:
                        self._inflight.setdefault(parent_id, set()).add(child_id)
                    self._submit(parent_id, child_id)
                recovered.append(child_id)
        return recovered

    def _undelivered(self, task_id: str) -> list[tuple[str, str]]:
        """``(parent_id, child_id)`` pairs on ``task_id``'s stream with a
        ``BackgroundSubagentStarted`` but no later ``BackgroundSubagentDelivered``."""
        started: dict[str, str] = {}
        delivered: set[str] = set()
        for env in self._event_log.read(task_id):
            if env.type == "BackgroundSubagentStarted":
                started[env.payload.subtask_id] = task_id
            elif env.type == "BackgroundSubagentDelivered":
                delivered.add(env.payload.subtask_id)
        return [
            (parent, child)
            for child, parent in started.items()
            if child not in delivered
        ]

    def _child_is_terminal(self, child_id: str) -> bool:
        for env in self._event_log.read(child_id):
            if env.type in ("TaskCompleted", "TaskFailed", "TaskCancelled"):
                return True
        return False

    def _safe_deliver(self, parent_id: str, child_id: str) -> None:
        try:
            self._deliver(parent_id, child_id)
        except Exception:  # noqa: BLE001 — recovery never crashes startup
            _log.warning(
                "background sub-agent %s re-delivery failed",
                child_id,
                exc_info=True,
            )
