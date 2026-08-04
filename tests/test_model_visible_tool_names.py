"""No model-visible text names a deleted tool or an old snake_case tool name.

The 2026-08-03 alignment renamed the provider-visible tool surface to the
Claude Code names (``Read`` / ``Bash`` / ``Task`` / ``AskUserQuestion`` / …)
and deleted the ``Task`` tool's ``spawns`` array. Prose the model reads —
a control tool's ``.md`` description, a validation ack, an error text — is
*not* covered by the schema goldens' byte pins when the same stale words also
sit in the golden, so this module pins the rule directly: model-visible text
uses the model-visible name.

Snake_case is still correct everywhere the model never looks — plugin and
contribution names, capability flags (``ask_user_question``), event
vocabulary, and the ``.md`` resource BASENAMES (``run_workflow.md``,
``spawn_subagent.md``). Only the *strings the model reads* are under test here.
"""

from __future__ import annotations

from typing import Any

from noeta.builtins.ask_user_question.impl import (
    ASK_USER_QUESTION_TOOL,
    ask_user_question_tool_schema,
    translate_ask_user_question,
    validate_call_id,
    validate_question_arguments,
)
from noeta.builtins.react.impl import run_workflow_tool_schema
from noeta.policies.control_semantics import ControlTranslateContext
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.decisions import StatePatchDecision
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
    ToolUseBlock,
)


# ---------------------------------------------------------------------------
# run_workflow: the delegation tool it points at is ``Task``, and the deleted
# ``spawns`` array must not survive anywhere in the description the model reads.
# ---------------------------------------------------------------------------


def _run_workflow_description() -> str:
    """The exact prose the model receives (schema description == the ``.md``;
    ``test_control_tool_descriptions`` pins that equality)."""
    description = run_workflow_tool_schema()["function"]["description"]
    assert isinstance(description, str)
    return description


def test_run_workflow_description_names_no_deleted_delegation_surface() -> None:
    """``spawn_subagent`` and the removed ``spawns`` array are gone from the
    description — both were deleted by the tool-alignment effort, so pointing
    the model at either sends it after a surface that does not exist. (The
    bare verb "spawn" is still legitimate prose about spawning sub-agents;
    only the tool name and the parameter reference are banned.)"""
    description = _run_workflow_description()
    assert "spawn_subagent" not in description
    assert "`spawns`" not in description
    assert "spawns array" not in description


def test_run_workflow_description_points_at_the_task_tool_and_turn_fanout() -> None:
    """The two "when NOT to use" bullets still route the model somewhere: the
    single delegation goes to ``Task``, and the fan-out idiom is several
    ``Task`` calls in ONE assistant turn (the ``spawns`` array's replacement)."""
    description = _run_workflow_description()
    assert "`Task` instead" in description
    assert "`Task` calls in ONE assistant turn" in description


# ---------------------------------------------------------------------------
# AskUserQuestion: every string the model can be handed by this built-in names
# the tool as ``AskUserQuestion``, never as the snake_case activation flag.
# ---------------------------------------------------------------------------


def _only_tool_call_ack_text() -> str:
    """The ack a mixed / repeated ask turn earns — a model-visible error."""
    block = ToolUseBlock(
        call_id="q1", tool_name=ASK_USER_QUESTION_TOOL, arguments={"questions": []}
    )
    other = ToolUseBlock(call_id="t1", tool_name="Read", arguments={"path": "x"})
    response = LLMResponse(stop_reason="tool_use", content=[block, other])
    assistant = Message(role="assistant", content=[TextBlock(text="")])
    decision = translate_ask_user_question(
        ControlTranslateContext(
            response=response,
            assistant_message=assistant,
            assistant_thinking=(),
            content_store=None,
        )
    )
    assert isinstance(decision, StatePatchDecision)
    ack = decision.messages_after[-1]
    return str(ack.content[0].error)


def _validation_error_texts() -> list[str]:
    """Every model-visible refusal the ask validators can produce."""
    texts: list[str] = []
    for bad_call_id in (None, "", "bad id with space", "x" * 65):
        ok, text = validate_call_id(bad_call_id)
        assert not ok
        texts.append(str(text))

    bad_arguments: tuple[Any, ...] = (
        "not-an-object",
        {},
        {"questions": []},
        {"questions": [{}]},
        {"questions": [{"question": "Which?", "header": "h", "multiSelect": False}]},
        {
            "questions": [
                {
                    "question": "Which?",
                    "header": "h",
                    "multiSelect": False,
                    "options": [{"label": "a", "description": "d"}],
                }
            ]
        },
    )
    for arguments in bad_arguments:
        ok, result = validate_question_arguments(arguments)
        assert not ok
        texts.append(str(result))
    return texts


def test_ask_user_question_model_visible_strings_use_the_claude_code_name() -> None:
    """The schema, the "only tool call" ack, and every validation refusal name
    the tool ``AskUserQuestion``; none leaks the snake_case activation flag."""
    model_visible = [
        to_canonical_bytes(ask_user_question_tool_schema()).decode("utf-8"),
        _only_tool_call_ack_text(),
        *_validation_error_texts(),
    ]
    for text in model_visible:
        assert "ask_user_question" not in text, text


def test_ask_user_question_acks_still_name_the_tool() -> None:
    """A negative-only assertion would pass on a text that dropped the name
    entirely, so pin that the acks which name a tool name the right one."""
    assert _only_tool_call_ack_text().startswith(ASK_USER_QUESTION_TOOL)
    named = [t for t in _validation_error_texts() if "call_id" in t]
    assert named
    for text in named:
        assert text.startswith(ASK_USER_QUESTION_TOOL)
