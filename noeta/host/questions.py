"""Answering a question a task waits on from a front end: typed lines
become an answer (:func:`typed_answer`, :func:`next_question`,
:func:`typed_one`), :func:`clarify_message` is what a person who would
rather talk the questions over says instead (posted as a ``deny`` with
``{"message": ...}``), and :func:`answer_question` checks the answer against
what was asked before posting it (the question form is
``noeta.core.questions``)."""

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

__all__ = ["answer_question", "answer_text", "clarify_message", "next_question",
           "typed_answer", "typed_one"]


def next_question(detail: dict[str, Any],
                  answers: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The first question of a pending question's ``detail`` (``{"questions":
    [...]}``) that ``answers`` does not answer yet, or ``None``."""
    done = answers or {}
    return next((q for q in detail_questions(detail) if q["id"] not in done), None)


def typed_one(question: dict[str, Any], text: str) -> dict[str, Any]:
    """One typed line as the answer to ``question``: an option's label (case
    and surrounding space ignored) selects it, comma-separated labels select
    several where the question allows it, anything else is ``other`` text."""
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
            out[question["id"]] = typed_one(question, text)
            break
    return out


#: What the model reads when the person would rather talk the questions
#: over than answer them (Claude Code's "Chat about this", in our words);
#: the questions follow, each with what was answered so far.
CLARIFY = (
    "The user wants to talk these questions over before answering: they may have more "
    "information, context or questions of their own. Start by asking them what they would "
    "like to clarify, take their reply into account, and then ask the questions again, "
    "reworded, if they still apply.\n\nQuestions asked:\n{questions}")


def answer_text(answer: Any) -> str:
    """An answer (``{"selected", "other"}``) as one line: the chosen labels,
    then the typed text (``""``: nothing answered)."""
    if not isinstance(answer, dict):
        return ""
    chosen = [str(label) for label in answer.get("selected") or []]
    other = answer.get("other")
    if isinstance(other, str) and other.strip():
        chosen.append(other.strip())
    return ", ".join(chosen)


def clarify_message(detail: dict[str, Any], answers: dict[str, Any] | None = None) -> str:
    """What a person who chose to talk a pending question's ``detail`` over
    says: :data:`CLARIFY` with each question and its answer so far
    (``answers`` as posted: each ``{"selected", "other"}`` by question id)."""
    lines: list[str] = []
    for question in detail_questions(detail):
        said = answer_text((answers or {}).get(question["id"]))
        lines.append(f'- "{question.get("question", "")}"')
        lines.append(f"  Answer: {said}" if said else "  (No answer provided)")
    return CLARIFY.format(questions="\n".join(lines))


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
