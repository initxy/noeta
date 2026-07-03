"""Control-tool semantics, collocated **per control tool**.

A coding agent's model-visible **control** tools — ``todo_write`` /
``ask_user_question`` / ``skill`` / ``spawn_subagent`` / ``run_workflow`` /
``structured_output`` — are SDK product **material** (mechanism-vs-material;
demoted from the kernel). The kernel
sees only neutral mechanism: a
:class:`~noeta.protocols.decisions.StatePatchDecision` applies caller-built
messages + a typed patch, a
:class:`~noeta.protocols.decisions.YieldForHumanDecision` carries an opaque
:class:`~noeta.protocols.decisions.HitlRequestAnchor`, a
:class:`~noeta.protocols.decisions.SpawnSubtaskDecision` delegates.

This module is the **single home** for each control tool's whole story —
its provider-visible schema, its argument validators / codec, and its
response→neutral-Decision translation seam, collocated in one per-tool
section so a maintainer reads one concept in one place (locality). It was
formed by merging the old ``control_tools`` (schema + validator + codec)
and ``_control_translate`` (the ``_maybe_*`` response→Decision seam) files,
which had split each control tool's concept across two files (a phase split,
not a tool boundary). The merge is byte-preserving: every schema, every
validation branch, every ack/error string, the routing priority, and the
extended-thinking threading are unchanged so the same LLM response still
produces the same Decision and the same rendered tool-schema bytes
(``View.provider_tool_schemas`` + the stable hash). The thin
``control_tools`` and ``_control_translate`` modules now re-export from here
so every existing import path keeps working unchanged.

The translation seam does NOT participate in stable-prefix schema
assembly (the Composer owns ``control_action_schemas``); it only translates
*responses*. ``plan`` permission mode and its enter/exit_plan_mode control
tools were removed.

Layering: imports only ``noeta.protocols.*`` and the sibling
``noeta.policies.descriptions`` / ``noeta.policies._workflow_sandbox`` — no
cross-band edge, no ``ReActPolicy`` import (so ``react`` may depend on this
module without a cycle).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional

from noeta.policies.descriptions import load_control_tool_description
from noeta.policies._workflow_sandbox import check_workflow_script
from noeta.protocols.canonical import from_canonical_bytes, to_canonical_bytes
from noeta.protocols.content_store import ContentStore
from noeta.protocols.decisions import (
    Decision,
    HitlRequestAnchor,
    SpawnSubtaskDecision,
    SpawnSubtaskSpec,
    SpawnSubtasksDecision,
    StatePatchDecision,
    TaskStatePatch,
    YieldForHumanDecision,
)
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.values import ContentRef


__all__ = [
    # todo_write
    "TODO_WRITE_TOOL",
    "TODO_WRITE_STATUSES",
    "todo_write_tool_schema",
    "validate_todos",
    # string validator (plan removed; skill still uses validate_required_string)
    "validate_required_string",
    # ask_user_question
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
    # skill
    "SKILL_TOOL",
    "skill_tool_schema",
    # spawn_subagent
    "SPAWN_SUBAGENT_TOOL",
    "spawn_subagent_tool_schema",
    # run_workflow
    "RUN_WORKFLOW_TOOL",
    "WORKFLOW_AGENT_NAME",
    "run_workflow_tool_schema",
    # structured_output
    "STRUCTURED_OUTPUT_TOOL",
    "structured_output_tool_schema",
    # translation seam
    "ControlToggles",
    "translate_control_tool",
]


# ===========================================================================
# Shared control-tool primitives
# ===========================================================================


def validate_required_string(
    value: Any, name: str, max_len: int
) -> tuple[bool, Optional[str]]:
    if not isinstance(value, str) or not value:
        return False, f"{name} must be a non-empty string"
    if len(value) > max_len:
        return False, f"{name} too long (max {max_len})"
    return True, value


def _enum_roster_prop(base_description: str, items) -> dict[str, Any]:
    """Build a string property with an enum constraint and a roster description; with empty ``items`` it is just the bare description."""
    prop: dict[str, Any] = {"type": "string", "description": base_description}
    if items:
        prop["enum"] = [name for name, _ in items]
        roster = "; ".join(
            f"{name} — {desc}" if desc else name for name, desc in items
        )
        prop["description"] = base_description + " Available: " + roster
    return prop


def _ack_patch_decision(
    tool_uses,
    assistant_message,
    assistant_thinking,
    *,
    patch,
    text: str,
    valid: bool,
) -> StatePatchDecision:
    """Shared ack builder for control tools: one ToolResultBlock per tool_use, wrapped in a StatePatchDecision."""
    ack = Message(
        role="tool",
        content=[
            ToolResultBlock(
                call_id=b.call_id,
                output=text,
                success=valid,
                error=None if valid else text,
            )
            for b in tool_uses
        ],
    )
    return StatePatchDecision(
        messages_before=(assistant_message,),
        patch=patch,
        messages_after=(ack,),
        assistant_thinking=assistant_thinking,
    )


@dataclass(frozen=True)
class ControlToggles:
    """The default-off enable flags that gate each control-tool branch.

    Collected from the ``*_enabled`` booleans ``ReActPolicy`` carries so the
    translation seam takes one small value object instead of four args. The
    routing order in :func:`translate_control_tool` (ask → plan → todo →
    spawn → skill) is fixed and independent of these flags — a disabled branch
    is simply skipped, exactly as the four nested ``if self._*_enabled`` guards
    did in ``react.py``.
    """

    ask_user_question: bool = False
    todo_write: bool = False
    delegation: bool = False
    skill_invocation: bool = False
    #: gate the ``run_workflow`` control tool. Routed last (after
    #: ``skill``); a disabled branch is skipped, default off so existing
    #: recordings/stable hashes are unchanged.
    workflow: bool = False


def translate_control_tool(
    response: LLMResponse,
    assistant_message: Message,
    *,
    toggles: ControlToggles,
    content_store: Optional[ContentStore] = None,
    skill_menu_names: frozenset[str] = frozenset(),
) -> Decision | None:
    """Translate a control-tool ``tool_use`` turn into a neutral Decision.

    Tries each enabled control tool in the FIXED routing priority order —
    ``ask_user_question`` → ``todo_write`` → ``spawn_subagent`` → ``skill``
    — and returns the first non-``None`` Decision. Returns ``None`` when no
    enabled control tool is present (the caller then falls through to the
    normal ``tool_calls`` path). Order matters when several control tools
    co-occur in one turn; it is byte-identical to the original nested
    ``_maybe_*`` dispatch in ``ReActPolicy._response_to_decision``.

    Extended-thinking end-to-end (Slice B): the LLM's ThinkingBlocks are
    extracted ONCE from ``response.content`` here and threaded into every
    control Decision the helpers build, matching the parallel
    ``ToolCallsDecision`` path in ``react.py`` so a reasoning-model turn
    that emits thinking + a control tool_use still carries its signature.
    """
    # Extract out-of-band thinking once so every helper reuses the same tuple
    # (non-reasoning models → empty tuple, no-op, byte-safe).
    assistant_thinking: tuple[ThinkingBlock, ...] = tuple(
        b for b in response.content if isinstance(b, ThinkingBlock)
    )
    if toggles.ask_user_question:
        ask = _maybe_ask_user_question_decision(
            response,
            assistant_message,
            content_store=content_store,
            assistant_thinking=assistant_thinking,
        )
        if ask is not None:
            return ask
    if toggles.todo_write:
        todo = _maybe_todo_write_decision(
            response, assistant_message, assistant_thinking=assistant_thinking
        )
        if todo is not None:
            return todo
    if toggles.delegation:
        spawn = _maybe_spawn_decision(
            response, assistant_message, assistant_thinking=assistant_thinking
        )
        if spawn is not None:
            return spawn
    if toggles.skill_invocation:
        skill = _maybe_skill_decision(
            response,
            assistant_message,
            menu_names=skill_menu_names,
            assistant_thinking=assistant_thinking,
        )
        if skill is not None:
            return skill
    if toggles.workflow:
        workflow = _maybe_workflow_decision(
            response, assistant_message, assistant_thinking=assistant_thinking
        )
        if workflow is not None:
            return workflow
    return None


# ===========================================================================
# todo_write — schema + validator + translate
# ===========================================================================

#: Model-visible **control** tool name for durable checklist updates.
TODO_WRITE_TOOL = "todo_write"
#: Allowed ``status`` values for a todo item (Claude-style).
TODO_WRITE_STATUSES = ("pending", "in_progress", "completed")
#: Input caps. Over-cap → malformed (recoverable, no state write).
_TODO_MAX_ITEMS = 50
_TODO_MAX_ID_LEN = 64
_TODO_MAX_CONTENT_LEN = 500


_TODO_WRITE_DESCRIPTION = load_control_tool_description("todo_write")


def todo_write_tool_schema() -> dict[str, Any]:
    """Provider-visible schema for :data:`TODO_WRITE_TOOL`.

    A **control** tool (never an Engine/ToolRuntime tool): a single
    ``todo_write`` call replace-alls ``TaskState.todos`` via a
    ``StatePatchDecision`` (``set_todos`` patch) → ``TaskStatePatched``.
    Added to the Composer's ``control_action_schemas`` (so it lands in
    ``View.provider_tool_schemas`` + the stable hash) only when
    ``todo_write_enabled`` — never registered as a tool."""
    return {
        "type": "function",
        "function": {
            "name": TODO_WRITE_TOOL,
            "description": _TODO_WRITE_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": (
                            "The full checklist (replace-all). Each item: "
                            "{id, content, status}."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "content": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": list(TODO_WRITE_STATUSES),
                                },
                            },
                            "required": ["id", "content", "status"],
                        },
                    },
                },
                "required": ["todos"],
            },
        },
    }


def validate_todos(
    arguments: Any,
) -> tuple[bool, "list[dict[str, Any]] | str"]:
    """Validate a ``todo_write`` ``todos`` arg. Returns ``(True, todos)``
    with a normalized list, or ``(False, error)``. Caps + non-empty + unique
    ids + status enum; never raises (malformed input is data, not an error)."""
    todos = arguments.get("todos") if isinstance(arguments, dict) else None
    if not isinstance(todos, list):
        return False, "todos must be a list"
    if len(todos) > _TODO_MAX_ITEMS:
        return False, f"too many todos (max {_TODO_MAX_ITEMS})"
    seen_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in todos:
        if not isinstance(item, dict):
            return False, "each todo must be an object"
        tid = item.get("id")
        content = item.get("content")
        status = item.get("status")
        if not isinstance(tid, str) or not tid:
            return False, "each todo needs a non-empty string id"
        if len(tid) > _TODO_MAX_ID_LEN:
            return False, f"todo id too long (max {_TODO_MAX_ID_LEN})"
        if tid in seen_ids:
            return False, f"duplicate todo id: {tid!r}"
        seen_ids.add(tid)
        if not isinstance(content, str) or not content:
            return False, "each todo needs non-empty string content"
        if len(content) > _TODO_MAX_CONTENT_LEN:
            return False, f"todo content too long (max {_TODO_MAX_CONTENT_LEN})"
        if status not in TODO_WRITE_STATUSES:
            return False, (
                "todo status must be one of " + ", ".join(TODO_WRITE_STATUSES)
            )
        normalized.append({"id": tid, "content": content, "status": status})
    return True, normalized


def _maybe_todo_write_decision(
    response: LLMResponse,
    assistant_message: Message,
    *,
    assistant_thinking: tuple[ThinkingBlock, ...] = (),
) -> Decision | None:
    """CW18b: translate a `todo_write` control-tool call into a neutral
    :class:`StatePatchDecision`, or ``None`` when no `todo_write` is present.

    Rules: `todo_write` must be the **sole** tool call in the turn (mixed
    with any other tool → recoverable error, no state write). Input is
    validated (list of ``{id, content, status}`` with caps + non-empty,
    unique ids); malformed → a ``StatePatchDecision`` with ``patch=None``
    whose ack carries the error so the model can retry (the task is NOT
    terminated). The kernel emits ``messages_before`` (assistant tool_use)
    → ``TaskStatePatched`` (only when ``patch`` set) → ``messages_after``
    (ack), emitting the same assistant → patch → ack sequence each run."""
    tool_uses = [b for b in response.content if isinstance(b, ToolUseBlock)]
    todo_blocks = [b for b in tool_uses if b.tool_name == TODO_WRITE_TOOL]
    if not todo_blocks:
        return None

    if len(todo_blocks) != len(tool_uses) or len(todo_blocks) != 1:
        return _ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text="todo_write must be the only tool call in the turn",
            valid=False,
        )
    ok, result = validate_todos(todo_blocks[0].arguments)
    if not ok:
        assert isinstance(result, str)
        return _ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=result,
            valid=False,
        )
    assert isinstance(result, list)
    return _ack_patch_decision(
        tool_uses,
        assistant_message,
        assistant_thinking,
        patch=TaskStatePatch(set_todos=list(result)),
        text=f"todos updated: {len(result)} item(s)",
        valid=True,
    )


# ===========================================================================
# ask_user_question — schema, caps, validators, codec, translate
# ===========================================================================

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


_ASK_USER_QUESTION_DESCRIPTION = load_control_tool_description(
    "ask_user_question"
)


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
        return _ack_patch_decision(
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
        return _ack_patch_decision(
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
        return _ack_patch_decision(
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


# ===========================================================================
# skill (D2 / D4) — model-driven skill invocation control tool
# ===========================================================================

#: Model-visible **control** tool name for model-driven skill menu selection.
SKILL_TOOL = "skill"


_SKILL_DESCRIPTION = load_control_tool_description("skill")


def skill_tool_schema(
    menu: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    """Provider-visible schema for :data:`SKILL_TOOL`.

    A **control** tool (never an Engine/ToolRuntime tool): a ``skill`` call
    activates a named skill via a ``StatePatchDecision`` (``activate_skills``
    patch), same channel pre-loop activations use. Added to the Composer's
    ``control_action_schemas`` only when ``skill_invocation_enabled`` AND
    the workspace has at least one indexed skill.

    ``menu`` is a sorted sequence of ``(name, description)`` pairs. The name
    is rendered into the ``skill`` property's ``enum``; the description is
    appended to its description as a human-readable roster, mirroring the
    ``spawn_subagent`` agent_directory pattern. A single required
    ``skill`` string parameter — no ``args``, no ``reason`` (D4).
    """
    skill_prop = _enum_roster_prop("Name of the skill to activate.", menu)
    return {
        "type": "function",
        "function": {
            "name": SKILL_TOOL,
            "description": _SKILL_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": skill_prop,
                },
                "required": ["skill"],
            },
        },
    }


# Max length for a skill name string (generous upper bound; the real gate is
# the menu enum, but this catches obviously-malformed payloads before we
# format the error roster).
_SKILL_NAME_MAX_LEN = 200


def _maybe_skill_decision(
    response: LLMResponse,
    assistant_message: Message,
    *,
    menu_names: frozenset[str],
    assistant_thinking: tuple[ThinkingBlock, ...] = (),
) -> Decision | None:
    """D1/D4: translate a `skill` control-tool call into a neutral
    :class:`StatePatchDecision`, or ``None`` when no `skill` is present.

    Rules mirror :func:`_maybe_todo_write_decision`: the `skill` call must be
    the **sole** tool call in the turn (mixed with any other tool →
    recoverable error ack, no state write). The ``skill`` argument is
    validated against the sorted menu set: a known name becomes a
    ``StatePatchDecision(activate_skills=[name])`` whose ack confirms the
    skill is loaded and will appear from the next turn; an unknown name
    becomes an error ack listing the available names so the model can retry.

    Duplicate activation (the name is already in ``active_skills``) is NOT
    special-cased here: the same success ack is returned and the state
    merge de-duplicates (``TaskStatePatch.apply`` unions ``activate_skills``
    with ``state.active_skills`` order-preserving). The kernel emits
    ``messages_before`` → ``TaskStatePatched`` → ``messages_after``, same
    sequence as ``todo_write``.
    """
    tool_uses = [b for b in response.content if isinstance(b, ToolUseBlock)]
    skill_blocks = [b for b in tool_uses if b.tool_name == SKILL_TOOL]
    if not skill_blocks:
        return None

    # Sole-call rule — exactly one `skill` block and nothing else.
    if len(skill_blocks) != len(tool_uses) or len(skill_blocks) != 1:
        return _ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text="skill must be the only tool call in the turn",
            valid=False,
        )

    block = skill_blocks[0]
    args = dict(block.arguments)
    ok, name_or_err = validate_required_string(
        args.get("skill"), "skill", _SKILL_NAME_MAX_LEN
    )
    if not ok:
        assert isinstance(name_or_err, str)
        return _ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=name_or_err,
            valid=False,
        )
    name = name_or_err
    assert isinstance(name, str)
    if name not in menu_names:
        available = ", ".join(sorted(menu_names)) if menu_names else "(none)"
        return _ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=f"unknown skill {name!r}; available: {available}",
            valid=False,
        )
    return _ack_patch_decision(
        tool_uses,
        assistant_message,
        assistant_thinking,
        patch=TaskStatePatch(activate_skills=[name]),
        text=f"Skill '{name}' loaded; its instructions will appear in your "
        f"context from the next turn.",
        valid=True,
    )


# ===========================================================================
# spawn_subagent — schema + translate
# ===========================================================================

#: Phase 4.5 Issue C — the model-visible **control** tool name a coding
#: parent calls to delegate to a named sub-agent. It is NOT an executable
#: workspace tool: the translation seam turns a single
#: ``ToolUseBlock(tool_name=SPAWN_SUBAGENT_TOOL)`` into a
#: ``SpawnSubtaskDecision`` and the ToolRuntime never invokes it. The
#: schema is generic (string ``agent`` / ``goal``) so this module needs
#: no ``noeta.agent`` import; authorization is the PermissionGuard's
#: ``allowed_subtask_agents``, not this schema.
SPAWN_SUBAGENT_TOOL = "spawn_subagent"


_SPAWN_SUBAGENT_DESCRIPTION = load_control_tool_description("spawn_subagent")


def spawn_subagent_tool_schema(
    agent_directory: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    """The provider-visible schema for :data:`SPAWN_SUBAGENT_TOOL`.

    Added to ``ThreeSegmentComposer.control_action_schemas`` (so it lands in
    ``View.provider_tool_schemas`` + the stable hash) only when delegation is
    enabled — never registered as an Engine tool. Carries a function-level
    ``description`` (externalized to
    ``policies/descriptions/spawn_subagent.md``).

    A non-empty ``agent_directory`` — a sorted tuple of ``(name, description)``
    pairs — annotates each spawn entry's ``agent`` property with an ``enum``
    (the list of allowed names, in order) and appends the human-readable roster
    to its description.

    The parameters advertise the **batch form**: a required ``spawns`` array of
    ``{agent, goal}`` entries. One entry = the classic single delegate-and-wait;
    several entries = a one-call concurrent fan-out. The array IS the schema
    because a single call carrying N entries is the only shape gpt-5.x models
    reliably batch — the same models essentially never emit two spawn tool
    calls in one turn, no matter what the description or the user demands
    (probed live, 17/17 single). The translate seam still accepts the legacy
    top-level ``{agent, goal}`` single form (old recordings replay byte-equal,
    and the workflow orchestration fabricates that form).
    """
    agent_prop = _enum_roster_prop(
        "Named sub-agent to delegate to.", agent_directory
    )
    return {
        "type": "function",
        "function": {
            "name": SPAWN_SUBAGENT_TOOL,
            "description": _SPAWN_SUBAGENT_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "spawns": {
                        "type": "array",
                        "minItems": 1,
                        "description": (
                            "The sub-agents to spawn. ONE entry delegates and "
                            "waits for that single result. SEVERAL entries fan "
                            "out and run CONCURRENTLY; their results return "
                            "together, in entry order. Always batch independent "
                            "goals into one call — spawning one entry per turn "
                            "is strictly sequential, never parallel."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent": agent_prop,
                                "goal": {
                                    "type": "string",
                                    "description": (
                                        "The focused goal for this sub-agent."
                                    ),
                                },
                            },
                            "required": ["agent", "goal"],
                        },
                    },
                    # background sub-agent (docs/adr/background-subagent.md): a
                    # single spawn with background=true does NOT block this turn —
                    # the parent gets a "started" receipt and keeps going, and the
                    # sub-agent's result is delivered as a notice when it finishes.
                    "background": {
                        "type": "boolean",
                        "description": (
                            "Run the sub-agent in the background instead of "
                            "waiting for it. With background=true you immediately "
                            "get a 'started' acknowledgement and keep working; the "
                            "sub-agent runs concurrently and its result is "
                            "delivered to you automatically when it finishes — you "
                            "never poll or wait. Use it for independent, "
                            "longer-running work (research, a broad scan) you want "
                            "off the critical path. Omit it (the default) to "
                            "delegate and wait for the result inline. Only valid "
                            "with exactly ONE spawns entry (a fan-out batch is "
                            "always foreground)."
                        ),
                    },
                },
                "required": ["spawns"],
            },
        },
    }


#: fan-out v2 master switch — now
#: **default ON**.
#: Both the SR2 ``spawn_subagent`` fan-out (:func:`_maybe_spawn_decision` below)
#: and the workflow ``parallel()`` (``orchestration.parallel``) read this one
#: judgment, so a single env var is the escape valve: set
#: ``NOETA_SUBTASK_CONCURRENCY`` to ``0``/``false``/``off``/``no`` to force the
#: legacy sequential drain. Unset (or anything unrecognized) ⇒ concurrent.
#:
#: Lives here, not in ``orchestration``, so :func:`_maybe_spawn_decision` can
#: reach it without a ``control_semantics → orchestration`` import cycle
#: (``orchestration`` imports ``control_tools``, a re-export shim for this
#: module); ``orchestration`` imports the helper back from here.
_SUBTASK_CONCURRENCY_ENV = "NOETA_SUBTASK_CONCURRENCY"


def _concurrent_fanout_enabled() -> bool:
    """True unless the escape valve forces a sequential group drain.

    Default ON: an unset (or unrecognized) ``NOETA_SUBTASK_CONCURRENCY`` means
    concurrent; only an explicit ``0``/``false``/``off``/``no`` (case-insensitive,
    whitespace-trimmed) forces the legacy sequential drain. A sequential group's
    ``SubtaskGroupCompleted`` carries no ``concurrent`` field (conditionally
    folded in the Engine), so it stays byte-identical to a pre-v2 recording.
    """
    return os.environ.get(_SUBTASK_CONCURRENCY_ENV, "").strip().lower() not in {
        "0", "false", "off", "no",
    }


def _spawn_call_members(args: dict[str, Any]) -> list[tuple[str, str]] | None:
    """The ``(agent, goal)`` members of ONE ``spawn_subagent`` call.

    Batch form: the ``spawns`` array (each entry ``{agent, goal}``) — the
    shape the advertised schema carries. Legacy single form: top-level
    ``{agent, goal}`` when ``spawns`` is absent — every pre-batch recording
    replays through this branch byte-equal, and the workflow orchestration
    fabricates it. Returns ``None`` when ``spawns`` is present but malformed
    (not a non-empty array of ``{agent, goal}`` objects) — the caller acks a
    recoverable error instead of letting empty names fail the task at the
    permission guard.
    """
    if "spawns" not in args:
        return [(str(args.get("agent", "")), str(args.get("goal", "")))]
    raw = args.get("spawns")
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    members: list[tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict) or "agent" not in entry or "goal" not in entry:
            return None
        members.append((str(entry.get("agent", "")), str(entry.get("goal", ""))))
    return members


def _maybe_spawn_decision(
    response: LLMResponse,
    assistant_message: Message,
    *,
    assistant_thinking: tuple[ThinkingBlock, ...] = (),
) -> Decision | None:
    """Issue C / SR2: translate `spawn_subagent` tool_use(s) into a
    spawn decision, or fail closed on a mixed batch.

    Returns ``None`` when no `spawn_subagent` is present (normal
    tool-call path). Each call expands to its member list via
    :func:`_spawn_call_members` (the batch ``spawns`` array, or the legacy
    single ``{agent, goal}`` form). Routing on the flattened member total:

    * `spawn_subagent` **mixed with any non-spawn** tool call →
      recoverable error ack (``StatePatchDecision`` with ``patch=None``,
      one ``ToolResultBlock(success=False)`` per call_id). The task is
      NOT terminated — the model can retry in a later turn. This matches
      the sibling control tools' sole-call philosophy (D4).
    * a malformed ``spawns`` argument → the same recoverable error ack.
    * exactly **one** member in total →
      `SpawnSubtaskDecision` (SR1 single-child path, unchanged —
      ``spawns`` with one entry behaves exactly like the legacy form).
    * **≥2** members (one call carrying an array, several calls, or a mix)
      → `SpawnSubtasksDecision` (SR2 fan-out; member order = call order,
      then entry order within a call; members of one call share its
      ``call_id`` and are numbered by ``member_index``). The
      `spawn_subagent` tool is never invoked through the ToolRuntime.
    """
    tool_uses = [
        b for b in response.content if isinstance(b, ToolUseBlock)
    ]
    spawn_blocks = [
        b for b in tool_uses if b.tool_name == SPAWN_SUBAGENT_TOOL
    ]
    if not spawn_blocks:
        return None
    if len(spawn_blocks) != len(tool_uses):
        # at least one non-spawn tool_use is present → mixed batch.
        return _ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text="spawn_subagent cannot be mixed with other tool calls in the same turn",
            valid=False,
        )
    members_per_call: list[tuple[ToolUseBlock, list[tuple[str, str]]]] = []
    for block in spawn_blocks:
        members = _spawn_call_members(dict(block.arguments))
        if members is None:
            return _ack_patch_decision(
                tool_uses,
                assistant_message,
                assistant_thinking,
                patch=None,
                text=(
                    "spawn_subagent: 'spawns' must be a non-empty array of "
                    "{agent, goal} objects"
                ),
                valid=False,
            )
        members_per_call.append((block, members))
    if sum(len(m) for _, m in members_per_call) == 1:
        block, members = members_per_call[0]
        agent_name, goal = members[0]
        return SpawnSubtaskDecision(
            agent_name=agent_name,
            goal=goal,
            assistant_message=assistant_message,
            assistant_thinking=assistant_thinking,
            # docs/adr/background-subagent.md: a lone spawn with background=True
            # does NOT suspend the parent on a barrier — the Engine launches it on
            # the background-subagent driver and the parent keeps its turn. Only
            # the single-spawn path reads it; the fan-out below stays foreground
            # (background is documented as single-entry-only and ignored there).
            background=bool(dict(block.arguments).get("background", False)),
        )
    # SR2: ≥2 members, all-spawn turn → fan-out batch. Members of one batch
    # call share its call_id, contiguously, numbered 0..k-1 — the resume
    # pairing expands each assistant tool_use by its member count and the
    # Engine renders one aggregated tool_result per call.
    specs = tuple(
        SpawnSubtaskSpec(
            agent_name=agent_name,
            goal=goal,
            call_id=block.call_id,
            member_index=index,
        )
        for block, members in members_per_call
        for index, (agent_name, goal) in enumerate(members)
    )
    return SpawnSubtasksDecision(
        specs=specs,
        assistant_message=assistant_message,
        assistant_thinking=assistant_thinking,
        # a one-turn fan-out of ≥2 spawn members IS an explicit "run these
        # in parallel" intent, so it drives wall-clock concurrently by default
        # (same escape valve as the workflow ``parallel()``). The Engine folds
        # this transient opt-in onto the persisted ``SubtaskGroupCompleted``
        # (``concurrent or None``), so a forced-sequential group stays
        # byte-identical to a pre-v2 recording.
        concurrent=_concurrent_fanout_enabled(),
    )


# ===========================================================================
# run_workflow — schema + translate
# ===========================================================================

#: Model-visible **control** tool name: launch a model-authored orchestration
#: script that fans agents out as real subtasks. Translated into
#: a ``SpawnSubtaskDecision`` whose child carries the orchestration interpreter
#: Policy — same family / plumbing as ``spawn_subagent``.
RUN_WORKFLOW_TOOL = "run_workflow"

#: Reserved ``agent_name`` carried by the ``run_workflow`` → ``SpawnSubtaskDecision``
#: translation. The host's child-engine builder routes a child with this name to
#: :class:`noeta.policies.orchestration.OrchestrationPolicy` (reading script/args
#: from the child's ``TaskCreated.inputs``) instead of resolving a roster agent.
#: It belongs to the PermissionGuard ``allowed_subtask_agents`` allow-list (so the
#: orchestration spawn passes) but NOT to the model-facing ``spawn_subagent``
#: directory — the model reaches a workflow only through ``run_workflow``.
#: Defined here (not in ``orchestration``) so the translation seam can name it
#: without importing ``orchestration`` (which would cycle through ``react``).
WORKFLOW_AGENT_NAME = "__workflow__"

#: Structured semantics: the model's single source of truth for how
#: to author a workflow script — the available names, the return convention, and
#: the determinism constraint. Lives in an independent text resource
#: (``policies/descriptions/run_workflow.md``, four-section shape), not a Python
#: string. ``run_workflow`` is a **control-layer orchestration
#: tool** — it goes through ``SpawnSubtaskDecision`` (→ ``OrchestrationPolicy``),
#: NOT the ToolRuntime — so its description resource lives beside the control
#: vocabulary in ``noeta.policies`` rather than in ``noeta.tools.descriptions``
#: (a ``policies → tools`` import is forbidden by the layering contract; the
#: loader mechanism is mirrored, not imported).
_RUN_WORKFLOW_DESCRIPTION = load_control_tool_description("run_workflow")


def run_workflow_tool_schema() -> dict[str, Any]:
    """Provider-visible schema for :data:`RUN_WORKFLOW_TOOL`.

    A **control** tool (never an Engine/ToolRuntime tool): a single
    ``run_workflow`` call is translated into a ``SpawnSubtaskDecision`` whose
    child carries the orchestration interpreter Policy
    (:class:`noeta.policies.orchestration.OrchestrationPolicy`). Added to the
    Composer's ``control_action_schemas`` (so it lands in
    ``View.provider_tool_schemas`` + the stable hash) only when
    ``workflow_enabled`` — never registered as a ToolRuntime tool.
    """
    return {
        "type": "function",
        "function": {
            "name": RUN_WORKFLOW_TOOL,
            "description": _RUN_WORKFLOW_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": (
                            "The orchestration script (Python). Calls "
                            "parallel()/agent()/log(), reads args, and uses "
                            "`return` for the final answer."
                        ),
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Optional arguments exposed to the script as `args`."
                        ),
                    },
                },
                "required": ["script"],
            },
        },
    }


#: cap on the model-authored workflow script (recoverable over-cap,
#: like the other control-tool input caps; keeps the SubtaskSpawned/TaskCreated
#: inputs body bounded).
_WORKFLOW_MAX_SCRIPT_LEN = 16_000

#: Fixed (model-independent) goal seeded onto the orchestration subtask. The
#: OrchestrationPolicy reads the script from ``inputs``, not the goal — a
#: constant keeps the recorded subtask goal stable across resume.
_WORKFLOW_GOAL = "Execute workflow orchestration script."


def _maybe_workflow_decision(
    response: LLMResponse,
    assistant_message: Message,
    *,
    assistant_thinking: tuple[ThinkingBlock, ...] = (),
) -> Decision | None:
    """Translate a `run_workflow` control-tool call into a
    :class:`SpawnSubtaskDecision` whose child carries the orchestration
    interpreter Policy.

    Same family / plumbing as ``spawn_subagent`` — it just names the reserved
    :data:`WORKFLOW_AGENT_NAME` and ferries ``{script, args}`` through
    ``inputs`` (→ ``SubtaskSpawned`` → child ``TaskCreated.inputs``), where the
    host's child-engine builder reads them to construct
    :class:`noeta.policies.orchestration.OrchestrationPolicy`.

    Sole-call rule (mirrors the sibling control tools): ``run_workflow`` must be
    the only tool call in the turn; mixed → recoverable error ack (no subtask).
    A missing / non-string / empty / over-cap ``script`` is likewise a
    recoverable error. The script's deterministic-sandbox guard (AST) is applied
    downstream (issue 03); this seam only validates the call shape.
    """
    tool_uses = [b for b in response.content if isinstance(b, ToolUseBlock)]
    workflow_blocks = [b for b in tool_uses if b.tool_name == RUN_WORKFLOW_TOOL]
    if not workflow_blocks:
        return None
    if len(workflow_blocks) != len(tool_uses) or len(workflow_blocks) != 1:
        return _ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text="run_workflow must be the only tool call in the turn",
            valid=False,
        )
    args = dict(workflow_blocks[0].arguments)
    ok, script_or_error = validate_required_string(
        args.get("script"), "script", _WORKFLOW_MAX_SCRIPT_LEN
    )
    if not ok:
        assert isinstance(script_or_error, str)
        return _ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=script_or_error,
            valid=False,
        )
    assert isinstance(script_or_error, str)
    # issue 03: deterministic-sandbox AST guard runs HERE (startup
    # / translation time) — a non-deterministic or malformed script is rejected
    # before any orchestration subtask is created, so a bad workflow never leaves
    # a half-run subtask behind. The model gets a recoverable ack pointing at the
    # offending line and may retry.
    script_error = check_workflow_script(script_or_error)
    if script_error is not None:
        return _ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=script_error,
            valid=False,
        )
    raw_args = args.get("args")
    workflow_args = dict(raw_args) if isinstance(raw_args, dict) else {}
    return SpawnSubtaskDecision(
        agent_name=WORKFLOW_AGENT_NAME,
        goal=_WORKFLOW_GOAL,
        inputs={"script": script_or_error, "args": workflow_args},
        assistant_message=assistant_message,
        assistant_thinking=assistant_thinking,
    )


# ===========================================================================
# structured_output — per-helper structured return
# ===========================================================================

#: Model-visible **control** tool name a workflow helper subtask uses to return
#: a structured (JSON-Schema-shaped) result. Injected ONLY into the helper
#: subtask whose ``agent(goal, schema=...)`` declared a schema;
#: the orchestration interpreter's ``StructuredOutputPolicy`` wrapper intercepts
#: the call and finishes that helper with the call's arguments. Distinct from
#: the session-level ``output_schema`` (top-level final-answer shape).
STRUCTURED_OUTPUT_TOOL = "structured_output"


def structured_output_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Provider-visible schema for :data:`STRUCTURED_OUTPUT_TOOL`.

    ``schema`` is the caller-supplied JSON Schema used verbatim as the tool's
    ``parameters`` — so the model's call arguments ARE the structured result.
    A **control** tool: never registered in the ToolRuntime; the helper's
    ``StructuredOutputPolicy`` wrapper turns a call into the helper's final
    answer. Added to the helper's ``control_action_schemas`` only when its
    ``agent()`` declared a schema (per-helper, opt-in)."""
    return {
        "type": "function",
        "function": {
            "name": STRUCTURED_OUTPUT_TOOL,
            "description": (
                "Provide your final answer as a structured object matching the "
                "required JSON schema. Call this exactly once when you are done."
            ),
            "parameters": dict(schema),
        },
    }
