"""``AskUserQuestion``: the model asks the user structured questions.

A valid call parks the task (``Park(on="question")``) with no result yet;
the host answers with ``client.answer(task_id, call_id, data=...)`` (each
question by id: ``{selected, other}``) and the call's result is prose naming
each question and its answer (Claude Code's shape); ``meta`` keeps the
structured answers.

**Invalid answers.** An answer reaches the task through its inbox, already
taken by the time the tool sees it, and ``client.answer`` deduplicates by
request id — so a question gets exactly one answer, and a parked call cannot
park again. The rule is therefore: validate before posting
(``noeta.host.questions.answer_question``, or :func:`validate_answer` for
hosts that post themselves — the 0.6 behavior of rejecting before anything is written), and
an invalid answer that is posted anyway ends the question with an error
result naming what was wrong, so the model can ask again. A stale answer
(for a call that is not waiting) is never read. ``decision="deny"`` with no
data dismisses the question like a stop does.
"""

from __future__ import annotations

import re
from importlib import resources
from typing import TYPE_CHECKING, Any

from noeta.core.events import Answer
from noeta.core.extension import Extension, TaskView
from noeta.core.questions import AnswerValidationError, normalize_answer
from noeta.core.tool import Park, Tool, ToolContext, ToolOutput, text_output
from noeta.toolkit.tools import SCHEMA_DIALECT

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "ASK_USER_QUESTION_TOOL",
    "WITHDRAWN_ERROR",
    "AnswerValidationError",
    "AskUser",
    "AskUserQuestionTool",
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
_MAX_PREVIEW_LEN = 20_000

WITHDRAWN_ERROR = "Question withdrawn by user (no answer provided)."
#: Leads the result of a call whose posted answer did not fit the questions.
INVALID_ANSWER_ERROR = "The user's answer could not be used: {error}"

DESCRIPTION = (resources.files("noeta.plugins.ask_user") / "descriptions"
               / "AskUserQuestion.md").read_text("utf-8").strip()

INPUT_SCHEMA: dict[str, Any] = {
    "$schema": SCHEMA_DIALECT,
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": _MAX_QUESTIONS,
            "description": "The questions to ask (1-4).",
            "items": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "The whole question, specific and ending with a question mark. "
                            "With multiSelect, word it so that several answers fit."),
                    },
                    "header": {
                        "type": "string",
                        "description": ("A very short label shown as a chip, at most 12 "
                                        "characters, such as \"Database\" or \"Scope\"."),
                    },
                    "options": {
                        "type": "array",
                        "minItems": _MIN_OPTIONS,
                        "maxItems": _MAX_OPTIONS,
                        "description": (
                            "2-4 distinct choices, mutually exclusive unless multiSelect is "
                            "set. Do not add an \"Other\" choice: one is always added."),
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": ("What the user sees and picks: 1-5 words "
                                                    "that name the choice."),
                                },
                                "description": {
                                    "type": "string",
                                    "description": ("What choosing this means: its effect and "
                                                    "trade-offs."),
                                },
                                "preview": {
                                    "type": "string",
                                    "description": (
                                        "Optional content shown while this option is "
                                        "focused: a UI mockup, a code snippet, a diagram or "
                                        "a config example to compare side by side."),
                                },
                            },
                            "required": ["label", "description"],
                            "additionalProperties": False,
                        },
                    },
                    "multiSelect": {
                        "type": "boolean",
                        "default": False,
                        "description": ("Let the user pick several options; set it when the "
                                        "choices do not exclude each other."),
                    },
                },
                "required": ["question", "header", "options", "multiSelect"],
                "additionalProperties": False,
            },
        },
        "answers": {
            "type": "object",
            "propertyNames": {"type": "string"},
            "additionalProperties": {"type": "string"},
            "description": "Filled in with the user's answers by the front end; leave it out.",
        },
        "annotations": {
            "type": "object",
            "propertyNames": {"type": "string"},
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "preview": {"type": "string",
                                "description": "The preview of the option the user chose."},
                    "notes": {"type": "string",
                              "description": "Notes the user added to this question."},
                },
                "additionalProperties": False,
            },
            "description": ("Per question text, what the user added: the chosen option's "
                            "preview and their notes. Filled in by the front end."),
        },
        "metadata": {
            "type": "object",
            "properties": {
                "source": {"type": "string",
                           "description": ("Where the question comes from, for statistics "
                                           "only (for example \"remember\"); never shown.")},
            },
            "additionalProperties": False,
            "description": "Statistics only; not shown to the user.",
        },
    },
    "required": ["questions"],
    "additionalProperties": False,
}


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
        option = {"label": label, "description": description}
        preview = item.get("preview")
        if preview is not None:
            if not isinstance(preview, str) or len(preview) > _MAX_PREVIEW_LEN:
                return [], f"option preview must be a string of at most {_MAX_PREVIEW_LEN} chars"
            option["preview"] = preview
        out.append(option)
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
        if any(q["question"] == question for q in questions):
            return f"two questions have the same text: {question!r}; each question must differ"
        if len(question) > _MAX_QUESTION_TEXT_LEN:
            return f"question too long (max {_MAX_QUESTION_TEXT_LEN})"
        header = item.get("header")
        if not isinstance(header, str) or not header:
            return "each question needs a non-empty header"
        if len(header) > _MAX_HEADER_LEN:
            return f"header too long (max {_MAX_HEADER_LEN} chars)"
        multi = item.get("multiSelect", False)
        if not isinstance(multi, bool):
            return "multiSelect must be a boolean"
        options, error = _options(item.get("options"))
        if error is not None:
            return error
        questions.append({"id": str(index), "question": question, "header": header,
                          "options": options, "multiSelect": multi})
    return questions


def validate_answer(call_input: dict[str, Any], data: Any) -> dict[str, dict[str, Any]]:
    """Validate ``data`` against an AskUserQuestion call's input (as
    ``Pending.input`` carries it). Raises :class:`AnswerValidationError`.

    A plain string answers a single-question call as its ``other`` text (a
    host that sends what the person typed still works)."""
    questions = validate_questions(call_input)
    if isinstance(questions, str):
        raise AnswerValidationError(questions)
    if isinstance(data, str) and len(questions) == 1:
        data = {questions[0]["id"]: {"other": data}}
    return normalize_answer(data, questions)


# ---------------------------------------------------------------- tool


def render_answers(questions: list[dict[str, Any]], answers: dict[str, dict[str, Any]]) -> str:
    """The result the model reads: each question with what the user chose
    (labels, then any typed text), a chosen option's preview after it."""
    parts: list[str] = []
    for q in questions:
        answer = answers.get(q["id"]) or {}
        chosen = list(answer.get("selected") or [])
        other = answer.get("other")
        if isinstance(other, str) and other.strip():
            chosen.append(other.strip())
        part = f'"{q["question"]}"="{", ".join(chosen)}"'
        previews = [o["preview"] for o in q["options"]
                    if o.get("preview") and o["label"] in (answer.get("selected") or [])]
        if previews:
            part += " selected preview:\n" + "\n".join(previews)
        parts.append(part)
    return ("The user answered your questions: " + ", ".join(parts)
            + ". Go on with these answers in mind.")


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
            return text_output(WITHDRAWN_ERROR, is_error=True, question_id=qid, withdrawn=True)
        data = outcome.data if isinstance(outcome, Answer) else outcome
        try:
            answers = validate_answer(input, data)
        except AnswerValidationError as exc:
            return text_output(INVALID_ANSWER_ERROR.format(error=exc), is_error=True,
                               question_id=qid, invalid_answer=str(exc))
        questions = validate_questions(input)
        assert not isinstance(questions, str)  # validate_answer passed
        return text_output(render_answers(questions, answers), question_id=qid,
                           answers=answers)


class AskUser(Extension):
    """Offers ``AskUserQuestion``. No constructor arguments."""

    name = "ask_user"

    def __init__(self) -> None:
        self._tools: list[Tool] = [AskUserQuestionTool()]

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return self._tools
