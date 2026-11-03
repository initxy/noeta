"""A call refused before its tool did anything, as Claude Code words it: the
``<tool_use_error>`` frame, and its ``InputValidationError`` check of the
arguments against the tool's input schema (run for every tool that does not
set ``unchecked_input``, before any permission check).

Claude Code's schemas accept a number or boolean written as a string, and an
array or object written as JSON text (its coercions); so does this check. A
``null`` for an optional argument counts as leaving it out: models outside
Anthropic's send one for arguments they skip.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

__all__ = ["NO_SUCH_TOOL", "input_problems", "tool_use_error"]

#: An unknown tool's result (Claude Code's words; the frame added).
NO_SUCH_TOOL = "Error: No such tool available: {name}"
#: Problems of other kinds listed at most (Claude Code's 20).
MAX_OTHER = 20
_TYPES = {"string": "string", "number": "number", "integer": "number", "boolean": "boolean",
          "array": "array", "object": "object", "null": "null"}


def tool_use_error(message: str) -> str:
    """``message`` as the result of a call refused before it ran."""
    return f"<tool_use_error>{message}</tool_use_error>"


def input_problems(name: str, schema: Any, value: Any) -> str | None:
    """Why ``value`` does not fit the input ``schema`` of the tool ``name``,
    in Claude Code's ``InputValidationError`` wording; ``None`` when it fits."""
    if not isinstance(schema, Mapping):
        return None
    missing: list[str] = []
    unexpected: list[str] = []
    wrong: list[str] = []
    other: list[str] = []
    _check(value, schema, [], missing, unexpected, wrong, other)
    lines = [*(f"The required parameter `{p}` is missing" for p in missing),
             *(f"An unexpected parameter `{p}` was provided" for p in unexpected), *wrong]
    if not lines and not other:
        return None
    lines += other[:MAX_OTHER]
    if len(other) > MAX_OTHER:
        more = len(other) - MAX_OTHER
        lines.append(f"({more} more {'issue' if more == 1 else 'issues'} not listed)")
    head = "issues" if len(lines) > 1 else "issue"
    return f"InputValidationError: {name} failed due to the following {head}:\n" \
        + "\n".join(lines)


def _path(parts: list[str | int]) -> str:
    out = ""
    for i, p in enumerate(parts):
        out += f"[{p}]" if isinstance(p, int) else (p if i == 0 else f".{p}")
    return out


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return "unknown"


def _coerced(value: Any, want: str) -> tuple[bool, Any]:
    """``(fits, value as the check reads it)`` for one declared type."""
    if want == "integer":
        ok, v = _coerced(value, "number")
        return (ok and float(v).is_integer(), v)
    if want == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return (math.isfinite(value), value)
        if isinstance(value, str):
            try:
                v = float(value.strip())
            except ValueError:
                return (False, value)
            return (math.isfinite(v) and value.strip() != "", v)
        return (False, value)
    if want == "boolean":
        if isinstance(value, bool):
            return (True, value)
        return (value in ("true", "false"), value == "true")
    if want in ("array", "object") and isinstance(value, str):
        try:
            v = json.loads(value)
        except ValueError:
            return (False, value)
        return (_kind(v) == want, v)
    return (_kind(value) == want, value)


def _check(value: Any, schema: Mapping[str, Any], at: list[str | int], missing: list[str],
           unexpected: list[str], wrong: list[str], other: list[str]) -> None:
    declared = schema.get("type")
    types = declared if isinstance(declared, list) else [declared] if declared else []
    if types:
        for t in types:
            fits, value = _coerced(value, t)
            if fits:
                break
        else:
            expected = _TYPES.get(str(types[0]), str(types[0]))
            wrong.append(f"The parameter `{_path(at)}` type is expected as `{expected}` but "
                         f"provided as `{_kind(value)}`")
            return
    if "enum" in schema and isinstance(schema["enum"], list) and value not in schema["enum"]:
        options = "|".join(json.dumps(o) for o in schema["enum"])
        where = "The input" if not at else f"The parameter `{_path(at)}`"
        other.append(f"{where} is invalid: Invalid option: expected one of {options}")
        return
    if isinstance(value, Mapping):
        props = schema.get("properties")
        props = props if isinstance(props, Mapping) else {}
        listed = schema.get("required")
        required: list[Any] = listed if isinstance(listed, list) else []
        missing += [_path([*at, key]) for key in required if key not in value]
        if schema.get("additionalProperties") is False:
            unexpected += [_path([*at, k]) for k in value if k not in props]
        for key, sub in props.items():
            # A null for an optional argument counts as leaving it out.
            if key in value and (value[key] is not None or key in required) \
                    and isinstance(sub, Mapping):
                _check(value[key], sub, [*at, key], missing, unexpected, wrong, other)
    elif isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for i, item in enumerate(value):
            _check(item, schema["items"], [*at, i], missing, unexpected, wrong, other)
