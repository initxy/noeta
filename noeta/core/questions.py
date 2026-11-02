"""Questions a tool puts to the person, in one shape every front end shows.

A tool asks by parking: ``return ToolOutput(park=ask({"question": ...}))``,
and its ``resume`` reads the answer with ``read_answer(outcome, <the same
ask(...)>)``. The detail is ``{"questions": [{"id", "question", "header"?,
"options"?, "multiSelect"?}], "partial"?}``; a question without options
asks for free text. The person's answer is ``{"answers": {id: {"selected":
[labels]} | {"other": text}}}``; with ``"partial": true`` it may leave
questions out (or empty), and only the answered ones are read. Front ends
need nothing from the asking plugin (``noeta.host.questions`` turns typed
lines into an answer and checks it before posting).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from noeta.core.events import Answer
from noeta.core.tool import Park

__all__ = [
    "AnswerValidationError",
    "ask",
    "detail_partial",
    "detail_questions",
    "normalize_answer",
    "read_answer",
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


def normalize_answer(raw: Any, questions: Sequence[Mapping[str, Any]], *,
                     partial: bool = False) -> dict[str, dict[str, Any]]:
    """Validate an answer — ``{"answers": {...}}`` or the bare map, keyed by
    question id, each ``{"selected": [labels], "other": text}`` — and return
    it normalised. With ``partial`` a question may be left out or answered
    with nothing (no label, blank text): it is unanswered and missing from
    the result; what is answered is checked all the same. Raises
    :class:`AnswerValidationError`."""
    if not isinstance(raw, dict):
        raise AnswerValidationError("answer body must be an object")
    answers_raw = raw.get("answers") if "answers" in raw else raw
    if not isinstance(answers_raw, dict):
        raise AnswerValidationError("answers must be an object")
    by_id = {str(q.get("id")): q for q in questions}
    missing = sorted(set(by_id) - set(answers_raw))
    if missing and not partial:
        raise AnswerValidationError(f"missing answer(s): {', '.join(missing)}")
    unknown = sorted(set(answers_raw) - set(by_id))
    if unknown:
        raise AnswerValidationError(f"unknown answer id(s): {', '.join(unknown)}")
    out: dict[str, dict[str, Any]] = {}
    for qid, question in by_id.items():
        if qid not in answers_raw:
            continue
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
            if partial:
                continue
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


def detail_partial(detail: Mapping[str, Any]) -> bool:
    """Whether a question's ``detail`` lets the answer leave questions out."""
    return detail.get("partial") is True


def detail_questions(detail: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The questions of a question's ``detail``, each with its ``id``."""
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
    detail = asked.detail if isinstance(asked, Park) else asked
    questions = detail_questions(detail)
    if isinstance(data, str) and len(questions) == 1:
        data = {questions[0]["id"]: {"other": data}}
    return normalize_answer(data, questions, partial=detail_partial(detail))
