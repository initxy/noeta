"""Context-provenance projection for a code session (pure read).

Projects the recorded ``ContextPlanComposed`` plans and the
``LLMRequestStarted`` message-selection counts. Body-free by construction: a
plan is decoded only far enough to summarise it, and every reference is reported
as ``{hash, bytes, media_type}``, so inspecting what the model saw never
re-materialises prompt or resource bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from tests._read_models.detail import task_created_header
from noeta.presets import official_specs
from noeta.protocols.canonical import from_canonical_bytes
from noeta.protocols.content_store import ContentStore
from noeta.protocols.context_plan import ContextPlan
from noeta.protocols.event_log import EventLogReader


__all__ = [
    "ContextPlanView",
    "SelectionView",
    "_ref_summary",
    "_resource_summary",
    "_context_plan_view",
    "_selection_view",
    "build_code_context_view",
    "build_code_session_context",
]


#: Recording aliases: an ``agent_name`` a stream may carry → its canonical name.
_ALIASES: dict[str, str] = {"default": "main"}

#: Snapshot of the canonical agent-name set, taken once per import.
_CANONICAL_NAMES: frozenset[str] = frozenset(official_specs())


def _is_code_agent_name(name: str) -> bool:
    return _ALIASES.get(name, name) in _CANONICAL_NAMES


@dataclass(frozen=True, slots=True)
class ContextPlanView:
    """One recorded ``ContextPlanComposed`` projected for display (no body bytes).

    ``decode_error`` is ``None`` on a sound plan; when the ``plan_ref`` is
    missing, unreadable, or decodes to something other than a
    :class:`~noeta.protocols.context_plan.ContextPlan` it carries a short reason
    and the other fields stay empty, so a corrupt recording is **flagged**,
    never silently shown as a valid empty plan."""

    seq: int
    occurred_at: float
    plan_ref: dict[str, Any]
    composer_version: str
    segment_hashes: dict[str, str]
    selected_skills: tuple[str, ...]
    retrieved_resources: tuple[dict[str, Any], ...]
    selected_messages: tuple[dict[str, Any], ...]
    dropped_messages: tuple[dict[str, Any], ...]
    decode_error: Optional[str]


@dataclass(frozen=True, slots=True)
class SelectionView:
    """One ``LLMRequestStarted``: the request-ref summary — the hash of the
    bytes the model actually saw, never the body — plus the recorded
    ``MessageSelection`` counts when one was attached.

    A view is built for EVERY ``LLMRequestStarted``, including the common case
    where no count-based truncation happened and ``selection`` is ``None``: the
    per-turn ``request_ref`` anchor is the whole point of the trace ("what did
    the model see this turn?") and must not vanish with the counts. Absent a
    ``MessageSelection`` the counts degrade to the no-truncation values
    (``strategy=""``, the rest ``0``)."""

    seq: int
    call_id: str
    model: str
    request_ref: dict[str, Any]
    input_tokens: int
    strategy: str
    candidates: int
    selected: int
    dropped: int
    limit: int


def _ref_summary(ref: Any) -> dict[str, Any]:
    """ContentRef → ``{hash, bytes, media_type}`` (never the body)."""
    return {
        "hash": getattr(ref, "hash", None),
        "bytes": getattr(ref, "size", None),
        "media_type": getattr(ref, "media_type", None),
    }


def _resource_summary(resource: Any) -> dict[str, Any]:
    """A ``ContextPlan.retrieved_resources`` entry → summary (no body bytes)."""
    if not isinstance(resource, dict):
        return {"reason": None, "hash": None, "bytes": None, "media_type": None}
    cref = resource.get("content_ref")
    return {
        "reason": resource.get("reason"),
        "hash": getattr(cref, "hash", None),
        "bytes": resource.get("bytes", getattr(cref, "size", None)),
        "media_type": resource.get(
            "media_type", getattr(cref, "media_type", None)
        ),
    }


def _context_plan_view(env: Any, content_store: ContentStore) -> ContextPlanView:
    seq = int(getattr(env, "seq", 0))
    occurred = float(getattr(env, "occurred_at", 0.0))

    def _err(reason: str) -> ContextPlanView:
        ref = getattr(env.payload, "plan_ref", None)
        return ContextPlanView(
            seq=seq, occurred_at=occurred, plan_ref=_ref_summary(ref),
            composer_version="", segment_hashes={}, selected_skills=(),
            retrieved_resources=(), selected_messages=(), dropped_messages=(),
            decode_error=reason,
        )

    ref = getattr(env.payload, "plan_ref", None)
    if ref is None:
        return _err("missing plan_ref")
    try:
        plan = from_canonical_bytes(content_store.get(ref))
    except Exception as exc:  # noqa: BLE001 — read-only view must not crash
        return _err(f"unreadable plan_ref ({type(exc).__name__})")
    # Canonical-tag type check: a body that decodes to anything other than a
    # ContextPlan is a decode_error, never duck-typed into one.
    if not isinstance(plan, ContextPlan):
        return _err(f"decoded {type(plan).__name__}, expected ContextPlan")
    return ContextPlanView(
        seq=seq,
        occurred_at=occurred,
        plan_ref=_ref_summary(ref),
        composer_version=plan.composer_version,
        segment_hashes=dict(plan.segment_hashes),
        selected_skills=tuple(plan.selected_skills),
        retrieved_resources=tuple(
            _resource_summary(r) for r in plan.retrieved_resources
        ),
        selected_messages=tuple(_ref_summary(r) for r in plan.selected_messages),
        dropped_messages=tuple(_ref_summary(r) for r in plan.dropped_messages),
        decode_error=None,
    )


def _selection_view(env: Any, selection: Any) -> SelectionView:
    payload = env.payload
    return SelectionView(
        seq=int(getattr(env, "seq", 0)),
        call_id=str(getattr(payload, "call_id", "")),
        model=str(getattr(payload, "model", "")),
        request_ref=_ref_summary(getattr(payload, "request_ref", None)),
        input_tokens=int(getattr(payload, "input_tokens", 0) or 0),
        strategy=str(getattr(selection, "strategy", "")),
        candidates=int(getattr(selection, "candidates", 0)),
        selected=int(getattr(selection, "selected", 0)),
        dropped=int(getattr(selection, "dropped", 0)),
        limit=int(getattr(selection, "limit", 0)),
    )


def build_code_context_view(
    event_log: EventLogReader,
    content_store: ContentStore,
    task_id: str,
    *,
    all_steps: bool = False,
) -> Optional[tuple[str, list[ContextPlanView], list[SelectionView]]]:
    """Project a session's recorded context provenance (pure read).

    Returns ``(agent, plans, selections)`` for a code session — ``plans`` from
    ``ContextPlanComposed`` (each ``plan_ref`` decoded; corrupt →
    ``decode_error``) and one ``selections`` entry per ``LLMRequestStarted``.
    ``all_steps=False`` keeps only the latest of each. ``None`` for a non-code
    or malformed-genesis stream; a valid code session with no
    ``ContextPlanComposed`` yields empty lists, which the caller reads as "no
    context recorded". Reads bytes only to decode the plan body; message and
    resource bodies are never emitted, only ref summaries."""
    events = event_log.read(task_id)
    if not events:
        return None
    header = task_created_header(event_log, task_id)
    if header is None or not _is_code_agent_name(header.agent_name):
        return None

    plans: list[ContextPlanView] = []
    selections: list[SelectionView] = []
    for env in events:
        if env.type == "ContextPlanComposed":
            plans.append(_context_plan_view(env, content_store))
        elif env.type == "LLMRequestStarted":
            # A SelectionView for EVERY LLM round-trip, so the per-turn
            # request_ref anchor survives even without a recorded
            # ``MessageSelection`` — ``_selection_view`` tolerates a None
            # selection and degrades its counts to the no-truncation values.
            selection = getattr(env.payload, "selection", None)
            selections.append(_selection_view(env, selection))

    if not all_steps:
        plans = plans[-1:]
        selections = selections[-1:]
    return header.agent_name, plans, selections


def _plan_view_json(p: ContextPlanView) -> dict[str, Any]:
    return {
        "seq": p.seq,
        "occurred_at": p.occurred_at,
        "plan_ref": dict(p.plan_ref),
        "composer_version": p.composer_version,
        "segment_hashes": dict(p.segment_hashes),
        "selected_skills": list(p.selected_skills),
        "retrieved_resources": [dict(r) for r in p.retrieved_resources],
        "selected_messages": [dict(m) for m in p.selected_messages],
        "dropped_messages": [dict(m) for m in p.dropped_messages],
        "decode_error": p.decode_error,
    }


def _selection_view_json(s: SelectionView) -> dict[str, Any]:
    return {
        "seq": s.seq,
        "call_id": s.call_id,
        "model": s.model,
        "request_ref": dict(s.request_ref),
        "input_tokens": s.input_tokens,
        "strategy": s.strategy,
        "candidates": s.candidates,
        "selected": s.selected,
        "dropped": s.dropped,
        "limit": s.limit,
    }


def build_code_session_context(
    event_log: EventLogReader,
    content_store: ContentStore,
    task_id: str,
) -> Optional[dict[str, Any]]:
    """JSON projection of a code session's context provenance.

    Runs :func:`build_code_context_view` with ``all_steps=True`` — every
    recorded turn, so a consumer can track the whole conversation — and shapes
    the dataclasses into a plain mapping ``{"agent", "plans": [...],
    "selections": [...]}``. Body-free: every reference is a ``{hash, bytes,
    media_type}`` summary, so a consumer that wants bytes must dereference them
    deliberately. ``None`` for a non-code or malformed-genesis stream."""
    result = build_code_context_view(
        event_log, content_store, task_id, all_steps=True
    )
    if result is None:
        return None
    agent, plans, selections = result
    return {
        "agent": agent,
        "plans": [_plan_view_json(p) for p in plans],
        "selections": [_selection_view_json(s) for s in selections],
    }
