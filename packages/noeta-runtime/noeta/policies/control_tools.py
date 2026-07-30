"""Compatibility re-export shim — see :mod:`noeta.policies.control_semantics`.

ADR deepening (C04 control-semantics): each control tool's schema +
validator + codec + response→Decision translation once lived collocated, one
per-tool section, in :mod:`noeta.policies.control_semantics`. This module's
historical role was the schema/validator/codec **half** of that story; it
re-exports the schema/validator/codec names that STILL live kernel-side so
every ``from noeta.policies.control_tools import ...`` call site keeps working.

Control-tool-surface S2 emptied the ``todo_write`` / ``ask_user_question`` /
``spawn_subagent`` bodies out of the kernel into their built-ins
(``noeta.builtins.{todo_write,ask_user_question,delegation}``), so their
schema / validator / codec names no longer resolve here and are no longer
re-exported. What remains is the kernel residue: the ``skill`` /
``run_workflow`` / ``structured_output`` schema surface (still kernel-resident
until S2b), the reserved ``spawn_subagent`` tool NAME (the subtask drain routes
on it), the reserved ``run_workflow`` / ``__workflow__`` vocabulary the drain +
resolver route on, and the neutral ``validate_required_string`` helper. This
shim is deleted wholesale in S2b (D9).
"""

from __future__ import annotations

from noeta.policies.control_semantics import (
    RUN_WORKFLOW_TOOL,
    SKILL_TOOL,
    SPAWN_SUBAGENT_TOOL,
    STRUCTURED_OUTPUT_TOOL,
    WORKFLOW_AGENT_NAME,
    run_workflow_tool_schema,
    skill_tool_schema,
    structured_output_tool_schema,
    validate_required_string,
)


__all__ = [
    # string validator (plan removed; skill still uses validate_required_string)
    "validate_required_string",
    # skill
    "SKILL_TOOL",
    "skill_tool_schema",
    # spawn_subagent — the reserved tool NAME stays kernel-side (the drain routes
    # on it); the schema moved into the ``delegation`` built-in.
    "SPAWN_SUBAGENT_TOOL",
    # run_workflow
    "RUN_WORKFLOW_TOOL",
    "WORKFLOW_AGENT_NAME",
    "run_workflow_tool_schema",
    # structured_output
    "STRUCTURED_OUTPUT_TOOL",
    "structured_output_tool_schema",
]
