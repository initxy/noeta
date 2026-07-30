"""Every control tool carries a non-empty, externalized function-level
description.

The composer renders a control tool's schema verbatim into
``View.provider_tool_schemas``; unlike executable tools there is no ``Tool``
dataclass to hang a ``description`` field on, so the description must be set
directly in each ``*_tool_schema()`` function — and sourced from a sibling
``<name>.md`` resource rather than a Python string literal. This guard fails
loudly if a new control tool ships without a function-level description or
hard-codes it inline instead of externalizing it.

Control-tool-surface S2/S2b moved every control tool into a built-in (schema +
``.md`` collocated beside the impl, loaded via the shared L0 ``load_markdown``):
``todo_write`` / ``ask_user_question`` / ``spawn_subagent`` into their own
built-ins; ``skill`` into ``skills``; ``run_workflow`` into ``react``. The kernel
``policies`` band keeps NO control-tool description any more. Each schema's
description must still equal its own externalized ``.md`` resource, wherever it
now lives.
"""
from __future__ import annotations

from typing import Any, Callable

from noeta.builtins.ask_user_question.impl import ask_user_question_tool_schema
from noeta.builtins.delegation.impl import spawn_subagent_tool_schema
from noeta.builtins.react.impl import run_workflow_tool_schema
from noeta.builtins.skills.impl import SKILL_TOOL, skill_tool_schema
from noeta.builtins.todo_write.impl import todo_write_tool_schema
from noeta.policies.control_semantics import RUN_WORKFLOW_TOOL
from noeta.protocols.resources import load_markdown

# (tool name, freshly built schema, its externalized-description loader) —
# roster-taking tools get an empty roster so we exercise the no-roster shape;
# the function-level description must be present regardless of roster contents.
_CONTROL_SCHEMAS: dict[str, tuple[dict[str, Any], Callable[[], str]]] = {
    "todo_write": (
        todo_write_tool_schema(),
        lambda: load_markdown("noeta.builtins.todo_write.impl", "todo_write"),
    ),
    "ask_user_question": (
        ask_user_question_tool_schema(),
        lambda: load_markdown(
            "noeta.builtins.ask_user_question.impl", "ask_user_question"
        ),
    ),
    "spawn_subagent": (
        spawn_subagent_tool_schema(),
        lambda: load_markdown(
            "noeta.builtins.delegation.impl", "spawn_subagent"
        ),
    ),
    SKILL_TOOL: (
        skill_tool_schema(),
        lambda: load_markdown("noeta.builtins.skills.impl", "skill"),
    ),
    RUN_WORKFLOW_TOOL: (
        run_workflow_tool_schema(),
        lambda: load_markdown("noeta.builtins.react.impl", "run_workflow"),
    ),
}


def test_every_control_tool_has_nonempty_function_description() -> None:
    for name, (schema, _loader) in _CONTROL_SCHEMAS.items():
        function = schema["function"]
        assert "description" in function, f"{name}: missing function description"
        assert function["description"].strip(), f"{name}: empty description"


def test_control_tool_description_is_externalized_to_markdown() -> None:
    """The schema's description must equal the ``<name>.md`` resource — i.e. be
    sourced from the file, not a divergent inline string."""
    for name, (schema, loader) in _CONTROL_SCHEMAS.items():
        assert schema["function"]["description"] == loader(), (
            f"{name}: schema description diverges from its .md resource"
        )
