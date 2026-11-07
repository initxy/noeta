"""Fold a loaded task's log both ways and compare: from the start, and from
a snapshot (``noeta.core.snapshots``) taken at several seqs plus the events
after it. Run on every load in the test suite (``tests/conftest.py``)."""

from __future__ import annotations

import dataclasses
from typing import Any

from noeta.core import snapshots
from noeta.core.loop.state import TaskState, fold, new_state

#: Split points tried per loaded log (spread over it, ends included).
SPLITS = 6


def _diff(a: TaskState, b: TaskState) -> list[str]:
    return [f.name for f in dataclasses.fields(TaskState)
            if getattr(a, f.name) != getattr(b, f.name)]


async def _fresh(run: Any, records: list[Any]) -> TaskState:
    first = records[0].event if records else None
    initial = {e.name: e.initial_state() for e in run.all_exts}
    base = await run._history(first, initial) or new_state(initial)
    base.task_id = run.task_id
    return base


async def check_both_ways(run: Any) -> None:
    if run.agent is None:
        return
    records = await run.store.read(run.task_id)
    full = fold(records, run.reducers, state=await _fresh(run, records), also=run.also)
    assert run.state == full, f"{run.task_id}: the loaded state differs from a full fold " \
                              f"in {_diff(run.state, full)}"
    n = len(records)
    for k in sorted({round(i * n / SPLITS) for i in range(SPLITS + 1)}):
        prefix = fold(records[:k], run.reducers, state=await _fresh(run, records), also=run.also)
        data = snapshots.encode(prefix)
        if data is None:
            continue  # not snapshotted: the log is folded from the start
        state = fold(records[k:], run.reducers, state=snapshots.decode(data), also=run.also)
        assert state == full, f"{run.task_id}: snapshot at seq {k} + tail differs from a " \
                              f"full fold in {_diff(state, full)}"
