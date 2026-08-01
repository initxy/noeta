"""The live, per-session table behind ``spawn_subagent(background=True)``: it
submits each background child's subtree to the shared fan-out executor so it runs
concurrently with the parent's continuing turn, and the parent never suspends.

This table is a runtime accelerator only. The durable trace is the
``BackgroundSubagent{Started,Delivered}`` pair on the parent stream plus the
child's own Task stream, so the registry is never persisted and has zero effect
on the recorded bytes; crash recovery rebuilds what it needs by scanning those
events. A :class:`threading.Lock` guards the in-flight table because ``launch``,
the executor drive, the done-callback and ``recover`` all arrive on different
threads.
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
    "DEFAULT_MAX_BACKGROUND_SUBAGENTS_PER_ROOT_TASK",
    "BackgroundSubagentRegistry",
]


#: Per-session background sub-agent concurrency cap, overridable via
#: ``HostConfig``. A launch over the ceiling is REJECTED rather than queued: a
#: clear "let one finish" refusal reads better to an agent than an invisible
#: wait. The count comes from the live table, never the log, so a rejected launch
#: writes no event.
DEFAULT_MAX_BACKGROUND_SUBAGENTS_PER_ROOT_TASK = 8


#: Build the delegation host for a parent's tree. The registry drives ONE
#: background child on it, holding only that child's lease.
BuildHostFn = Callable[[str], DrainHost]

#: The delivery hook fired once a background child reaches terminal (or its drive
#: raised). It MUST NOT block — it hands off to a daemon drive thread.
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
        max_per_root_task: int = DEFAULT_MAX_BACKGROUND_SUBAGENTS_PER_ROOT_TASK,
    ) -> None:
        self._event_log = event_log
        self._content_store = content_store
        self._dispatcher = dispatcher
        self._build_host = build_host
        self._deliver = deliver
        self._max_per_root_task = max_per_root_task
        self._lock = threading.Lock()
        # session-root task id -> set of in-flight background child ids.
        self._inflight: dict[str, set[str]] = {}

    def capacity(self, parent_task_id: str) -> Optional[str]:
        """Return a rejection reason when the session is at its background cap,
        else ``None``. The handler checks this BEFORE any durable write, so an
        over-cap launch leaves no trace."""
        with self._lock:
            running = len(self._inflight.get(parent_task_id, ()))
        if running >= self._max_per_root_task:
            return (
                f"too many background sub-agents "
                f"({running}/{self._max_per_root_task}) running for this session; "
                "let one finish before starting another"
            )
        return None

    def launch(self, *, parent_task_id: str, child_task_id: str) -> None:
        """Enqueue + submit a freshly-created background child to the executor.

        Non-blocking by contract: the drive runs on the shared bounded pool
        concurrently with the parent's turn, and the done-callback hands the
        child to the delivery hook."""
        with self._lock:
            self._inflight.setdefault(parent_task_id, set()).add(child_task_id)
        self._submit(parent_task_id, child_task_id)

    def _submit(self, parent_task_id: str, child_task_id: str) -> None:
        # A background child is not enqueued by the lifecycle observer, so the
        # registry must do it or the targeted child-lease finds nothing to lease.
        # ``reserved=True`` keeps it targeted-lease-only: only the descent that
        # seeds the child's goal may claim it, so a resident-worker pool's
        # untargeted poll cannot steal the unseeded child and drive it with an
        # empty message history. ``parent_task_id`` routes the child onto its
        # parent's queue for the re-enqueues after its first claim.
        self._dispatcher.enqueue(
            child_task_id, reserved=True, parent_task_id=parent_task_id
        )
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

        A drive that raised (an unsupported mid-flight approval, a cancellation)
        is logged but STILL delivered: the delivery hook reads the child's real
        terminal state from its own EventLog and renders the right notice, or
        drops it.

        ``TaskCancellationRequested`` is the one special case — the session was
        cancelled or closed and the child's ``cancel_check`` aborted the drive.
        Its ``TaskCancelled`` must be written here, or the child stays a
        non-terminal orphan and a later crash-recovery scan re-drives a cancelled
        child to completion. Delivery is skipped: the session is being torn down,
        so there is nothing to push."""
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
        OWN stream.

        Idempotent: skips a child with no genesis yet, or one that already
        reached a terminal because it finished a hair before the cancel landed.
        Leaseless ``system_emit`` is safe here — the aborted drive already
        released the child's lease, so this cannot race the Engine's single
        RuntimeState writer."""
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

    def forget_root_task(self, parent_task_id: str) -> None:
        """Drop a session's in-flight tracking (cancel / close cascade).

        The drives themselves are torn down cooperatively by the ``cancel_check``
        the :class:`DrainHost` threads into each child step; this only frees the
        table so the per-session cap is restored. Idempotent."""
        with self._lock:
            self._inflight.pop(parent_task_id, None)

    def recover(self) -> list[str]:
        """Re-drive / re-deliver background sub-agents orphaned by a host crash.

        A restart loses the in-memory table, so the log is the truth: every
        ``BackgroundSubagentStarted`` without a matching
        ``BackgroundSubagentDelivered`` is an orphan. A non-terminal child is
        re-enqueued and re-submitted — the descent is resume-safe, skipping the
        goal re-seed when the child already has messages — while a terminal child
        only lost its turn-boundary notice and is re-delivered without a
        re-drive.

        Runs ONCE at live host startup as a side effect, never re-derived by a
        resume that folds the log. Requires the event log to expose the
        task-stream index; a test double without one recovers nothing."""
        index = self._event_log
        if not hasattr(index, "list_task_streams"):
            return []
        recovered: list[str] = []
        for summary in index.list_task_streams():  # type: ignore[attr-defined]
            for parent_id, child_id in self._undelivered(summary.task_id):
                if self._child_is_terminal(child_id):
                    self._safe_deliver(parent_id, child_id)
                elif not self._safe_submit(parent_id, child_id):
                    # One unrecoverable record must not be counted as recovered
                    # nor stop the scan.
                    continue
                recovered.append(child_id)
        return recovered

    def _undelivered(self, task_id: str) -> list[tuple[str, str]]:
        """``(parent_id, child_id)`` pairs started on ``task_id``'s stream but
        never delivered."""
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

    def _safe_submit(self, parent_id: str, child_id: str) -> bool:
        """Wrap a recovery re-submit so ONE corrupted record cannot abort host
        startup — the caller invokes ``recover()`` with no try/except of its own.
        Registers the child in-flight first, so a drive that starts before
        failing is still tracked, and rolls that back when ``_submit`` raises so a
        failed record leaves no phantom entry blocking the session's cap."""
        with self._lock:
            self._inflight.setdefault(parent_id, set()).add(child_id)
        try:
            self._submit(parent_id, child_id)
        except Exception:  # noqa: BLE001 — recovery never crashes startup
            with self._lock:
                kids = self._inflight.get(parent_id)
                if kids is not None:
                    kids.discard(child_id)
                    if not kids:
                        self._inflight.pop(parent_id, None)
            _log.warning(
                "background sub-agent %s recovery re-submit failed",
                child_id,
                exc_info=True,
            )
            return False
        return True
