"""Catalog projection: the session list, narrowed to code sessions (pure read).

Filters the generic session catalog down to tasks whose genesis
``TaskCreated.agent_name`` is a registered coding Agent (the canonical
``noeta.presets`` names) and adds the code-facing status fields.

Deliberately **light**: it imports only the narrow ``EventLogReader`` /
``EventLogTaskIndex`` / ``ContentStore`` Protocols, ``fold``,
``official_specs``, and the typed wake-handle constants. Enumeration goes
through ``EventLogTaskIndex`` and the genesis through the narrow reader, so
listing sessions never drags in the Engine host seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from noeta.execution.multi_turn import NEXT_GOAL_WAKE_HANDLE
from tests._read_models._common import _APPROVAL_HANDLE_PREFIX
from tests._read_models.detail import task_created_header
from noeta.presets import official_specs
from noeta.core.fold import fold
from noeta.protocols.content_store import ContentStore
from noeta.protocols.event_log import EventLogReader, EventLogTaskIndex
from noeta.protocols.wake import HumanResponseReceived
from noeta.builtins.ask_user_question.impl import QUESTION_HANDLE_PREFIX


__all__ = [
    "CodeSessionRow",
    "_status_text",
    "list_code_sessions",
    "filter_code_sessions",
]


#: Recording aliases: an ``agent_name`` a stream may carry → its canonical name.
_ALIASES: dict[str, str] = {"default": "main"}

#: Snapshot of the canonical agent-name set, taken once per import.
_CANONICAL_NAMES: frozenset[str] = frozenset(official_specs())


def _is_code_agent_name(name: str) -> bool:
    return _ALIASES.get(name, name) in _CANONICAL_NAMES


@dataclass(frozen=True, slots=True)
class CodeSessionRow:
    """One catalog row. ``model`` stays ``None`` until a ``ModelBound`` is
    folded; ``status_text`` is the code-facing label (see :func:`_status_text`)."""

    task_id: str
    agent: str
    goal: str
    model: Optional[str]
    status: str
    closed: bool
    status_text: str
    last_seq: int
    last_event_time: float


def _status_text(status: str, closed: bool, wake_on: Any) -> str:
    """Code-facing status with fixed precedence: ``closed`` > ``terminal`` >
    ``awaiting approval`` > ``resumable`` > the underlying ``status``.

    The approval / next-goal cases are decided from the **typed** ``wake_on``
    (a :class:`HumanResponseReceived`), never by string-matching a rendered
    value."""
    if closed:
        return "closed"
    if status == "terminal":
        return "terminal"
    if isinstance(wake_on, HumanResponseReceived):
        if wake_on.handle.startswith(_APPROVAL_HANDLE_PREFIX):
            return "awaiting approval"
        if wake_on.handle.startswith(QUESTION_HANDLE_PREFIX):
            return "awaiting answer"
        if wake_on.handle == NEXT_GOAL_WAKE_HANDLE:
            return "resumable"
    return status


def list_code_sessions(
    task_index: EventLogTaskIndex,
    event_log: EventLogReader,
    content_store: ContentStore,
) -> list[CodeSessionRow]:
    """Code-session rows, most-recent-update first (pure read).

    Enumerates via the ``EventLogTaskIndex`` catalog, keeps only tasks whose
    genesis ``agent_name`` is a registered coding Agent, and folds each for
    status / closed / model / ``wake_on``. Malformed streams (no ``TaskCreated``)
    and non-code tasks are skipped. ``task_index`` and ``event_log`` are
    typically the same concrete log.
    """
    rows: list[CodeSessionRow] = []
    for summary in task_index.list_task_streams():
        header = task_created_header(event_log, summary.task_id)
        if header is None or not _is_code_agent_name(header.agent_name):
            continue  # malformed (no TaskCreated) or not a code session
        folded = fold(event_log, content_store, summary.task_id)
        gov = folded.governance
        rows.append(
            CodeSessionRow(
                task_id=summary.task_id,
                agent=header.agent_name,
                goal=header.goal,
                model=gov.model_binding,
                status=folded.status,
                closed=gov.closed,
                status_text=_status_text(
                    folded.status, gov.closed, folded.wake_on
                ),
                last_seq=summary.last_seq,
                last_event_time=summary.last_event_time,
            )
        )
    return rows


def filter_code_sessions(
    rows: list[CodeSessionRow],
    *,
    status: Optional[str] = None,
    agent: Optional[str] = None,
    closed: Optional[bool] = None,
    grep: Optional[str] = None,
    sort: str = "updated",
    limit: Optional[int] = None,
) -> list[CodeSessionRow]:
    """Filter / sort / truncate code-session rows (pure; no I/O).

    Filters AND together; order of operations is filter → sort → limit. All
    args default to "no filter", and ``sort="updated"`` **preserves the input
    order** — the recency-desc + task_id tiebreak ``list_code_sessions`` already
    produced — so a default call returns the rows unchanged.

    - ``status`` — case-insensitive substring against the displayed
      ``status_text`` (so ``"approval"`` matches ``"awaiting approval"``).
    - ``agent`` — case-insensitive exact match on ``agent``.
    - ``closed`` — folded ``governance.closed`` bool (``True``/``False``);
      ``None`` keeps both.
    - ``grep`` — case-insensitive substring on ``goal`` only (no JSON/event scan).
    - ``sort`` — ``"updated"`` (input order, no re-sort), ``"agent"`` (agent asc,
      then most-recent first, then task_id), or ``"task"`` (task_id asc).
    - ``limit`` — keep the first N after filter+sort (caller validates N > 0).
    """
    result = list(rows)
    if status is not None:
        needle = status.casefold()
        result = [r for r in result if needle in r.status_text.casefold()]
    if agent is not None:
        want = agent.casefold()
        result = [r for r in result if r.agent.casefold() == want]
    if closed is not None:
        result = [r for r in result if r.closed == closed]
    if grep is not None:
        needle = grep.casefold()
        result = [r for r in result if needle in r.goal.casefold()]
    if sort == "agent":
        result.sort(
            key=lambda r: (r.agent.casefold(), -r.last_event_time, r.task_id)
        )
    elif sort == "task":
        result.sort(key=lambda r: r.task_id)
    elif sort != "updated":
        raise ValueError(f"unknown sort key: {sort!r}")
    if limit is not None:
        result = result[:limit]
    return result
