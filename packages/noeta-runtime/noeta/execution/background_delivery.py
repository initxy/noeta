"""The shared background-delivery seam: surface a finished background activity —
a backgrounded shell command or a background sub-agent — in its session at a
turn boundary, proactively, without the user asking again and the model polling.

The two tenants differ only in how they project a finished activity into a
completion notice, and that projection is the :data:`PlanFn` the caller
supplies. Everything else lives here once: the non-blocking hop off the watcher
/ executor callback, the terminal-parent drop, and the bounded mid-turn retry.
Retrying is determinism-safe because it changes only WHEN the notice turn is
injected, never the recorded bytes.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from noeta.core.fold import fold


__all__ = [
    "BackgroundDelivery",
    "DeliverFn",
    "PlanFn",
    "DEFAULT_DELIVER_TIMEOUT_S",
    "DEFAULT_DELIVER_POLL_S",
    "DEFAULT_DELIVER_MAX_POLL_S",
]

_log = logging.getLogger("noeta.execution.background_delivery")

#: How long the delivery thread keeps re-attempting while the parent session is
#: still mid-turn (its spawning turn outran the activity). A settled parent
#: delivers on the first attempt.
#:
#: The bound is a leak guard, NOT a delivery deadline: once it expires a
#: sub-agent result is re-pushed only by the host's re-scan when one of the
#: session's turns settles (or by the recovery scan at the next Client start),
#: so a bound shorter than a long turn delays the result of work that actually
#: finished. It is therefore set well past any
#: plausible turn, and the loop stops early on the one state that really has
#: nowhere to deliver to — a terminal parent.
DEFAULT_DELIVER_TIMEOUT_S = 3600.0
#: First retry interval. The wait backs off (doubling, capped at
#: :data:`DEFAULT_DELIVER_MAX_POLL_S`) so a parent that stays busy for minutes
#: costs a fold a second rather than twenty.
DEFAULT_DELIVER_POLL_S = 0.05
DEFAULT_DELIVER_MAX_POLL_S = 1.0

#: Push the completion notice and drive the turn, given the wired notifier. MUST
#: raise when the session is not idle-suspended on its next-goal handle, since
#: that raise is how the loop learns to defer and retry.
DeliverFn = Callable[[Any], None]

#: Project a finished activity into a :data:`DeliverFn`, run ONCE on the delivery
#: thread before the retry loop; ``None`` drops the delivery entirely. Any read
#: of the activity's own stream belongs here, so the retry loop re-attempts only
#: the notify, never the projection.
PlanFn = Callable[[], Optional[DeliverFn]]


class BackgroundDelivery:
    """Host-side delivery glue shared by the two background tenants.

    One instance per host, holding the L0 read seams (used only to fold the
    parent's status) and the completion notifier, which is wired late via
    :meth:`set_notifier` because the driver wraps the host and so cannot exist
    when the host is constructed. Inert until then: a background exit still
    records its durable event, but drives no wake-and-notify turn.
    """

    def __init__(self, *, event_log: Any, content_store: Any) -> None:
        self._event_log = event_log
        self._content_store = content_store
        self._notifier: Optional[Any] = None
        self._lock = threading.Lock()
        # Keys of deliveries whose drive thread is still running (waiting for
        # the parent to settle, or pushing). A second hand-off for the same
        # key is dropped, so a re-scan can never push one result twice.
        self._pending: set[str] = set()
        # Set by :meth:`close`: nothing new is handed off and a waiting drive
        # stops instead of driving a turn on a shut-down host.
        self._closed = False

    def set_notifier(self, notifier: Any) -> None:
        """Wire the completion notifier (the ``InteractionDriver``). Idempotent."""
        self._notifier = notifier

    def close(self) -> None:
        """Stop delivering — the owning Client is shutting down.

        A hand-off after this is a no-op and a drive still waiting for its
        parent returns at its next attempt without pushing. The activity's
        durable exit event stands; a background sub-agent's result stays
        undelivered for the next Client's recovery scan. Idempotent."""
        self._closed = True

    def is_pending(self, key: str) -> bool:
        """True while a delivery handed off under ``key`` is still running."""
        with self._lock:
            return key in self._pending

    def on_exit(
        self,
        *,
        task_id: str,
        plan: PlanFn,
        thread_name: str,
        retry_timeout_s: float = DEFAULT_DELIVER_TIMEOUT_S,
        poll_s: float = DEFAULT_DELIVER_POLL_S,
        key: Optional[str] = None,
    ) -> None:
        """Hand a finished background activity to a daemon delivery thread.

        Runs on the watcher / executor callback thread, so it MUST NOT block —
        hence the short-lived daemon thread. No-op until a notifier is wired, and
        after :meth:`close`; the durable exit event is the authoritative record
        either way. ``key`` names the activity: while a delivery under the same
        key is still running, another hand-off for it is dropped."""
        notifier = self._notifier
        if notifier is None or self._closed:
            return
        if key is not None:
            with self._lock:
                if key in self._pending:
                    return
                self._pending.add(key)

        def _run() -> None:
            try:
                self.drive(
                    notifier, task_id, plan,
                    retry_timeout_s=retry_timeout_s, poll_s=poll_s,
                )
            finally:
                if key is not None:
                    with self._lock:
                        self._pending.discard(key)

        try:
            threading.Thread(target=_run, name=thread_name, daemon=True).start()
        except BaseException:
            if key is not None:
                with self._lock:
                    self._pending.discard(key)
            raise

    def drive(
        self,
        notifier: Any,
        task_id: str,
        plan: PlanFn,
        *,
        retry_timeout_s: float = DEFAULT_DELIVER_TIMEOUT_S,
        poll_s: float = DEFAULT_DELIVER_POLL_S,
    ) -> None:
        """Turn-boundary completion push (synchronous; the daemon-thread body).

        Three parent states, one loop: a terminal session has no turn to wake, so
        the push is dropped and the durable exit event stands for audit; a
        session idle-suspended on its next-goal handle is woken and driven
        through one notice turn; anything else makes ``deliver`` raise and is
        re-attempted — with a backing-off wait — until the session settles, goes
        terminal, or ``retry_timeout_s`` elapses (``0.0`` ⇒ a single attempt).

        The notice waits for the parent however long its turn runs: the result
        is real work the model asked for, and giving up on it merely because the
        parent stayed busy loses it until the next process start. A background
        backstop must never crash, so the leak guard expiring is still swallowed
        — but loudly, because by then a finished sub-agent's answer is gone."""
        if self._closed:
            return
        deliver = plan()
        if deliver is None:
            return  # cancelled / nothing to deliver
        deadline = time.monotonic() + retry_timeout_s
        wait = poll_s
        while True:
            if self._closed:
                return  # the host shut down — leave it for the next start
            task = fold(self._event_log, self._content_store, task_id)
            if task.status == "terminal":
                return  # no turn to wake — the exit event stands for audit
            try:
                deliver(notifier)
                return
            except Exception:  # noqa: BLE001 — mid-turn defer; never crash a backstop
                if time.monotonic() >= deadline:
                    _log.warning(
                        "background completion for session %s DEFERRED (still "
                        "not idle-suspended on next-goal after %.1fs); a "
                        "sub-agent result is re-pushed when a turn of the "
                        "session settles or at the next start",
                        task_id,
                        retry_timeout_s,
                    )
                    return
                time.sleep(wait)
                wait = min(wait * 2, DEFAULT_DELIVER_MAX_POLL_S)
