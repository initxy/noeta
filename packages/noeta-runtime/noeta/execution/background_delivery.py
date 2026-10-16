"""The shared background-delivery seam: surface background activity in its
session proactively, without the user asking again and the model polling.

Two shapes. A background command's or a monitor's notice
(:meth:`BackgroundDelivery.post_notice`) is queued as a pending injection at
once — a running turn takes it in between tool calls — and a turn is driven
for it only when none is running to take it. A finished background sub-agent
(:meth:`BackgroundDelivery.on_exit`) is pushed at a turn boundary, its
projection being the :data:`PlanFn` the caller supplies. Both hop off the
watcher / executor callback, drop a terminal or closed session, and wait out a
busy one; waiting changes only WHEN a turn runs, never the recorded bytes.
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
        # Sessions with a settle thread running, and those posted to since
        # that thread last looked (so it looks again before it stops).
        self._settling: set[str] = set()
        self._dirty: set[str] = set()

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

    def post_notice(
        self,
        *,
        task_id: str,
        notice: str,
        retry_timeout_s: float = DEFAULT_DELIVER_TIMEOUT_S,
        poll_s: float = DEFAULT_DELIVER_POLL_S,
    ) -> None:
        """Queue ``notice`` for the model of ``task_id`` and see it delivered.

        Synchronous part (on the caller's thread, so notices keep their order):
        a terminal or closed session drops it; otherwise the notifier records
        it as a pending injection, which a running turn drains at its next
        top-of-loop. Then a per-session settle thread watches: once nothing is
        pending it stops; if the session parks on its next-goal handle with
        injections still pending — the notice landed as the turn ended, or
        while it was parked — it drives a turn that takes them in. No-op until
        a notifier is wired, and after :meth:`close`."""
        notifier = self._notifier
        if notifier is None or self._closed:
            return
        task = fold(self._event_log, self._content_store, task_id)
        if task.status == "terminal" or getattr(task.governance, "closed", False):
            return
        notifier.inject_notice(task_id, notice=notice)
        with self._lock:
            self._dirty.add(task_id)
            if task_id in self._settling:
                return
            self._settling.add(task_id)
        try:
            threading.Thread(
                target=self._settle,
                args=(notifier, task_id, retry_timeout_s, poll_s),
                name=f"noeta-bg-notice-{task_id}",
                daemon=True,
            ).start()
        except BaseException:
            with self._lock:
                self._settling.discard(task_id)
            raise

    def _settle(
        self, notifier: Any, task_id: str, retry_timeout_s: float, poll_s: float
    ) -> None:
        """Drive a turn for ``task_id``'s pending injections if no running
        turn will take them; stop once none is pending (see
        :meth:`post_notice`)."""
        deadline = time.monotonic() + retry_timeout_s
        wait = poll_s
        try:
            while not self._closed:
                with self._lock:
                    self._dirty.discard(task_id)
                task = fold(self._event_log, self._content_store, task_id)
                done = (
                    not task.governance.pending_injections
                    or task.status == "terminal"
                    or getattr(task.governance, "closed", False)
                )
                if done:
                    with self._lock:
                        if task_id not in self._dirty:
                            self._settling.discard(task_id)
                            return
                    continue
                try:
                    notifier.deliver_injections(task_id)
                    wait = poll_s
                    continue
                except Exception:  # noqa: BLE001 — not parked: a running turn takes them
                    pass
                if time.monotonic() >= deadline:
                    _log.warning(
                        "background notices for session %s still pending after "
                        "%.1fs; the session's next turn takes them in",
                        task_id,
                        retry_timeout_s,
                    )
                    break
                time.sleep(wait)
                wait = min(wait * 2, DEFAULT_DELIVER_MAX_POLL_S)
        finally:
            with self._lock:
                self._settling.discard(task_id)

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

        Three parent states, one loop: a terminal or closed session has no turn
        to wake, so the push is dropped and the durable exit event stands for
        audit; a
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
            if getattr(task.governance, "closed", False):
                # A closed conversation is done until a human goal reopens it:
                # a notice is not one, so it wakes nothing — the exit event
                # stands, and the close's own kill cascade is what ended most
                # of these jobs in the first place.
                return
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
