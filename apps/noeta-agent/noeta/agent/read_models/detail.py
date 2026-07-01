"""read_models.detail — `noeta code inspect <task-id>` detail projection (pure read).

Folds one code session into a :class:`CodeSessionDetail` for ``noeta code
inspect``. Also home to the shared genesis reader ``task_created_header`` (used
by the catalog and context-view read models too).

No longer imports
``noeta.agent.roster.agents.AGENTS``; uses :mod:`noeta.presets` + legacy aliases to
decide whether a stream is a code session.

Read-only and deliberately **light** (CW5b watchpoint): imports only the narrow
``EventLogReader`` / ``ContentStore`` Protocols, ``fold``, ``official_specs``,
and the typed wake-handle constants — NEVER ``noeta.agent.execution.resolver`` /
``Engine`` / provider / storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from noeta.execution.multi_turn import NEXT_GOAL_WAKE_HANDLE
from noeta.agent.read_models._common import _APPROVAL_HANDLE_PREFIX
from noeta.presets import official_specs
from noeta.core.fold import fold
from noeta.protocols.content_store import ContentStore
from noeta.protocols.event_log import EventLogReader
from noeta.protocols.wake import (
    HumanResponseReceived,
    SubtaskCompleted,
    SubtaskGroupCompleted,
    TimerFired,
)
from noeta.policies.control_tools import (
    load_answers_body,
    load_questions_body,
    question_id_from_handle,
)


__all__ = [
    "_Header",
    "task_created_header",
    "CodeSessionDetail",
    "_plainify",
    "_extract_changed_path",
    "_wake_kind_and_handle",
    "build_code_session_detail",
]


#: D1: legacy recording aliases.
_ALIASES: dict[str, str] = {"default": "main"}

#: Module-level snapshot: the canonical agent-name set (for code-session checks).
_CANONICAL_NAMES: frozenset[str] = frozenset(official_specs())


def _is_code_agent_name(name: str) -> bool:
    """True if ``agent_name`` is a known code agent (legacy aliases included)."""
    return _ALIASES.get(name, name) in _CANONICAL_NAMES

#: Default count for the ``recent_*`` slices of a session detail (CW6 OQ4 —
#: fixed, no ``--limit`` flag yet).
_DEFAULT_RECENT = 5


@dataclass(frozen=True, slots=True)
class _Header:
    agent_name: str
    goal: str


def task_created_header(
    event_log: EventLogReader, task_id: str
) -> Optional[_Header]:
    """Read the genesis ``TaskCreated`` (``agent_name`` + ``goal``) via the
    narrow reader. ``None`` for a stream with no ``TaskCreated`` (malformed →
    the caller skips it). Depends only on ``EventLogReader`` + the event payload
    — NOT ``noeta.agent.execution.resolver`` (CW5b P1.1)."""
    for env in event_log.read(task_id):
        if env.type == "TaskCreated":
            payload = env.payload
            return _Header(
                agent_name=str(getattr(payload, "agent_name", "")),
                goal=str(getattr(payload, "goal", "")),
            )
    return None


# ---------------------------------------------------------------------------
# CW6 — `noeta code inspect <task-id>` detail projection (code-specific, pure read)
# ---------------------------------------------------------------------------


def _wake_kind_and_handle(
    wake_on: Any,
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Classify folded ``wake_on`` into kind/handle/action ids.

    Returns ``(wake_kind, wake_handle, approval_call_id, question_id)`` from the
    **typed** condition; action ids are set only for their matching wake kind.
    """
    if isinstance(wake_on, HumanResponseReceived):
        handle = wake_on.handle
        if handle.startswith(_APPROVAL_HANDLE_PREFIX):
            return "approval", handle, handle[len(_APPROVAL_HANDLE_PREFIX) :], None
        qid = question_id_from_handle(handle)
        if qid is not None:
            return "question", handle, None, qid
        if handle == NEXT_GOAL_WAKE_HANDLE:
            return "next-goal", handle, None, None
        return "human", handle, None, None
    if isinstance(wake_on, (SubtaskCompleted, SubtaskGroupCompleted)):
        return "subtask", None, None, None
    if isinstance(wake_on, TimerFired):
        return "timer", None, None, None
    return None, None, None, None


@dataclass(frozen=True, slots=True)
class CodeSessionDetail:
    """Read-only summary of one code session for ``noeta code inspect``.

    Folded from the EventLog; every field is derived (no writes). The
    approval fields (``wake_kind`` / ``wake_handle`` / ``approval_call_id``)
    exist so the observation→action path is visible: a session ``awaiting
    approval`` exposes the exact ``call_id`` a future ``noeta code approve``
    (CW7) consumes."""

    task_id: str
    agent: str
    goal: str
    status: str
    status_text: str
    closed: bool
    closed_by: Optional[str]
    close_reason: Optional[str]
    model: Optional[str]
    wake_kind: Optional[str]
    wake_handle: Optional[str]
    approval_call_id: Optional[str]
    question_id: Optional[str]
    active_skills: tuple[str, ...]
    iterations: int
    tool_calls: int
    cost_usd: float
    spawned_subtasks: int
    pending_approvals: tuple[dict[str, Any], ...]
    pending_questions: tuple[dict[str, Any], ...]
    recent_tool_calls: tuple[dict[str, Any], ...]
    recent_approvals: tuple[dict[str, Any], ...]
    recent_question_answers: tuple[dict[str, Any], ...]
    recent_denials: tuple[dict[str, Any], ...]
    files_changed: tuple[str, ...]
    # CW18a — plan/todo read-model surface (folded TaskState; pure read).
    # ``todos`` / ``decisions`` are JSON-plainified (see ``_plainify``) so a
    # downstream ``json.dumps`` can never crash on a non-native value.
    phase: Optional[str]
    next_action: Optional[str]
    todos: tuple[dict[str, Any], ...]
    decisions: tuple[dict[str, Any], ...]
    context_stats: dict[str, Any]
    last_seq: int
    last_event_time: float


def _plainify(value: Any) -> Any:
    """Recursively coerce a folded value to JSON-native types (CW18a W5).

    ``dict``/``list``/``tuple`` recurse; ``str``/``int``/``float``/``bool``/
    ``None`` pass through; anything else (a ContentRef, a dataclass, …) is
    ``str()``-ified — so emitting ``todos``/``decisions`` can never raise in
    ``json.dumps`` even if a policy stashed a non-native value."""
    if isinstance(value, dict):
        return {str(k): _plainify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plainify(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _extract_changed_path(side_effect: Any) -> Optional[str]:
    """Defensive path-like extraction from a ``ToolResultRecorded`` side-effect
    (CW6 OQ2 — we do NOT pin the fs side-effect schema). Returns the first of a
    few common keys, or ``None`` when nothing path-like is present."""
    if not isinstance(side_effect, dict):
        return None
    for key in ("path", "target", "file"):
        value = side_effect.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def build_code_session_detail(
    event_log: EventLogReader,
    content_store: ContentStore,
    task_id: str,
    *,
    recent: int = _DEFAULT_RECENT,
) -> Optional[CodeSessionDetail]:
    """Fold one code session into a :class:`CodeSessionDetail` (pure read).

    Returns ``None`` when the stream is empty, has no ``TaskCreated`` genesis,
    or its ``agent_name`` is not a registered coding Agent (the caller turns
    ``None`` into a clean "not a code session" error). Unlike ``noeta code
    resume``'s preflight, MCP / delegation sessions are **observable** — there
    is no refusal here, only a fold.

    Imports nothing beyond the narrow ``EventLogReader`` / ``ContentStore``
    Protocols + ``fold`` + ``AGENTS`` (CW5b watchpoint — no resolver / Engine /
    provider / storage)."""
    from noeta.agent.read_models.catalog import _status_text
    from noeta.agent.read_models.context_view import (
        _ref_summary,
        build_code_context_view,
    )

    events = event_log.read(task_id)
    if not events:
        return None
    header = task_created_header(event_log, task_id)
    if header is None or not _is_code_agent_name(header.agent_name):
        return None

    folded = fold(event_log, content_store, task_id)
    gov = folded.governance
    wake_kind, wake_handle, approval_call_id, question_id = _wake_kind_and_handle(
        folded.wake_on
    )

    # Single read-pass for the slices fold does not pre-aggregate: the recent
    # tool calls and the changed-file paths (only counters live in governance).
    tool_calls_seen: list[dict[str, Any]] = []
    files_changed: list[str] = []
    for env in events:
        if env.type == "ToolCallStarted":
            payload = env.payload
            tool_calls_seen.append(
                {
                    "call_id": str(getattr(payload, "call_id", "")),
                    "tool_name": str(getattr(payload, "tool_name", "")),
                }
            )
        elif env.type == "ToolResultRecorded":
            for side_effect in getattr(env.payload, "side_effects", None) or ():
                path = _extract_changed_path(side_effect)
                if path is not None and path not in files_changed:
                    files_changed.append(path)

    def _pending_question_item(
        question_id: str, value: dict[str, Any]
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "question_id": question_id,
            "call_id": str(value.get("call_id", "")),
            "question_count": int(value.get("question_count", 0) or 0),
            "reason": value.get("reason"),
        }
        ref = value.get("questions_ref")
        item["questions_ref"] = _ref_summary(ref)
        if ref is not None:
            try:
                item["questions"] = _plainify(load_questions_body(content_store, ref))
            except Exception as exc:  # noqa: BLE001 - fail-open projection
                item["questions"] = []
                item["decode_error"] = str(exc)
        else:
            item["questions"] = []
            item["decode_error"] = "missing questions_ref"
        return item

    def _answer_item(value: dict[str, Any]) -> dict[str, Any]:
        item = dict(value)
        ref = item.get("answers_ref")
        item["answers_ref"] = _ref_summary(ref)
        if ref is not None:
            try:
                item["answers"] = _plainify(load_answers_body(content_store, ref))
            except Exception as exc:  # noqa: BLE001 - fail-open projection
                item["answers"] = {}
                item["decode_error"] = str(exc)
        return item

    context_view = build_code_context_view(event_log, content_store, task_id)
    context_stats: dict[str, Any] = {}
    if context_view is not None:
        from noeta.agent.read_models.context_stats import compact_context_stats

        _, context_plans, context_selections = context_view
        context_stats = compact_context_stats(context_plans, context_selections)

    last = events[-1]
    return CodeSessionDetail(
        task_id=task_id,
        agent=header.agent_name,
        goal=header.goal,
        status=folded.status,
        status_text=_status_text(folded.status, gov.closed, folded.wake_on),
        closed=gov.closed,
        closed_by=gov.closed_by,
        close_reason=gov.close_reason,
        model=gov.model_binding,
        wake_kind=wake_kind,
        wake_handle=wake_handle,
        approval_call_id=approval_call_id,
        question_id=question_id,
        active_skills=tuple(folded.state.active_skills),
        iterations=gov.iterations,
        tool_calls=gov.tool_calls,
        cost_usd=gov.cost_usd,
        spawned_subtasks=gov.spawned_subtasks,
        pending_approvals=tuple(
            {"call_id": call_id, "tool_name": str(value.get("tool_name", ""))}
            for call_id, value in gov.pending_approvals.items()
        ),
        pending_questions=tuple(
            _pending_question_item(question_id, value)
            for question_id, value in gov.pending_questions.items()
        ),
        recent_tool_calls=tuple(tool_calls_seen[-recent:]),
        recent_approvals=tuple(dict(a) for a in gov.approvals[-recent:]),
        recent_question_answers=tuple(
            _answer_item(a) for a in gov.question_answers[-recent:]
        ),
        recent_denials=tuple(dict(d) for d in gov.denied[-recent:]),
        files_changed=tuple(files_changed[-recent:]),
        phase=folded.state.phase,
        next_action=folded.state.next_action,
        todos=tuple(_plainify(t) for t in folded.state.todos),
        decisions=tuple(_plainify(d) for d in folded.state.decisions),
        context_stats=context_stats,
        last_seq=int(getattr(last, "seq", 0)),
        last_event_time=float(getattr(last, "occurred_at", 0.0)),
    )
