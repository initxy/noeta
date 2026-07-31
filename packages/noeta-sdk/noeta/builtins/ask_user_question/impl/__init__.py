"""``ask_user_question`` — the structured-HITL control tool, as a built-in plugin.

Control-tool-surface S2 (D3 + D8): ``ask_user_question``'s whole story moved out
of the kernel's control band into this built-in — its provider-visible schema,
its argument validators, its response→neutral-Decision translate body, its
``.md`` description, AND its answer-side codec (``question_handle`` /
``load_questions_body`` / ``normalize_answer_document``). The move is
byte-preserving (the S0 golden pins the schema bytes).

The answer codec is kernel RESIDUE: the driver's ``answer`` path decodes a
submitted answer body, but the kernel can no longer import the codec statically
(it lives here now, and the kernel never imports ``noeta.builtins``). So the
mount carries it on its typed ``answer_codec`` field (spec §4.3) — the builder
collects it, the host threads it onto the session's Engine, and the driver
reads it there, failing loudly for a session that never mounted
this tool. What this impl imports back from the kernel is neutral mechanism: the
mount + codec types (``noeta.execution.control_tool``), the decision-time
``ControlTranslateContext``, the shared ack builder ``ack_patch_decision``, and
the canonical-bytes codec.

Reached only through the plugin loader's ``ref`` resolution; nothing imports it
statically.
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


ASK_USER_QUESTION_TOOL = "ask_user_question"
QUESTION_HANDLE_PREFIX = "question-"
QUESTION_BODY_MEDIA_TYPE = "application/json"

_HANDLE_SAFE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_QUESTIONS = 3
_MAX_QUESTION_ID_LEN = 64
_MAX_QUESTION_TEXT_LEN = 500
_MAX_HEADER_LEN = 40
_MAX_CHOICES = 5
_MAX_CHOICE_LABEL_LEN = 80
_MAX_CHOICE_DESCRIPTION_LEN = 300
_MAX_REASON_LEN = 500
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
                                "id": {"type": "string"},
                                "question": {"type": "string"},
                                "header": {"type": "string"},
                                "choices": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string"},
                                            "label": {"type": "string"},
                                            "description": {"type": "string"},
                                        },
                                        "required": ["id", "label"],
                                    },
                                },
                                "allow_freeform": {"type": "boolean"},
                            },
                            "required": ["id", "question"],
                        },
                    },
                    "reason": {"type": "string"},
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
        return False, "ask_user_question arguments must be an object"
    raw_questions = arguments.get("questions")
    if not isinstance(raw_questions, list):
        return False, "questions must be a list"
    if not 1 <= len(raw_questions) <= _MAX_QUESTIONS:
        return False, f"questions must contain 1-{_MAX_QUESTIONS} items"
    reason = arguments.get("reason")
    if reason is not None:
        if not isinstance(reason, str):
            return False, "reason must be a string"
        if len(reason) > _MAX_REASON_LEN:
            return False, f"reason too long (max {_MAX_REASON_LEN})"

    seen_question_ids: set[str] = set()
    questions: list[dict[str, Any]] = []
    for item in raw_questions:
        if not isinstance(item, dict):
            return False, "each question must be an object"
        qid = item.get("id")
        if not isinstance(qid, str) or not qid:
            return False, "each question needs a non-empty string id"
        if len(qid) > _MAX_QUESTION_ID_LEN or not is_question_id(qid):
            return False, "question id must match ^[A-Za-z0-9_-]{1,64}$"
        if qid in seen_question_ids:
            return False, f"duplicate question id: {qid!r}"
        seen_question_ids.add(qid)

        question = item.get("question")
        if not isinstance(question, str) or not question:
            return False, "each question needs a non-empty question string"
        if len(question) > _MAX_QUESTION_TEXT_LEN:
            return False, f"question too long (max {_MAX_QUESTION_TEXT_LEN})"

        header = item.get("header")
        if header is not None:
            if not isinstance(header, str):
                return False, "header must be a string"
            if len(header) > _MAX_HEADER_LEN:
                return False, f"header too long (max {_MAX_HEADER_LEN})"

        allow_freeform = item.get("allow_freeform", True)
        if not isinstance(allow_freeform, bool):
            return False, "allow_freeform must be a boolean"

        choices, error = _normalize_choices(item.get("choices"))
        if error is not None:
            return False, error
        if not choices and not allow_freeform:
            return False, (
                "each question must provide choices or allow freeform answers"
            )
        questions.append(
            {
                "id": qid,
                "question": question,
                "header": header,
                "choices": choices,
                "allow_freeform": allow_freeform,
            }
        )
    return True, (questions, reason)


def _normalize_choices(raw: Any) -> tuple[list[dict[str, Any]], Optional[str]]:
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return [], "choices must be a list when present"
    if not 1 <= len(raw) <= _MAX_CHOICES:
        return [], f"choices must contain 1-{_MAX_CHOICES} items"
    seen: set[str] = set()
    choices: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            return [], "each choice must be an object"
        cid = item.get("id")
        if not isinstance(cid, str) or not cid:
            return [], "each choice needs a non-empty string id"
        if len(cid) > _MAX_QUESTION_ID_LEN or not is_question_id(cid):
            return [], "choice id must match ^[A-Za-z0-9_-]{1,64}$"
        if cid in seen:
            return [], f"duplicate choice id: {cid!r}"
        seen.add(cid)
        label = item.get("label")
        if not isinstance(label, str) or not label:
            return [], "each choice needs a non-empty string label"
        if len(label) > _MAX_CHOICE_LABEL_LEN:
            return [], f"choice label too long (max {_MAX_CHOICE_LABEL_LEN})"
        description = item.get("description")
        if description is not None:
            if not isinstance(description, str):
                return [], "choice description must be a string"
            if len(description) > _MAX_CHOICE_DESCRIPTION_LEN:
                return [], (
                    "choice description too long "
                    f"(max {_MAX_CHOICE_DESCRIPTION_LEN})"
                )
        choices.append(
            {"id": cid, "label": label, "description": description}
        )
    return choices, None


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

    ``raw`` may be either ``{"answers": {...}}`` or the direct answer map.
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
        # B17 / U6 — a chosen option and a freeform note may COEXIST (product
        # direction ①): validate each field independently and require AT LEAST
        # one. A missing/blank text is treated as absent, so "pick a choice and
        # leave the other box empty" is just the choice. The normalized shape is
        # unchanged ({choice_id, text} with None for the absent field), so older
        # single-field recordings stay byte-identical and replay-safe; the rule
        # only loosens (anything valid before is still valid).
        raw_choice = raw_answer.get("choice_id")
        raw_text = raw_answer.get("text")
        # P2 hardening — a present but non-string text is malformed, not absent;
        # reject it explicitly (consistent with validating each field on its own).
        # None and a blank string are still treated as "no text given".
        if raw_text is not None and not isinstance(raw_text, str):
            raise AnswerValidationError(f"answer {qid!r} text must be a string")
        has_choice = raw_choice is not None
        has_text = isinstance(raw_text, str) and raw_text.strip() != ""
        if not has_choice and not has_text:
            raise AnswerValidationError(
                f"answer {qid!r} must contain a choice_id or non-empty text"
            )
        choice_id: Optional[str] = None
        if has_choice:
            if not isinstance(raw_choice, str) or not raw_choice:
                raise AnswerValidationError(
                    f"answer {qid!r} choice_id must be a non-empty string"
                )
            choices = question.get("choices")
            if not isinstance(choices, list):
                choices = []
            allowed = {
                str(choice.get("id"))
                for choice in choices
                if isinstance(choice, dict)
            }
            if raw_choice not in allowed:
                raise AnswerValidationError(
                    f"answer {qid!r} choice_id {raw_choice!r} is not allowed"
                )
            choice_id = raw_choice
        text: Optional[str] = None
        if has_text:
            if not question.get("allow_freeform", True):
                raise AnswerValidationError(
                    f"answer {qid!r} does not allow freeform text"
                )
            if len(raw_text) > _MAX_ANSWER_TEXT_LEN:
                raise AnswerValidationError(
                    f"answer {qid!r} text too long (max {_MAX_ANSWER_TEXT_LEN})"
                )
            text = raw_text
        normalized[qid] = {"choice_id": choice_id, "text": text}
    return normalized


def _maybe_ask_user_question_decision(
    response: LLMResponse,
    assistant_message: Message,
    *,
    content_store: Optional[ContentStore],
    assistant_thinking: tuple[ThinkingBlock, ...] = (),
) -> Decision | None:
    """CW18d: translate `ask_user_question` into the neutral HITL
    primitive.

    A valid call becomes a :class:`YieldForHumanDecision` carrying an
    opaque :class:`HitlRequestAnchor` (the SDK builds the
    ContentStore-backed ``questions_ref`` + the ``question-<id>`` handle):
    the kernel writes the neutral ``UserQuestionRequested`` audit anchor
    and suspends, never decoding the schema. A mixed/malformed call becomes
    a recoverable :class:`StatePatchDecision` (assistant tool_use +
    error ack, no patch, no suspend).

    The ask branch owns the whole turn before other control/tool routing;
    a valid call suspends and therefore intentionally has no immediate
    tool-result ack.
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
#: on the ask mount's typed ``answer_codec`` field (spec §4.3). Built ONCE at
#: import (the three functions are stateless).
ASK_ANSWER_CODEC = AskAnswerCodec(
    question_handle=question_handle,
    load_questions_body=load_questions_body,
    normalize_answer_document=normalize_answer_document,
)


def build_ask_user_question_control_tool(
    ctx: ControlToolBuildContext,
) -> Optional[ControlToolMount]:
    """The ``control_tool`` contribution factory (manifest ``ref`` target).

    Self-gates on the effective ``ask_user_question`` capability flag and
    reproduces the pre-migration internal ``_ask_user_question_mount`` exactly:
    routing band 100, schema band 300 (the S0 golden byte order). It also
    carries the answer codec on the mount's typed ``answer_codec`` field so the
    driver can decode a submitted answer without importing this built-in.
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
