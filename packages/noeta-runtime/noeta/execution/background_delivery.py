"""Mechanism C — deliver a finished background activity to its session.

A background activity — a ``shell_run(run_in_background=True)`` command
(docs/adr/shell-permission-and-background.md) or a
``spawn_subagent(background=True)`` sub-agent
(docs/adr/background-subagent.md) — may terminate while its session sits idle on
the next-goal suspend, or while it is still mid-turn. Either way its result must
be surfaced at a **turn boundary**, proactively, without the user asking again
and without the model polling. The two paths were described as "isomorphic" in
the ADRs and had duplicated the same host-side glue; this module is that glue,
given one home:

* the non-blocking daemon-thread hop off the watcher / executor callback (which
  MUST NOT block);
* the parent-fold terminal check — a terminal session has no turn to wake, so
  the push is dropped and the durable exit event stays for audit;
* the mid-turn-deferral retry loop — a parent still advancing its own turn is
  re-attempted until it settles or a bounded deadline elapses. Determinism-safe:
  retrying only changes WHEN the notice turn is injected, never the recorded
  bytes.

The two tenants differ only in how they PROJECT a finished activity into a
completion notice (a pointer + job handle for a command; a dereferenced, inlined
sub-agent result behind an exactly-once delivery anchor for a sub-agent). That
projection is the :data:`PlanFn` the caller supplies — everything else, the part
that was copy-pasted twice and had already drifted (the sub-agent path grew a
bounded retry the command path lacked), lives here once.

Code-agnostic by contract: imports only ``noeta.core`` (the parent fold) — no
product, no transport.
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
#: delivers on the first attempt (no wait); a never-settling parent gives up here
#: rather than leaking a daemon thread. Bounded and determinism-safe — retrying
#: only shifts WHEN the notice turn is injected, never the recorded bytes.
DEFAULT_DELIVER_TIMEOUT_S = 30.0
DEFAULT_DELIVER_POLL_S = 0.05

#: Push the completion notice + drive the turn, given the wired notifier (an
#: ``InteractionDriver``). MUST raise when the session is not idle-suspended on
#: its next-goal handle (mid-turn / terminal) so the loop defers / retries.
DeliverFn = Callable[[Any], None]

#: Project a finished activity into a :data:`DeliverFn`, run ONCE on the delivery
#: thread before the retry loop. Returns ``None`` to drop the delivery entirely
#: (nothing to deliver — e.g. a cancelled sub-agent whose session is being torn
#: down). Any read of the activity's own stream belongs here, so the retry loop
#: re-attempts only the notify, not the projection.
PlanFn = Callable[[], Optional[DeliverFn]]


class BackgroundDelivery:
    """Host-side Mechanism-C delivery glue shared by the two background tenants.

    One instance per host. Holds the host's shared L0 read seams (``event_log`` /
    ``content_store``, used only to fold the parent's status) and the completion
    notifier, which the product wires late via :meth:`set_notifier` — the driver
    wraps the host, so the host cannot construct it. Inert until a notifier is
    wired: a background exit still records its durable event, but drives no
    wake-and-notify turn (oneshot / lifecycle / tests stay byte-identical, no
    push).
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
        session_id: str,
        plan: PlanFn,
        thread_name: str,
        retry_timeout_s: float = DEFAULT_DELIVER_TIMEOUT_S,
        poll_s: float = DEFAULT_DELIVER_POLL_S,
    ) -> None:
        """Hand a finished background activity to a daemon delivery thread.

        Runs on the watcher / executor callback thread, so it MUST NOT block: it
        spawns a short-lived daemon thread that runs :meth:`drive` and returns at
        once. No-op until a notifier is wired (the durable exit event is the
        authoritative record regardless)."""
        notifier = self._notifier
        if notifier is None:
            return
        threading.Thread(
            target=self.drive,
            args=(notifier, session_id, plan),
            kwargs={"retry_timeout_s": retry_timeout_s, "poll_s": poll_s},
            name=thread_name,
            daemon=True,
        ).start()

    def drive(
        self,
        notifier: Any,
        session_id: str,
        plan: PlanFn,
        *,
        retry_timeout_s: float = DEFAULT_DELIVER_TIMEOUT_S,
        poll_s: float = DEFAULT_DELIVER_POLL_S,
    ) -> None:
        """Turn-boundary completion push (synchronous; the daemon-thread body).

        Projects the activity once (``plan``; ``None`` ⇒ nothing to deliver, drop),
        then the three-state loop:

        * **terminal session** — no turn to wake (cancelled / failed); drop the
          push, the durable exit event stays for audit.
        * **idle-suspended on NEXT_GOAL** — ``deliver`` wakes + drives a fresh
          notice turn and returns.
        * **mid-turn / any other suspend** — ``deliver`` raises
          (``_require_human_suspend`` rejects a non-next-goal-suspended task);
          re-attempt until the session settles or ``retry_timeout_s`` elapses
          (``0.0`` ⇒ single attempt). A background backstop must never crash, so a
          persistent mid-turn state is swallowed as a deferred no-op — the durable
          exit event still records the fact, and recovery / the next poll surfaces
          it."""
        deliver = plan()
        if deliver is None:
            return  # cancelled / nothing to deliver
        deadline = time.monotonic() + retry_timeout_s
        while True:
            task = fold(self._event_log, self._content_store, session_id)
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
                        session_id,
                        retry_timeout_s,
                    )
                    return
                time.sleep(poll_s)
