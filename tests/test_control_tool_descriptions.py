"""Every control tool carries a non-empty, externalized function-level
description.

The composer renders a control tool's schema verbatim into
``View.provider_tool_schemas``; unlike executable tools there is no ``Tool``
dataclass to hang a ``description`` field on, so the description must be set
directly in each ``*_tool_schema()`` function — and sourced
from a sibling ``policies/descriptions/<name>.md`` resource rather than a Python
string literal. This guard fails loudly if a new control tool ships without a
function-level description or hard-codes it inline instead of externalizing it.
"""
from __future__ import annotations

from noeta.policies._control_translate import (
    SPAWN_SUBAGENT_TOOL,
    spawn_subagent_tool_schema,
)
from noeta.policies.control_tools import (
    ASK_USER_QUESTION_TOOL,
    RUN_WORKFLOW_TOOL,
    SKILL_TOOL,
    TODO_WRITE_TOOL,
    ask_user_question_tool_schema,
    run_workflow_tool_schema,
    skill_tool_schema,
    todo_write_tool_schema,
)
from noeta.policies.descriptions import load_control_tool_description

# (tool name, freshly built schema) — roster-taking tools get an empty roster
# so we exercise the no-roster shape; the function-level description must be
# present regardless of roster contents.
_CONTROL_SCHEMAS = {
    TODO_WRITE_TOOL: todo_write_tool_schema(),
    ASK_USER_QUESTION_TOOL: ask_user_question_tool_schema(),
    SKILL_TOOL: skill_tool_schema(),
    RUN_WORKFLOW_TOOL: run_workflow_tool_schema(),
    SPAWN_SUBAGENT_TOOL: spawn_subagent_tool_schema(),
}


def test_every_control_tool_has_nonempty_function_description() -> None:
    for name, schema in _CONTROL_SCHEMAS.items():
        function = schema["function"]
        assert "description" in function, f"{name}: missing function description"
        assert function["description"].strip(), f"{name}: empty description"


def test_control_tool_description_is_externalized_to_markdown() -> None:
    """The schema's description must equal the ``<name>.md`` resource — i.e. be
    sourced from the file, not a divergent inline string."""
    for name, schema in _CONTROL_SCHEMAS.items():
        assert schema["function"]["description"] == load_control_tool_description(
            name
        ), f"{name}: schema description diverges from its .md resource"
