"""A task's kernel timers: firing the due ones and when the next is due."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from noeta.core.events import TimerFired
from noeta.core.store import Post

if TYPE_CHECKING:
    from noeta.core.loop.state import TaskState
    from noeta.core.runner.task import TaskRun


async def fire_timers(run: TaskRun) -> None:
    """Post the due timers to the inbox while the task is idle. A turn
    in progress keeps them for later, also while it is parked on an
    approval, a question or children: Claude Code fires a scheduled
    prompt only once it is idle, so a timer due meanwhile fires (once)
    when the turn ends. The dedupe key, the record that
    set the timer, makes a crash between post and take harmless and
    lets a timer set again for the same name and time fire again."""
    st = run.state
    if st.turn_open or st.calls or st.ended is not None or not st.timers:
        return
    now = time.time()
    for name, timer in sorted(st.timers.items(), key=lambda kv: kv[1]["at"]):
        if timer["at"] <= now:
            await run.store.post(Post(task_id=run.task_id, body=TimerFired(name=name),
                                      dedupe_key=f"timer:{name}:{timer['seq']}"))


def next_timer(state: TaskState) -> float | None:
    """When the earliest timer is due, or ``None`` without timers."""
    return min((t["at"] for t in state.timers.values()), default=None)
