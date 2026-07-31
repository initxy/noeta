"""Conservative JSON-Schema check for a ``structured_output`` payload.

``structured_output`` is a **contract with a program**, not with a reader: the
caller of ``agent(goal, schema=...)`` parses the returned object by the shape
its schema declared. The provider-side tool ``parameters`` are a soft steer for
the model, not an enforced constraint, so a payload that misses a required
field or gets a type wrong would otherwise be accepted verbatim and blow up far
from its cause — a downstream ``KeyError`` against a ledger that says the task
*succeeded*. :func:`validate_structured_payload` is the gate that turns that
into a retry the model can act on.

**Deliberately conservative — it reports only violations it is certain of.**
A keyword it does not implement (``$ref``, ``anyOf`` / ``oneOf`` / ``allOf``,
``not``, ``patternProperties``, ``format``, numeric bounds, …) is *ignored*,
never treated as a failure. The error direction matters: a false rejection
costs the model a turn and can fail an otherwise-correct task, while a missed
violation only leaves today's behaviour in place. So the checker is a floor on
correctness, not a complete JSON Schema implementation — and no dependency
(``noeta-runtime`` declares none, ``noeta-sdk`` only what its transports need).

Determinism is a hard requirement, not a nicety: the messages come back as a
recorded rejection in the task's event stream, so the same payload and schema
must always produce byte-identical text. Every traversal here walks declaration
order (``properties`` insertion order, ``required`` list order); nothing
iterates a set, and the report is truncated at a fixed count.
"""

from __future__ import annotations

from typing import Any


__all__ = ["validate_structured_payload", "MAX_REPORTED_VIOLATIONS"]


#: Cap on reported violations. A payload that misses twenty fields does not
#: need twenty lines to be fixed — the first few name the shape well enough,
#: and a bounded report keeps the rejection message a stable, small prompt.
MAX_REPORTED_VIOLATIONS = 8


#: JSON Schema ``type`` → the Python types it accepts. ``bool`` is checked
#: before ``int`` everywhere because ``isinstance(True, int)`` is true.
_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate_structured_payload(
    payload: Any, schema: Any
) -> tuple[str, ...]:
    """Check ``payload`` against ``schema``; return the violations found.

    An empty tuple means "nothing certainly wrong" — which, given the
    conservative contract above, is what the caller treats as valid. Each entry
    is one human-readable line naming the JSON path and the problem, ready to
    hand back to the model verbatim.
    """
    violations: list[str] = []
    _check(payload, schema, "$", violations)
    return tuple(violations[:MAX_REPORTED_VIOLATIONS])


def _check(value: Any, schema: Any, path: str, out: list[str]) -> None:
    """Walk one (value, schema) pair, appending violations to ``out``.

    Recursion stops early once the report is full, so a deeply broken payload
    costs a bounded amount of work.
    """
    if len(out) >= MAX_REPORTED_VIOLATIONS or not isinstance(schema, dict):
        return

    # A schema that offers alternatives (anyOf/oneOf/allOf/$ref/not) is outside
    # what this checker models. Descending into one branch would produce
    # confident errors about a branch the payload never claimed, so the whole
    # subtree is skipped — the conservative direction.
    if any(k in schema for k in ("anyOf", "oneOf", "allOf", "not", "$ref")):
        return

    declared = schema.get("type")
    if not _type_ok(value, declared):
        out.append(f"{path}: expected type {_type_label(declared)}, got {_kind(value)}")
        # The value is not the declared shape, so descending into properties /
        # items would only restate the same fault field by field.
        return

    enum = schema.get("enum")
    if isinstance(enum, list) and enum and value not in enum:
        out.append(f"{path}: value {value!r} is not one of the allowed values")
        return

    if isinstance(value, dict):
        _check_object(value, schema, path, out)
    elif isinstance(value, list):
        _check_array(value, schema, path, out)


def _check_object(value: dict, schema: dict, path: str, out: list[str]) -> None:
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}

    required = schema.get("required")
    if isinstance(required, list):
        for name in required:
            if len(out) >= MAX_REPORTED_VIOLATIONS:
                return
            if isinstance(name, str) and name not in value:
                out.append(f"{_join(path, name)}: required property is missing")

    # ``additionalProperties: false`` is the one form of the keyword with an
    # unambiguous reading; a schema-valued one describes the extras rather than
    # forbidding them, and is left alone.
    if schema.get("additionalProperties") is False and properties:
        for name in value:
            if len(out) >= MAX_REPORTED_VIOLATIONS:
                return
            if name not in properties:
                out.append(
                    f"{_join(path, str(name))}: property is not allowed "
                    "(the schema forbids additional properties)"
                )

    # Declaration order, not payload order — same schema ⇒ same report.
    for name, sub in properties.items():
        if len(out) >= MAX_REPORTED_VIOLATIONS:
            return
        if isinstance(name, str) and name in value:
            _check(value[name], sub, _join(path, name), out)


def _check_array(value: list, schema: dict, path: str, out: list[str]) -> None:
    items = schema.get("items")
    if isinstance(items, dict):
        for index, entry in enumerate(value):
            if len(out) >= MAX_REPORTED_VIOLATIONS:
                return
            _check(entry, items, f"{path}[{index}]", out)
    elif isinstance(items, list):
        # Tuple form: the i-th schema constrains the i-th element; extras are
        # governed by ``additionalItems``, which this checker does not model.
        for index, sub in enumerate(items):
            if len(out) >= MAX_REPORTED_VIOLATIONS:
                return
            if index < len(value):
                _check(value[index], sub, f"{path}[{index}]", out)


def _type_ok(value: Any, declared: Any) -> bool:
    """Whether ``value`` satisfies ``declared``, permissively.

    An absent, unknown, or malformed ``type`` is satisfied — only a type this
    checker recognises can produce a violation.
    """
    if isinstance(declared, str):
        check = _TYPE_CHECKS.get(declared)
        return check is None or check(value)
    if isinstance(declared, list):
        known = [t for t in declared if isinstance(t, str) and t in _TYPE_CHECKS]
        if not known:
            return True
        return any(_TYPE_CHECKS[t](value) for t in known)
    return True


def _type_label(declared: Any) -> str:
    if isinstance(declared, str):
        return declared
    if isinstance(declared, list):
        return " or ".join(str(t) for t in declared)
    return "?"


def _kind(value: Any) -> str:
    """The JSON Schema type name for a value, for the error text."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) or isinstance(value, float):
        return "integer" if isinstance(value, int) else "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _join(path: str, name: str) -> str:
    return f"{path}.{name}"
