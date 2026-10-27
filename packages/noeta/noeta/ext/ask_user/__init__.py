"""``AskUserQuestion``: the model asks the user structured questions.

A valid call parks the task (``Park(on="question")``) with no result yet;
the host answers with ``client.answer(task_id, call_id, data=...)`` and the
call's result becomes ``{"question_id", "answers": {id: {selected, other}}}``.

**Invalid answers.** An answer reaches the task through its inbox, already
taken by the time the tool sees it, and ``client.answer`` deduplicates by
request id — so a question gets exactly one answer, and a parked call cannot
park again. The rule is therefore: validate before posting
(:func:`answer_question`, or :func:`validate_answer` for hosts that post
themselves — the 0.6 behavior of rejecting before anything is written), and
an invalid answer that is posted anyway ends the question with an error
result naming what was wrong, so the model can ask again. A stale answer
(for a call that is not waiting) is never read. ``decision="deny"`` with no
data dismisses the question like a stop does.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from typing import TYPE_CHECKING, Any

from noeta.events import Answer
from noeta.ext.base import Deny, Extension, Gate, TaskView
from noeta.tools.base import Park, Tool, ToolContext, ToolOutput, text_output
from noeta.types import ToolUse

if TYPE_CHECKING:
    from collections.abc import Sequence

    from noeta.client import Client

__all__ = [
    "ALONE_ERROR",
    "ASK_USER_QUESTION_TOOL",
    "WITHDRAWN_ERROR",
    "AnswerValidationError",
    "AskUser",
    "AskUserQuestionTool",
    "answer_question",
    "normalize_answer",
    "validate_answer",
    "validate_questions",
]

ASK_USER_QUESTION_TOOL = "AskUserQuestion"

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_QUESTIONS = 4
_MIN_OPTIONS = 2
_MAX_OPTIONS = 4
_MAX_QUESTION_TEXT_LEN = 500
_MAX_HEADER_LEN = 12
_MAX_OPTION_LABEL_LEN = 80
_MAX_OPTION_DESCRIPTION_LEN = 300
_MAX_ANSWER_TEXT_LEN = 4000

#: Stamped on every call of a reply that mixes AskUserQuestion with others.
ALONE_ERROR = (
    "Nothing in this response ran: AskUserQuestion must be called on its own. "
    "Re-issue the other calls in a separate response."
)
WITHDRAWN_ERROR = "Question withdrawn by user (no answer provided)."
#: Leads the result of a call whose posted answer did not fit the questions.
INVALID_ANSWER_ERROR = "The user's answer could not be used: {error}"

DESCRIPTION = (resources.files("noeta.ext.ask_user") / "descriptions"
               / "AskUserQuestion.md").read_text("utf-8").strip()

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": _MAX_QUESTIONS,
            "items": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The complete question, ending with a question mark.",
                    },
                    "header": {
                        "type": "string",
                        "description": "Very short chip label (max 12 chars), e.g. 'Auth method'.",
                    },
                    "options": {
                        "type": "array",
                        "minItems": _MIN_OPTIONS,
                        "maxItems": _MAX_OPTIONS,
                        "description": (
                            "Distinct choices. An 'Other' free-text option is always added "
                            "automatically."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["label", "description"],
                        },
                    },
                    "multiSelect": {
                        "type": "boolean",
                        "description": "Allow selecting several options.",
                    },
                },
                "required": ["question", "header", "options", "multiSelect"],
            },
        },
    },
    "required": ["questions"],
}


class AnswerValidationError(ValueError):
    """An answer does not fit the pending questions. ``code`` is
    ``"invalid_answer"`` (0.6's coded error)."""

    code = "invalid_answer"


# ---------------------------------------------------------------- validation


def validate_call_id(call_id: Any) -> str | None:
    """The error text for an unusable call id, else ``None``."""
    if not isinstance(call_id, str) or not call_id:
        return "AskUserQuestion call_id must be a non-empty string"
    if not _ID_RE.fullmatch(call_id):
        return "AskUserQuestion call_id must match ^[A-Za-z0-9_-]{1,64}$"
    return None


def _options(raw: Any) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(raw, list):
        return [], "options must be a list"
    if not _MIN_OPTIONS <= len(raw) <= _MAX_OPTIONS:
        return [], f"options must contain {_MIN_OPTIONS}-{_MAX_OPTIONS} items"
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            return [], "each option must be an object"
        label = item.get("label")
        if not isinstance(label, str) or not label:
            return [], "each option needs a non-empty string label"
        if len(label) > _MAX_OPTION_LABEL_LEN:
            return [], f"option label too long (max {_MAX_OPTION_LABEL_LEN})"
        if label in seen:
            return [], f"duplicate option label: {label!r}"
        seen.add(label)
        description = item.get("description")
        if not isinstance(description, str) or not description:
            return [], "each option needs a non-empty string description"
        if len(description) > _MAX_OPTION_DESCRIPTION_LEN:
            return [], f"option description too long (max {_MAX_OPTION_DESCRIPTION_LEN})"
        out.append({"label": label, "description": description})
    return out, None


def validate_questions(arguments: Any) -> list[dict[str, Any]] | str:
    """The normalised questions (each with its index as ``id``), or the
    error text the model sees."""
    if not isinstance(arguments, dict):
        return "AskUserQuestion arguments must be an object"
    raw = arguments.get("questions")
    if not isinstance(raw, list):
        return "questions must be a list"
    if not 1 <= len(raw) <= _MAX_QUESTIONS:
        return f"questions must contain 1-{_MAX_QUESTIONS} items"
    questions: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return "each question must be an object"
        question = item.get("question")
        if not isinstance(question, str) or not question:
            return "each question needs a non-empty question string"
        if len(question) > _MAX_QUESTION_TEXT_LEN:
            return f"question too long (max {_MAX_QUESTION_TEXT_LEN})"
        header = item.get("header")
        if not isinstance(header, str) or not header:
            return "each question needs a non-empty header"
        if len(header) > _MAX_HEADER_LEN:
            return f"header too long (max {_MAX_HEADER_LEN} chars)"
        multi = item.get("multiSelect")
        if not isinstance(multi, bool):
            return "each question needs a boolean multiSelect"
        options, error = _options(item.get("options"))
        if error is not None:
            return error
        questions.append({"id": str(index), "question": question, "header": header,
                          "options": options, "multiSelect": multi})
    return questions


def normalize_answer(raw: Any, questions: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Validate an answer — ``{"answers": {...}}`` or the bare map, keyed by
    question id (``"0"``, ``"1"``, ...), each ``{"selected": [labels],
    "other": text}`` — and return it normalised. Raises
    :class:`AnswerValidationError`."""
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
            if len(raw_other) > _MAX_ANSWER_TEXT_LEN:
                raise AnswerValidationError(
                    f"answer {qid!r} other text too long (max {_MAX_ANSWER_TEXT_LEN})")
            other = raw_other
        out[qid] = {"selected": selected, "other": other}
    return out


def validate_answer(call_input: dict[str, Any], data: Any) -> dict[str, dict[str, Any]]:
    """Validate ``data`` against an AskUserQuestion call's input (as
    ``Pending.input`` carries it). Raises :class:`AnswerValidationError`."""
    questions = validate_questions(call_input)
    if isinstance(questions, str):
        raise AnswerValidationError(questions)
    return normalize_answer(data, questions)


async def answer_question(client: Client, task_id: str, request_id: str, data: Any) -> None:
    """Answer a pending question after validating it — nothing is posted
    for a stale or invalid answer (raises :class:`AnswerValidationError`)."""
    from noeta.ext.permissions.pending import pending

    for item in pending(await client.events(task_id), task_id):
        if item.kind == "question" and item.request_id == request_id:
            validate_answer(item.input, data)
            await client.answer(task_id, request_id, data=data)
            return
    raise AnswerValidationError(f"no pending user question for question_id {request_id!r}")


# ---------------------------------------------------------------- tool


def _result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


class AskUserQuestionTool:
    name = ASK_USER_QUESTION_TOOL
    description = DESCRIPTION
    input_schema = INPUT_SCHEMA
    read_only = False
    #: An answer cannot be fetched again, so it is never cleared.
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        error = validate_call_id(ctx.call_id)
        if error is not None:
            return text_output(error, is_error=True)
        questions = validate_questions(input)
        if isinstance(questions, str):
            return text_output(questions, is_error=True)
        return ToolOutput(park=Park(on="question", detail={"questions": questions}))

    async def resume(self, input: dict[str, Any], ctx: ToolContext, outcome: Any) -> ToolOutput:
        qid = ctx.call_id
        if outcome is None or (isinstance(outcome, Answer) and outcome.decision == "deny"
                               and outcome.data is None):
            body = _result({"question_id": qid, "withdrawn": True})
            return text_output(f"{WITHDRAWN_ERROR}\n{body}", is_error=True,
                               question_id=qid, withdrawn=True)
        data = outcome.data if isinstance(outcome, Answer) else outcome
        try:
            answers = validate_answer(input, data)
        except AnswerValidationError as exc:
            return text_output(INVALID_ANSWER_ERROR.format(error=exc), is_error=True,
                               question_id=qid, invalid_answer=str(exc))
        return text_output(_result({"question_id": qid, "answers": answers}),
                           question_id=qid, answers=answers)


class AskUser(Extension):
    """Offers ``AskUserQuestion`` and refuses a reply that mixes it with
    other calls (every call of that reply gets :data:`ALONE_ERROR`). No
    constructor arguments."""

    name = "ask_user"

    def __init__(self) -> None:
        self._tools: list[Tool] = [AskUserQuestionTool()]

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return self._tools

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        uses = [c.use for c in view.state.calls]
        if len(uses) > 1 and any(u.name == ASK_USER_QUESTION_TOOL for u in uses):
            return Deny(reason=ALONE_ERROR)
        return None
