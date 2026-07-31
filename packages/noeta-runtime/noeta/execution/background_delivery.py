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
]

_log = logging.getLogger("noeta.execution.background_delivery")

#: How long the delivery thread keeps re-attempting while the parent session is
#: still mid-turn (its spawning turn outran the activity). A settled parent
#: delivers on the first attempt; the bound is what stops a never-settling parent
#: from leaking a daemon thread.
DEFAULT_DELIVER_TIMEOUT_S = 30.0
DEFAULT_DELIVER_POLL_S = 0.05

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

    def set_notifier(self, notifier: Any) -> None:
        """Wire the completion notifier (the ``InteractionDriver``). Idempotent."""
        self._notifier = notifier

    def on_exit(
        self,
        *,
        task_id: str,
        plan: PlanFn,
        thread_name: str,
        retry_timeout_s: float = DEFAULT_DELIVER_TIMEOUT_S,
        poll_s: float = DEFAULT_DELIVER_POLL_S,
    ) -> None:
        """Hand a finished background activity to a daemon delivery thread.

        Runs on the watcher / executor callback thread, so it MUST NOT block —
        hence the short-lived daemon thread. No-op until a notifier is wired; the
        durable exit event is the authoritative record either way."""
        notifier = self._notifier
        if notifier is None:
            return
        threading.Thread(
            target=self.drive,
            args=(notifier, task_id, plan),
            kwargs={"retry_timeout_s": retry_timeout_s, "poll_s": poll_s},
            name=thread_name,
            daemon=True,
        ).start()

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
        re-attempted until the session settles or ``retry_timeout_s`` elapses
        (``0.0`` ⇒ a single attempt). A background backstop must never crash, so
        a parent that never settles is swallowed as a deferred no-op."""
        deliver = plan()
        if deliver is None:
            return  # cancelled / nothing to deliver
        deadline = time.monotonic() + retry_timeout_s
        while True:
            task = fold(self._event_log, self._content_store, task_id)
            if task.status == "terminal":
                return  # no turn to wake — the exit event stands for audit
            try:
                deliver(notifier)
                return
            except Exception:  # noqa: BLE001 — mid-turn defer; never crash a backstop
                if time.monotonic() >= deadline:
                    _log.debug(
                        "background completion for session %s deferred (not "
                        "idle-suspended on next-goal within %.1fs)",
                        task_id,
                        retry_timeout_s,
                    )
                    return
                time.sleep(poll_s)
