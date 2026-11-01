"""Answering a question a task waits on from a front end: typed lines
become an answer (:func:`typed_answer`, :func:`next_question`), and
:func:`answer_question` checks the answer against what was asked before
posting it (the question form is ``noeta.core.questions``)."""

from __future__ import annotations

import json
from typing import Any

from noeta.core.client import ClientAPI
from noeta.core.questions import (
    AnswerValidationError,
    detail_partial,
    detail_questions,
    normalize_answer,
)
from noeta.host.pending import pending

__all__ = ["answer_question", "next_question", "typed_answer"]


def next_question(detail: dict[str, Any],
                  answers: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The first question of a pending question's ``detail`` (``{"questions":
    [...]}``) that ``answers`` does not answer yet, or ``None``."""
    done = answers or {}
    return next((q for q in detail_questions(detail) if q["id"] not in done), None)


def _typed_one(question: dict[str, Any], text: str) -> dict[str, Any]:
    labels = {str(o.get("label", "")).strip().lower(): str(o.get("label", ""))
              for o in question.get("options") or [] if isinstance(o, dict)}
    typed = text.strip()
    if typed.lower() in labels:
        return {"selected": [labels[typed.lower()]]}
    if question.get("multiSelect"):
        parts = [p.strip().lower() for p in typed.split(",") if p.strip()]
        if parts and all(p in labels for p in parts):
            return {"selected": list(dict.fromkeys(labels[p] for p in parts))}
    return {"other": text}


def typed_answer(detail: dict[str, Any], text: str,
                 answers: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Turn one line a person typed into answers for a pending question
    (``detail`` as ``Pending.detail`` or a ``Waiting`` entry carries it).

    The line answers the first unanswered question: an option's label
    (case and surrounding space ignored) selects it, comma-separated labels
    select several where the question allows it, anything else is the
    ``other`` text. A JSON object answers several at once (the
    ``{"answers": {...}}`` body or the bare map); where the question may
    be answered in part (``"partial"``), the object is the whole answer and
    the questions it leaves out stay unanswered. Returns ``answers`` with
    the new ones added; post ``{"answers": ...}`` once :func:`next_question`
    is ``None``."""
    out = dict(answers or {})
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            body = parsed.get("answers")
            out = {**out, **(body if isinstance(body, dict) else parsed)}
            if detail_partial(detail):
                out.update({q["id"]: {} for q in detail_questions(detail)
                            if q["id"] not in out})
            return out
    for question in detail_questions(detail):
        if question["id"] not in out:
            out[question["id"]] = _typed_one(question, text)
            break
    return out


async def answer_question(client: ClientAPI, task_id: str, request_id: str, data: Any) -> None:
    """Answer a pending question after checking it against what was asked —
    nothing is posted for a stale or invalid answer (raises
    :class:`AnswerValidationError`)."""
    for item in pending(await client.events(task_id), task_id):
        if item.kind == "question" and item.request_id == request_id:
            questions = detail_questions(item.detail)
            if isinstance(data, str) and len(questions) == 1:
                data = {questions[0]["id"]: {"other": data}}
            normalize_answer(data, questions, partial=detail_partial(item.detail))
            await client.answer(task_id, request_id, data=data)
            return
    raise AnswerValidationError(f"no pending user question for question_id {request_id!r}")
