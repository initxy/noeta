"""The whole conversation of a task, read from its log: what a compaction's
saved transcript holds. The fold keeps only the compacted conversation."""

from __future__ import annotations

from noeta.core.events import Compacted, TaskCreated
from noeta.core.extension import ReadLog
from noeta.core.loop.state import TaskState, continued, fold
from noeta.core.types import Message

#: How far the conversations a task continues are followed back.
MAX_DEPTH = 64


async def full_conversation(read_log: ReadLog, task_id: str) -> list[Message]:
    """Every message the task's conversation added, in order, never
    compacted: its log folded without its compactions, on the conversation
    it continues (``history_from``, read the same way)."""
    return (await _uncompacted(read_log, task_id, None, 0)).messages


async def _uncompacted(read_log: ReadLog, task_id: str, through: int | None,
                       depth: int) -> TaskState:
    log = [r for r in await read_log(task_id) if through is None or r.seq <= through]
    first = log[0].event if log else None
    base = None
    if isinstance(first, TaskCreated) and first.history_from and depth < MAX_DEPTH:
        base = continued(await _uncompacted(read_log, first.history_from,
                                            first.history_through, depth + 1))
    return fold((r for r in log if not isinstance(r.event, Compacted)), {}, state=base)
