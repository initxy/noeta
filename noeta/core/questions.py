"""Questions a tool puts to the person, in one shape every front end shows.

A tool asks by parking: ``return ToolOutput(park=ask({"question": ...}))``,
and its ``resume`` reads the answer with ``read_answer(outcome, <the same
ask(...)>)``. The detail is ``{"questions": [{"id", "question", "header"?,
"options"?, "multiSelect"?}]}``; a question without options asks for free
text. The person's answer is ``{"answers": {id: {"selected": [labels]} |
{"other": text}}}``. Front ends need nothing from the asking plugin:
:func:`next_question` and :func:`typed_answer` turn typed lines into an
answer, :func:`answer_question` checks it before posting.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from noeta.core.events import Answer
from noeta.core.tool import Park

if TYPE_CHECKING:
    from noeta.core.client import Client

__all__ = [
    "AnswerValidationError",
    "answer_question",
    "ask",
    "next_question",
    "normalize_answer",
    "read_answer",
    "typed_answer",
]

MAX_ANSWER_TEXT_LEN = 4000


class AnswerValidationError(ValueError):
    """An answer does not fit the pending questions. ``code`` is
    ``"invalid_answer"`` (0.6's coded error)."""

    code = "invalid_answer"


def ask(*questions: Mapping[str, Any]) -> Park:
    """Park the call on these questions. Each is ``{"question": text}`` with
    optional ``header``, ``options`` (``[{"label", "description"?}]``) and
    ``multiSelect``; ids are their positions. Raises ``ValueError`` on a
    malformed question (a bug in the asking plugin)."""
    if not questions:
        raise ValueError("ask() needs at least one question")
    out = []
    for index, q in enumerate(questions):
        text = q.get("question")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("each question needs a non-empty 'question' string")
        options = []
        for opt in q.get("options") or []:
            label = opt.get("label") if isinstance(opt, Mapping) else None
            if not isinstance(label, str) or not label:
                raise ValueError("each option needs a non-empty 'label'")
            options.append({"label": label, "description": str(opt.get("description") or "")})
        if len({o["label"] for o in options}) != len(options):
            raise ValueError("option labels must differ")
        item: dict[str, Any] = {"id": str(index), "question": text, "options": options,
                                "multiSelect": bool(q.get("multiSelect", False))}
        if q.get("header"):
            item["header"] = str(q["header"])
        out.append(item)
    return Park(on="question", detail={"questions": out})


def normalize_answer(raw: Any, questions: Sequence[Mapping[str, Any]]
                     ) -> dict[str, dict[str, Any]]:
    """Validate an answer — ``{"answers": {...}}`` or the bare map, keyed by
    question id, each ``{"selected": [labels], "other": text}`` — and return
    it normalised. Raises :class:`AnswerValidationError`."""
    if not isinstance(raw, dict):
        raise AnswerValidationError("answer body must be an object")
    answers_raw = raw.get("answers") if "answers" in raw else raw
    if not isinstance(answers_raw, dict):
        raise AnswerValidationError("answers must be an object")
    by_id = {str(q.get("id")): q for q in questions}
    missing = sorted(set(by_id) - set(answers_raw))
    if missing:
        raise AnswerValidationError(f"missing answer(s): {', '.join(missing)}")
    unknown = sorted(set(answers_raw) - set(by_id))
    if unknown:
        raise AnswerValidationError(f"unknown answer id(s): {', '.join(unknown)}")
    out: dict[str, dict[str, Any]] = {}
    for qid, question in by_id.items():
        answer = answers_raw[qid]
        if not isinstance(answer, dict):
            raise AnswerValidationError(f"answer {qid!r} must be an object")
        raw_selected = answer.get("selected")
        raw_other = answer.get("other")
        if raw_selected is None:
            selected: list[str] = []
        elif isinstance(raw_selected, list) and all(isinstance(x, str) for x in raw_selected):
            selected = list(raw_selected)
        else:
            raise AnswerValidationError(f"answer {qid!r} selected must be a list of strings")
        if raw_other is not None and not isinstance(raw_other, str):
            raise AnswerValidationError(f"answer {qid!r} other must be a string")
        has_other = isinstance(raw_other, str) and raw_other.strip() != ""
        if not selected and not has_other:
            raise AnswerValidationError(
                f"answer {qid!r} must contain selected labels or 'other' text")
        allowed = {str(o.get("label")) for o in question.get("options") or []
                   if isinstance(o, dict)}
        for label in selected:
            if label not in allowed:
                raise AnswerValidationError(
                    f"answer {qid!r} selected label {label!r} is not an option")
        if len(selected) > 1 and not question.get("multiSelect", False):
            raise AnswerValidationError(
                f"answer {qid!r} selected several labels but multiSelect is off")
        if len(set(selected)) != len(selected):
            raise AnswerValidationError(f"answer {qid!r} selected the same label twice")
        other: str | None = None
        if has_other and raw_other is not None:
            if len(raw_other) > MAX_ANSWER_TEXT_LEN:
                raise AnswerValidationError(
                    f"answer {qid!r} other text too long (max {MAX_ANSWER_TEXT_LEN})")
            other = raw_other
        out[qid] = {"selected": selected, "other": other}
    return out


def _detail_questions(detail: Mapping[str, Any]) -> list[dict[str, Any]]:
    qs = detail.get("questions") or []
    return [{**q, "id": str(q.get("id", i))} for i, q in enumerate(qs) if isinstance(q, dict)]


def read_answer(outcome: Any, asked: Park | Mapping[str, Any]
                ) -> dict[str, dict[str, Any]] | None:
    """What a parked question's ``resume`` got, checked against what was
    ``asked`` (the :func:`ask` result, or its detail): the normalised
    answers, or ``None`` when the question was withdrawn (the turn stopped,
    or dismissed with ``deny``). A plain string answers a single question as
    its text. Raises :class:`AnswerValidationError`."""
    if outcome is None or (isinstance(outcome, Answer) and outcome.decision == "deny"
                           and outcome.data is None):
        return None
    data = outcome.data if isinstance(outcome, Answer) else outcome
    questions = _detail_questions(asked.detail if isinstance(asked, Park) else asked)
    if isinstance(data, str) and len(questions) == 1:
        data = {questions[0]["id"]: {"other": data}}
    return normalize_answer(data, questions)


# ---------------------------------------------------------------- typed answers


def next_question(detail: dict[str, Any],
                  answers: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The first question of a pending question's ``detail`` (``{"questions":
    [...]}``) that ``answers`` does not answer yet, or ``None``."""
    done = answers or {}
    return next((q for q in _detail_questions(detail) if q["id"] not in done), None)


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
    ``{"answers": {...}}`` body or the bare map). Returns ``answers`` with
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
            return {**out, **(body if isinstance(body, dict) else parsed)}
    for question in _detail_questions(detail):
        if question["id"] not in out:
            out[question["id"]] = _typed_one(question, text)
            break
    return out


async def answer_question(client: Client, task_id: str, request_id: str, data: Any) -> None:
    """Answer a pending question after checking it against what was asked —
    nothing is posted for a stale or invalid answer (raises
    :class:`AnswerValidationError`)."""
    from noeta.core.pending import pending

    for item in pending(await client.events(task_id), task_id):
        if item.kind == "question" and item.request_id == request_id:
            questions = _detail_questions(item.detail)
            if isinstance(data, str) and len(questions) == 1:
                data = {questions[0]["id"]: {"other": data}}
            normalize_answer(data, questions)
            await client.answer(task_id, request_id, data=data)
            return
    raise AnswerValidationError(f"no pending user question for question_id {request_id!r}")
