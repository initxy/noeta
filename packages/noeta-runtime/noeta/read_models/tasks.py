"""Task list read-model — one summary row per task stream.

Enumeration goes through the
:class:`noeta.protocols.event_log.EventLogTaskIndex` capability and lifecycle
state comes from :func:`noeta.core.fold.fold`, so the projection works against
any backend and never touches adapter internals.
"""

from __future__ import annotations

from typing import Any

from noeta.core.fold import fold
from noeta.protocols.content_store import ContentStore
from noeta.protocols.event_log import EventLogReader, EventLogTaskIndex


__all__ = ["list_task_summaries"]


def list_task_summaries(
    task_index: EventLogTaskIndex,
    event_log: EventLogReader,
    content_store: ContentStore,
) -> list[dict[str, Any]]:
    """Return one summary row per task stream, most-recent-update first.

    Row shape::

        {task_id, status, closed, last_seq, last_event_time, created_event_time,
         parent_task_id, agent_name, workspace_dir, background_jobs}

    ``closed`` is ORTHOGONAL to ``status``: a closed conversation is still
    ``suspended``. Both are folded per task rather than read off an Observer, so
    a row cannot disagree with the stream it summarises.

    ``parent_task_id`` is ``None`` for a root conversation, which is how a
    caller separates conversations from the subtasks they spawned.
    ``workspace_dir`` is an absolute path — sessions welded to the same
    workspace share it, and a session with no host binding has ``None``.
    ``background_jobs`` is folded from events emitted on the ROOT-TASK stream,
    so folding a root row surfaces every job, including ones a subtask spawned.

    ``task_index`` and ``event_log`` are typically the SAME concrete log, which
    satisfies both Protocols; they stay separate parameters so fold and resume
    keep depending only on the narrow ``EventLogReader``.
    """
    rows: list[dict[str, Any]] = []
    for summary in task_index.list_task_streams():
        folded = fold(event_log, content_store, summary.task_id)
        # The spawning agent name lives in the genesis ``TaskCreated``, so the
        # first event of the stream is the only place to read it from.
        stream = event_log.read(summary.task_id)
        created_event_time = (
            float(getattr(stream[0], "occurred_at", summary.last_event_time))
            if stream
            else float(summary.last_event_time)
        )
        agent_name = (
            getattr(stream[0].payload, "agent_name", "") if stream else ""
        )
        rows.append(
            {
                "task_id": summary.task_id,
                "status": folded.status,
                "closed": folded.governance.closed,
                "last_seq": summary.last_seq,
                "last_event_time": summary.last_event_time,
                "created_event_time": created_event_time,
                "parent_task_id": folded.parent_task_id,
                "agent_name": agent_name,
                "workspace_dir": folded.governance.workspace,
                "background_jobs": folded.governance.background_jobs,
            }
        )
    return rows
