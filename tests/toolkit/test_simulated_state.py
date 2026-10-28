"""``simulated_state`` folds pending records on a copy the live state never sees."""

from __future__ import annotations

from typing import Any

from noeta.core.events import ExtensionRecorded
from noeta.core.extension import TaskView
from noeta.core.loop.state import CORE, TaskState
from noeta.toolkit.context import simulated_state


async def _no_compaction(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("not called")


def test_folding_pending_records_leaves_the_live_state_alone() -> None:
    state = TaskState()
    state.timers["kept"] = {"at": 1.0, "text": "", "seq": 0}
    view = TaskView(state=state, complete=_no_compaction, env=None)
    view.pending = [
        ExtensionRecorded(extension=CORE, kind="timer", data={"name": "new", "at": 2.0}),
        ExtensionRecorded(extension=CORE, kind="timer_cleared", data={"name": "kept"}),
    ]
    sim = simulated_state(view)
    assert set(sim.timers) == {"new"}
    assert set(state.timers) == {"kept"}
