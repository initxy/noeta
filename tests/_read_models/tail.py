"""read_models.tail — `noeta code tail` event rows (generic EventLog, pure read).

Reads a session's raw EventLog stream into one short deterministic line per
event (sequence / wall-clock / type / one-line detail gloss) for ``noeta code
tail``. Pure: no clock, no sleep — the CLI owns the poll interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from noeta.protocols.event_log import EventLogReader


__all__ = [
    "TailRow",
    "_short",
    "_wake_repr",
    "_tail_detail",
    "tail_event_rows",
]


@dataclass(frozen=True, slots=True)
class TailRow:
    """One line of ``noeta code tail``: a sequence number, wall-clock time, the
    event ``type``, and a short deterministic one-line ``detail`` gloss."""

    seq: int
    occurred_at: float
    type: str
    detail: str


def _short(value: Any, limit: int = 40) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _wake_repr(wake: Any) -> str:
    if wake is None:
        return "-"
    handle = getattr(wake, "handle", None)
    if isinstance(handle, str):
        return handle
    return type(wake).__name__


def _tail_detail(env: Any) -> str:
    """Short, deterministic per-event gloss for the tail ``detail`` column.

    Defensive: every field read is a ``getattr`` and the whole body is guarded
    so an unexpected payload shape yields ``""`` rather than raising (CW6 gate
    "detail never raises")."""
    payload = env.payload
    event_type = env.type

    def g(key: str) -> Any:
        return getattr(payload, key, None)

    try:
        if event_type == "TaskCreated":
            return f"agent={g('agent_name')} goal={_short(g('goal'))}"
        if event_type == "ModelBound":
            return f"model={g('model')} by={g('principal_identity')}"
        if event_type in ("ToolCallStarted", "ToolCallApprovalRequested"):
            return f"{g('call_id')} {g('tool_name')}"
        if event_type == "ToolCallApprovalResolved":
            return f"{g('call_id')} {g('tool_name')} approved={g('approved')}"
        if event_type == "UserQuestionRequested":
            return f"{g('question_id')} questions={g('question_count')}"
        if event_type == "UserQuestionAnswered":
            return (
                f"{g('question_id')} answers={g('answer_count')} "
                f"answered_by={g('answered_by')}"
            )
        if event_type == "ToolCallDenied":
            return f"{g('call_id')} {g('tool_name')} reason={_short(g('reason'))}"
        if event_type == "ToolResultRecorded":
            return f"{g('call_id')} success={g('success')}"
        if event_type == "TaskSuspended":
            return f"wake={_wake_repr(g('wake_on'))}"
        if event_type == "TaskWoken":
            return f"wake_event={_wake_repr(g('wake_event'))}"
        if event_type == "ConversationClosed":
            return f"by={g('closed_by')} reason={_short(g('reason'))}"
        if event_type == "ConversationReopened":
            return f"by={g('reopened_by')} reason={_short(g('reason'))}"
        if event_type == "SubtaskSpawned":
            return f"{g('subtask_id')} agent={g('agent_name')}"
        if event_type == "TaskFailed":
            return f"reason={_short(g('reason'))}"
        if event_type == "TaskCompleted":
            return "answer recorded"
    except Exception:  # noqa: BLE001 — observation must never crash on payload
        return ""
    return ""


def tail_event_rows(
    event_log: EventLogReader,
    task_id: str,
    *,
    after_seq: Optional[int] = None,
) -> tuple[list[TailRow], int]:
    """Read the events strictly past ``after_seq`` (or the whole stream when
    ``None``) and return ``(rows, new_cursor)`` in append order.

    ``new_cursor`` is the max ``seq`` seen, or ``after_seq`` (defaulting to 0)
    when no new events — the ``--follow`` loop feeds it straight back as the
    next ``after_seq``. Pure: no clock / no sleep (the CLI owns the poll
    interval), so tests call this directly and deterministically."""
    cursor = after_seq if after_seq is not None else 0
    rows: list[TailRow] = []
    for env in event_log.read(task_id, after_seq=after_seq):
        seq = int(getattr(env, "seq", 0))
        rows.append(
            TailRow(
                seq=seq,
                occurred_at=float(getattr(env, "occurred_at", 0.0)),
                type=str(env.type),
                detail=_tail_detail(env),
            )
        )
        if seq > cursor:
            cursor = seq
    return rows, cursor
