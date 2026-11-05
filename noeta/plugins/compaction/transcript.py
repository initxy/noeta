"""The whole conversation of a task, read from its log: what a compaction's
saved transcript holds. The fold keeps only the compacted conversation."""

from __future__ import annotations

from collections.abc import Sequence

from noeta.core.events import Compacted, Recorded, TaskCreated
from noeta.core.extension import ReadLog
from noeta.core.loop.state import TaskState, continued, fold
from noeta.core.records import spawning_call
from noeta.core.types import Message

#: How far the conversations a task continues are followed back.
MAX_DEPTH = 64


async def full_conversation(read_log: ReadLog, task_id: str) -> list[Message]:
    """Every message the task's conversation added, in order, never
    compacted: its log folded without its compactions, on the conversation
    it continues (``history_from``, read the same way)."""
    return (await _uncompacted(read_log, task_id, list(await read_log(task_id)), 0)).messages


async def _uncompacted(read_log: ReadLog, task_id: str, log: Sequence[Recorded],
                       depth: int) -> TaskState:
    """``log`` (task ``task_id``'s) folded without its compactions, on the
    conversation it continues, the source's call that started it (a fork's
    ``Agent`` call) answered as the runner answers it."""
    first = log[0].event if log else None
    base = None
    if isinstance(first, TaskCreated) and first.history_from and depth < MAX_DEPTH:
        source = list(await read_log(first.history_from))
        through = first.history_through
        base = continued(await _uncompacted(
            read_log, first.history_from,
            [r for r in source if through is None or r.seq <= through], depth + 1),
            started_by=spawning_call(source, task_id))
    return fold((r for r in log if not isinstance(r.event, Compacted)), {}, state=base)
