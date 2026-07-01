"""Compatibility re-export shim — see :mod:`noeta.policies.control_semantics`.

ADR deepening (C04 control-semantics): each control tool's schema +
validator + codec + response→Decision translation now live collocated, one
per-tool section, in :mod:`noeta.policies.control_semantics`. This module's
historical role was the schema/validator/codec **half** of that story (the
other half — the ``_maybe_*`` translation seam — lived in
``_control_translate``); the two were merged so reading one control tool no
longer means jumping between two files.

This thin module re-exports the schema/validator/codec names it always
exported so every ``from noeta.policies.control_tools import ...`` call site
keeps working unchanged. No behavior, bytes, or public names changed — only
the home of the implementation moved.
"""

from __future__ import annotations

from noeta.policies.control_semantics import (
    ASK_USER_QUESTION_TOOL,
    AnswerValidationError,
    QUESTION_BODY_MEDIA_TYPE,
    QUESTION_HANDLE_PREFIX,
    QuestionDecodeError,
    RUN_WORKFLOW_TOOL,
    SKILL_TOOL,
    STRUCTURED_OUTPUT_TOOL,
    TODO_WRITE_STATUSES,
    TODO_WRITE_TOOL,
    WORKFLOW_AGENT_NAME,
    ask_user_question_tool_schema,
    is_question_id,
    load_answers_body,
    load_questions_body,
    normalize_answer_document,
    put_answers_body,
    put_questions_body,
    question_handle,
    question_id_from_handle,
    run_workflow_tool_schema,
    skill_tool_schema,
    structured_output_tool_schema,
    todo_write_tool_schema,
    validate_call_id,
    validate_question_arguments,
    validate_required_string,
    validate_todos,
)


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
    # run_workflow
    "RUN_WORKFLOW_TOOL",
    "WORKFLOW_AGENT_NAME",
    "run_workflow_tool_schema",
    # structured_output
    "STRUCTURED_OUTPUT_TOOL",
    "structured_output_tool_schema",
]
