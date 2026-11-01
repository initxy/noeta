"""Structured output as the ``structured_output`` plugin records it, for the
others that read it (the workflow plugin, the headless host): a task whose
config sets ``output_schema`` answers through the ``StructuredOutput`` tool,
and each accepted call is recorded as ``("structured_output", "answer",
{"value", "call_id"})``, in the same append as the call's result."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from noeta.core.events import ExtensionRecorded, Recorded
from noeta.toolkit.json_schema import schema_problem

__all__ = ["ANSWER_KIND", "PLUGIN", "RETRIES_REASON", "SCHEMA_KEY", "TOOL", "answer_schema_problem",
           "structured_answer"]

PLUGIN = "structured_output"
ANSWER_KIND = "answer"
#: The task config key holding the JSON Schema of the answer.
SCHEMA_KEY = "output_schema"
#: The tool's model-visible name (Claude Code's).
TOOL = "StructuredOutput"
#: The reason a turn fails with after too many refused calls (the headless
#: host reports it as Claude Code's ``error_max_structured_output_retries``).
RETRIES_REASON = "max_structured_output_retries"


def answer_schema_problem(schema: Any) -> str | None:
    """Why ``schema`` cannot be an answer's schema (``None``: it can): it
    becomes a tool's input schema, which a provider takes only with an
    ``object`` root (Anthropic refuses every request otherwise)."""
    problem = schema_problem(schema)
    if problem is None and schema.get("type") != "object":
        problem = ('the root must declare "type": "object" (a tool\'s input is an object; '
                   "wrap another value in a property)")
    return problem


def structured_answer(records: Iterable[Recorded], after: int = 0) -> Any:
    """The value of the latest accepted ``StructuredOutput`` call among
    ``records`` after seq ``after`` (``None`` when there is none)."""
    value: Any = None
    for rec in records:
        ev = rec.event
        if rec.seq > after and isinstance(ev, ExtensionRecorded) and ev.extension == PLUGIN \
                and ev.kind == ANSWER_KIND and isinstance(ev.data, dict):
            value = ev.data.get("value")
    return value
