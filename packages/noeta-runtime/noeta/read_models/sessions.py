"""Session / task list read-model (CW5a Phase 1).

Builds the ``GET /tasks`` summary rows the Web SPAs display. Extracted verbatim
(same shape, same order) from the former private list helper so the move is
behavior-preserving — but it now enumerates via the
:class:`noeta.protocols.event_log.EventLogTaskIndex` capability instead of
reaching into adapter privates (``_streams`` / ``_conn``), and folds each task
through :func:`noeta.core.fold.fold` for its lifecycle state.
"""

from __future__ import annotations

from typing import Any

from noeta.core.fold import fold
from noeta.protocols.content_store import ContentStore
from noeta.protocols.event_log import EventLogReader, EventLogTaskIndex


__all__ = ["list_session_summaries"]


def list_session_summaries(
    task_index: EventLogTaskIndex,
    event_log: EventLogReader,
    content_store: ContentStore,
) -> list[dict[str, Any]]:
    """Return one summary row per task stream, most-recent-update first.

    Row shape::

        {task_id, status, closed, last_seq, last_event_time, created_event_time,
         parent_task_id, agent_name, workspace_dir, background_jobs}

    ``parent_task_id`` is the spawning task for a subtask (from
    ``TaskCreated.parent_task_id``), or ``None`` for a root conversation — the
    Web session list filters subtasks out by it.

    ``workspace_dir`` is the session's welded workspace
    **absolute path**, folded from the single ``TaskHostBound`` event. The Web
    session list groups sessions by this path: multiple sessions opened against
    one registered workspace share the same ``workspace_dir`` → one group; a
    bare session's private ``<base>/session-<uuid>`` dir is unique → ungrouped.
    ``None`` for old (name-era) recordings or sessions with no host binding —
    those land in the ungrouped bucket too. The front-end maps the path back to
    a display name via the ``/capabilities`` registry ``[{id, name, path}]``.

    ``background_jobs`` is the session's append-only list of
    background-shell jobs, folded from the ``BackgroundShell*`` events. Issue 04
    emits those on the SESSION ROOT stream, so folding the root row surfaces
    every job — incl. ones a subtask spawned. Each entry carries
    ``{job_id, command, status, spawned_by_task_id, ref?, exit_code?, signal?}``;
    the front-end chip reads it and derefs ``ref`` for the job's output.

    ``status`` and ``closed`` are derived by folding each task (never from an
    Observer); ``closed`` is ORTHOGONAL to ``status`` — a closed conversation is
    still ``suspended``. Ordering comes from ``list_task_streams()`` (recency
    desc, deterministic ``task_id`` tie-break) and is preserved here.

    ``task_index`` and ``event_log`` are typically the SAME concrete log (it
    satisfies both Protocols); they are separate parameters so the dependency on
    the enumeration capability is explicit and fold/resume keep needing only the
    narrow ``EventLogReader``.
    """
    rows: list[dict[str, Any]] = []
    for summary in task_index.list_task_streams():
        folded = fold(event_log, content_store, summary.task_id)
        # The spawning agent name lives in the genesis ``TaskCreated`` — surface
        # it so the Web trace can label each node of the subtask tree (the
        # reserved ``__workflow__`` orchestration name included).
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
                # the welded workspace absolute path (or None) the
                # Web session list groups sessions by.
                "workspace_dir": folded.governance.workspace,
                "background_jobs": folded.governance.background_jobs,
            }
        )
    return rows
