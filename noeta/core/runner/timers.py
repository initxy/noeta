"""A task's kernel timers: firing the due ones and when the next is due."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from noeta.core.events import TimerFired
from noeta.core.loop.state import timer_held
from noeta.core.store import Post

if TYPE_CHECKING:
    from noeta.core.loop.state import TaskState
    from noeta.core.runner.task import TaskRun


async def fire_timers(run: TaskRun, now: float | None = None) -> None:
    """Post the due timers to the inbox while the task is idle. A turn
    in progress keeps them for later, also while it is parked on an
    approval, a question or children: Claude Code fires a scheduled
    prompt only once it is idle, so a timer due meanwhile fires (once)
    when the turn ends. The dedupe key, the record that
    set the timer, makes a crash between post and take harmless and
    lets a timer set again for the same name and time fire again; a
    firing taken while its plugin was off (``held``) fires anew. A timer
    of a plugin the task runs without waits (``timer_held``).
    ``now`` defaults to this worker's clock."""
    st = run.state
    if st.turn_open or st.calls or st.ended is not None or not st.timers:
        return
    if now is None:
        now = time.time()
    for name, timer in sorted(st.timers.items(), key=lambda kv: kv[1]["at"]):
        if timer["at"] <= now and not timer_held(st, name):
            held = f":{timer['held']}" if timer.get("held") else ""
            await run.store.post(Post(task_id=run.task_id, body=TimerFired(name=name),
                                      dedupe_key=f"timer:{name}:{timer['seq']}{held}"))


def next_timer(state: TaskState) -> float | None:
    """When the earliest timer is due, or ``None`` without timers (a held
    one, ``timer_held``, counts as none)."""
    return min((t["at"] for n, t in state.timers.items() if not timer_held(state, n)),
               default=None)
