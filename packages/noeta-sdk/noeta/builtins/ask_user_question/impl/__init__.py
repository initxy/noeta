"""``ask_user_question`` — the structured-HITL control tool: schema, argument
validation, decision translation, and the answer-side codec.

A valid call suspends the task on a neutral :class:`YieldForHumanDecision`
carrying an opaque anchor, so the kernel never decodes this schema and the
whole question vocabulary stays here. The driver's ``answer`` path does have to
decode a submitted reply, yet the kernel never imports ``noeta.builtins`` —
hence the codec rides the mount's typed ``answer_codec`` field, collected by
the builder and threaded onto the Engine. Reached only through the plugin
loader's ``ref`` resolution; nothing imports it statically.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from noeta.execution.control_tool import (
    AskAnswerCodec,
    ControlToolBuildContext,
    ControlToolMount,
)
from noeta.policies.control_semantics import (
    ControlTranslateContext,
    ack_patch_decision,
)
from noeta.protocols.canonical import from_canonical_bytes, to_canonical_bytes
from noeta.protocols.content_store import ContentStore
from noeta.protocols.decisions import (
    Decision,
    HitlRequestAnchor,
    YieldForHumanDecision,
)
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    ThinkingBlock,
    ToolUseBlock,
)
from noeta.protocols.resources import load_markdown
from noeta.protocols.values import ContentRef


__all__ = [
    "ASK_USER_QUESTION_TOOL",
    "QUESTION_HANDLE_PREFIX",
    "QUESTION_BODY_MEDIA_TYPE",
    "is_question_id",
    "question_handle",
    "question_id_from_handle",
    "ask_user_question_tool_schema",
    "validate_call_id",
    "validate_question_arguments",
    "put_questions_body",
    "put_answers_body",
    "load_questions_body",
    "load_answers_body",
    "normalize_answer_document",
    "QuestionDecodeError",
    "AnswerValidationError",
    "translate_ask_user_question",
    "ASK_ANSWER_CODEC",
    "build_ask_user_question_control_tool",
]


ASK_USER_QUESTION_TOOL = "AskUserQuestion"
QUESTION_HANDLE_PREFIX = "question-"
QUESTION_BODY_MEDIA_TYPE = "application/json"

_HANDLE_SAFE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_QUESTIONS = 4
_MIN_OPTIONS = 2
_MAX_OPTIONS = 4
_MAX_QUESTION_TEXT_LEN = 500
_MAX_HEADER_LEN = 12
_MAX_OPTION_LABEL_LEN = 80
_MAX_OPTION_DESCRIPTION_LEN = 300
_MAX_ANSWER_TEXT_LEN = 4000


class QuestionDecodeError(ValueError):
    """A stored questions/answers body could not be decoded as expected."""


class AnswerValidationError(ValueError):
    """User answer JSON does not match the pending question body."""


def is_question_id(value: str) -> bool:
    return bool(_HANDLE_SAFE_RE.fullmatch(value))


def question_handle(question_id: str) -> str:
    return f"{QUESTION_HANDLE_PREFIX}{question_id}"


def question_id_from_handle(handle: str) -> Optional[str]:
    if not handle.startswith(QUESTION_HANDLE_PREFIX):
        return None
    qid = handle[len(QUESTION_HANDLE_PREFIX):]
    return qid if is_question_id(qid) else None


_ASK_USER_QUESTION_DESCRIPTION = load_markdown(__package__, "ask_user_question")


def ask_user_question_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": ASK_USER_QUESTION_TOOL,
            "description": _ASK_USER_QUESTION_DESCRIPTION,
            "parameters": {
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
                                    "description": (
                                        "The complete question, ending with "
                                        "a question mark."
                                    ),
                                },
                                "header": {
                                    "type": "string",
                                    "description": (
                                        "Very short chip label (max 12 "
                                        "chars), e.g. 'Auth method'."
                                    ),
                                },
                                "options": {
                                    "type": "array",
                                    "minItems": _MIN_OPTIONS,
                                    "maxItems": _MAX_OPTIONS,
                                    "description": (
                                        "Distinct choices. An 'Other' "
                                        "free-text option is always added "
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
                                    "description": (
                                        "Allow selecting several options."
                                    ),
                                },
                            },
                            "required": [
                                "question",
                                "header",
                                "options",
                                "multiSelect",
                            ],
                        },
                    },
                },
                "required": ["questions"],
            },
        },
    }


def validate_call_id(call_id: Any) -> tuple[bool, str]:
    if not isinstance(call_id, str) or not call_id:
        return False, "ask_user_question call_id must be a non-empty string"
    if not is_question_id(call_id):
        return (
            False,
            "ask_user_question call_id must match ^[A-Za-z0-9_-]{1,64}$",
        )
    return True, call_id


def validate_question_arguments(
    arguments: Any,
) -> tuple[bool, "tuple[list[dict[str, Any]], Optional[str]] | str"]:
    if not isinstance(arguments, dict):
        return False, "AskUserQuestion arguments must be an object"
    raw_questions = arguments.get("questions")
    if not isinstance(raw_questions, list):
        return False, "questions must be a list"
    if not 1 <= len(raw_questions) <= _MAX_QUESTIONS:
        return False, f"questions must contain 1-{_MAX_QUESTIONS} items"

    questions: list[dict[str, Any]] = []
    for index, item in enumerate(raw_questions):
        if not isinstance(item, dict):
            return False, "each question must be an object"
        question = item.get("question")
        if not isinstance(question, str) or not question:
            return False, "each question needs a non-empty question string"
        if len(question) > _MAX_QUESTION_TEXT_LEN:
            return False, f"question too long (max {_MAX_QUESTION_TEXT_LEN})"

        header = item.get("header")
        if not isinstance(header, str) or not header:
            return False, "each question needs a non-empty header"
        if len(header) > _MAX_HEADER_LEN:
            return False, f"header too long (max {_MAX_HEADER_LEN} chars)"

        multi_select = item.get("multiSelect")
        if not isinstance(multi_select, bool):
            return False, "each question needs a boolean multiSelect"

        options, error = _normalize_options(item.get("options"))
        if error is not None:
            return False, error
        questions.append(
            {
                # The index doubles as the stable key answers reference —
                # questions carry no model-supplied id in the reference shape.
                "id": str(index),
                "question": question,
                "header": header,
                "options": options,
                "multiSelect": multi_select,
            }
        )
    return True, (questions, None)


def _normalize_options(raw: Any) -> tuple[list[dict[str, Any]], Optional[str]]:
    if not isinstance(raw, list):
        return [], "options must be a list"
    if not _MIN_OPTIONS <= len(raw) <= _MAX_OPTIONS:
        return [], (
            f"options must contain {_MIN_OPTIONS}-{_MAX_OPTIONS} items"
        )
    seen: set[str] = set()
    options: list[dict[str, Any]] = []
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
            return [], (
                "option description too long "
                f"(max {_MAX_OPTION_DESCRIPTION_LEN})"
            )
        options.append({"label": label, "description": description})
    return options, None


def put_questions_body(
    content_store: ContentStore, questions: list[dict[str, Any]]
) -> ContentRef:
    return content_store.put(
        to_canonical_bytes({"questions": questions}),
        media_type=QUESTION_BODY_MEDIA_TYPE,
    )


def put_answers_body(
    content_store: ContentStore, answers: dict[str, dict[str, Any]]
) -> ContentRef:
    return content_store.put(
        to_canonical_bytes({"answers": answers}),
        media_type=QUESTION_BODY_MEDIA_TYPE,
    )


def load_questions_body(
    content_store: ContentStore, ref: ContentRef
) -> list[dict[str, Any]]:
    try:
        restored = from_canonical_bytes(content_store.get(ref))
    except Exception as exc:  # noqa: BLE001 - observation/preflight wraps this
        raise QuestionDecodeError(f"could not decode questions_ref: {exc}") from exc
    if not isinstance(restored, dict) or not isinstance(
        restored.get("questions"), list
    ):
        raise QuestionDecodeError("questions_ref body must be an object with questions")
    questions = restored["questions"]
    if not all(isinstance(q, dict) for q in questions):
        raise QuestionDecodeError("questions_ref questions must be objects")
    return [dict(q) for q in questions]


def load_answers_body(
    content_store: ContentStore, ref: ContentRef
) -> dict[str, dict[str, Any]]:
    try:
        restored = from_canonical_bytes(content_store.get(ref))
    except Exception as exc:  # noqa: BLE001 - observation wraps this
        raise QuestionDecodeError(f"could not decode answers_ref: {exc}") from exc
    if not isinstance(restored, dict) or not isinstance(restored.get("answers"), dict):
        raise QuestionDecodeError("answers_ref body must be an object with answers")
    answers: dict[str, dict[str, Any]] = {}
    for key, value in restored["answers"].items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise QuestionDecodeError("answers_ref answers must map strings to objects")
        answers[key] = dict(value)
    return answers


def normalize_answer_document(
    raw: Any,
    questions: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Validate and normalize a submitted answer body.

    ``raw`` may be either ``{"answers": {...}}`` or the direct answer map,
    keyed by the question's index key (``"0"``, ``"1"``, …). Each answer is
    ``{"selected": [labels...], "other": text}``: ``selected`` must be labels
    of the question's options (at most one unless ``multiSelect``), and
    ``other`` is the always-available free-text slot (the auto-appended
    "Other" option). At least one of the two must be present.
    """
    if not isinstance(raw, dict):
        raise AnswerValidationError("answer body must be an object")
    answers_raw = raw.get("answers") if "answers" in raw else raw
    if not isinstance(answers_raw, dict):
        raise AnswerValidationError("answers must be an object")
    by_id = {str(q.get("id")): q for q in questions}
    answer_ids = set(answers_raw)
    question_ids = set(by_id)
    missing = sorted(question_ids - answer_ids)
    if missing:
        raise AnswerValidationError(f"missing answer(s): {', '.join(missing)}")
    unknown = sorted(answer_ids - question_ids)
    if unknown:
        raise AnswerValidationError(f"unknown answer id(s): {', '.join(unknown)}")

    normalized: dict[str, dict[str, Any]] = {}
    for qid, question in by_id.items():
        raw_answer = answers_raw[qid]
        if not isinstance(raw_answer, dict):
            raise AnswerValidationError(f"answer {qid!r} must be an object")
        raw_selected = raw_answer.get("selected")
        raw_other = raw_answer.get("other")
        if raw_selected is None:
            selected: list[str] = []
        elif isinstance(raw_selected, list) and all(
            isinstance(x, str) for x in raw_selected
        ):
            selected = list(raw_selected)
        else:
            raise AnswerValidationError(
                f"answer {qid!r} selected must be a list of strings"
            )
        if raw_other is not None and not isinstance(raw_other, str):
            raise AnswerValidationError(f"answer {qid!r} other must be a string")
        has_other = isinstance(raw_other, str) and raw_other.strip() != ""
        if not selected and not has_other:
            raise AnswerValidationError(
                f"answer {qid!r} must contain selected labels or 'other' text"
            )
        allowed = {
            str(option.get("label"))
            for option in question.get("options") or []
            if isinstance(option, dict)
        }
        for label in selected:
            if label not in allowed:
                raise AnswerValidationError(
                    f"answer {qid!r} selected label {label!r} is not an option"
                )
        if len(selected) > 1 and not question.get("multiSelect", False):
            raise AnswerValidationError(
                f"answer {qid!r} selected several labels but multiSelect is off"
            )
        if len(set(selected)) != len(selected):
            raise AnswerValidationError(
                f"answer {qid!r} selected the same label twice"
            )
        other: Optional[str] = None
        if has_other:
            if len(raw_other) > _MAX_ANSWER_TEXT_LEN:
                raise AnswerValidationError(
                    f"answer {qid!r} other text too long "
                    f"(max {_MAX_ANSWER_TEXT_LEN})"
                )
            other = raw_other
        normalized[qid] = {"selected": selected, "other": other}
    return normalized


def _maybe_ask_user_question_decision(
    response: LLMResponse,
    assistant_message: Message,
    *,
    content_store: Optional[ContentStore],
    assistant_thinking: tuple[ThinkingBlock, ...] = (),
) -> Decision | None:
    """Translate an ``ask_user_question`` call into the neutral HITL primitive.

    A valid call becomes a :class:`YieldForHumanDecision` carrying an opaque
    :class:`HitlRequestAnchor` (the ContentStore-backed ``questions_ref`` plus
    the ``question-<id>`` handle): the kernel writes the audit anchor and
    suspends without decoding the schema, so a valid call deliberately has no
    immediate tool-result ack. A mixed or malformed call becomes a recoverable
    :class:`StatePatchDecision` — assistant tool_use plus an error ack, no
    patch, no suspend — so the model can retry.
    """
    tool_uses = [b for b in response.content if isinstance(b, ToolUseBlock)]
    ask_blocks = [
        b for b in tool_uses if b.tool_name == ASK_USER_QUESTION_TOOL
    ]
    if not ask_blocks:
        return None

    if len(ask_blocks) != len(tool_uses) or len(ask_blocks) != 1:
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text="ask_user_question must be the only tool call in the turn",
            valid=False,
        )
    block = ask_blocks[0]
    ok, call_id_or_error = validate_call_id(block.call_id)
    if not ok:
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=call_id_or_error,
            valid=False,
        )
    ok, result = validate_question_arguments(block.arguments)
    if not ok:
        assert isinstance(result, str)
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=result,
            valid=False,
        )
    assert isinstance(result, tuple)
    questions, reason = result
    call_id = call_id_or_error
    if content_store is None:
        raise RuntimeError(
            "ask_user_question requires a content_store on ReActPolicy; "
            "the runner/resume must thread it into the policy factory"
        )
    questions_ref = put_questions_body(content_store, questions)
    return YieldForHumanDecision(
        prompt="",
        assistant_message=assistant_message,
        assistant_thinking=assistant_thinking,
        request_anchor=HitlRequestAnchor(
            questions_ref=questions_ref,
            question_count=len(questions),
            handle=question_handle(call_id),
            request_id=call_id,
            reason=reason,
        ),
    )


def translate_ask_user_question(ctx: ControlTranslateContext) -> Optional[Decision]:
    """The ``ask_user_question`` routing seam the mount binds into a spec."""
    return _maybe_ask_user_question_decision(
        ctx.response,
        ctx.assistant_message,
        content_store=ctx.content_store,
        assistant_thinking=ctx.assistant_thinking,
    )


#: The answer-side codec the kernel driver's ``answer`` path consumes, carried
#: on the ask mount's typed ``answer_codec`` field. Built once at import — the
#: three functions are stateless.
ASK_ANSWER_CODEC = AskAnswerCodec(
    question_handle=question_handle,
    load_questions_body=load_questions_body,
    normalize_answer_document=normalize_answer_document,
)


def build_ask_user_question_control_tool(
    ctx: ControlToolBuildContext,
) -> Optional[ControlToolMount]:
    """The ``control_tool`` contribution factory (manifest ``ref`` target).

    Self-gates on the effective ``ask_user_question`` capability flag: mounting
    is enablement. Routing band 100 and schema band 300 are the byte-order
    contract the control-tool schema goldens pin — do not renumber.
    """
    if not ctx.flag("ask_user_question"):
        return None
    return ControlToolMount(
        name=ASK_USER_QUESTION_TOOL,
        schema=ask_user_question_tool_schema(),
        translate=translate_ask_user_question,
        routing_priority=100,
        schema_priority=300,
        answer_codec=ASK_ANSWER_CODEC,
    )
