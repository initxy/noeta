"""A JSON Schema check of a value: the keywords a structured answer's schema
uses (types, objects, arrays, strings, numbers, ``enum`` / ``const``, the
``anyOf`` / ``oneOf`` / ``allOf`` / ``not`` combinators and local ``$ref``).
A keyword it does not know is not checked, and ``format`` is not checked
(as Claude Code compiles its schemas with formats off).

Each problem reads ``<where>: <what>``, ``where`` being ``root`` or a JSON
pointer into the value (``/items/0/name``)."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

__all__ = ["schema_errors", "schema_problem"]

_TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")
#: Problems reported at most; a value far off its schema would list every leaf.
MAX_PROBLEMS = 20


def schema_problem(schema: Any) -> str | None:
    """Why ``schema`` cannot be used as a schema (``None``: it can)."""
    if not isinstance(schema, Mapping):
        return "a schema is a JSON object"
    declared = schema.get("type")
    names = declared if isinstance(declared, list) else [declared]
    unknown = [n for n in names if n is not None and n not in _TYPES]
    if unknown:
        return f"unknown type {unknown[0]!r}"
    for key in ("properties", "$defs", "definitions", "patternProperties"):
        if key in schema and not isinstance(schema[key], Mapping):
            return f"{key} must be an object"
    if "required" in schema and not (isinstance(schema["required"], list) and all(
            isinstance(r, str) for r in schema["required"])):
        return "required must be a list of property names"
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema and not (isinstance(schema[key], list) and schema[key]):
            return f"{key} must be a non-empty list of schemas"
    if "pattern" in schema:
        try:
            re.compile(str(schema["pattern"]))
        except re.error as exc:
            return f"pattern is not a regular expression: {exc}"
    return None


def schema_errors(value: Any, schema: Any) -> list[str]:
    """What keeps ``value`` from matching ``schema`` (empty: it matches)."""
    out: list[str] = []
    _check(value, schema, "", schema, out)
    return out[:MAX_PROBLEMS]


def _where(path: str) -> str:
    return path or "root"


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "integer" if value.is_integer() else "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def _is(value: Any, name: str) -> bool:
    actual = _type_of(value)
    return actual == name or (name == "number" and actual == "integer")


def _resolve(ref: str, root: Any) -> Any:
    if not ref.startswith("#"):
        return None
    node = root
    for part in ref[1:].split("/")[1:] if ref != "#" else []:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return bool(a == b)


def _check(value: Any, schema: Any, path: str, root: Any, out: list[str],
           depth: int = 0) -> None:
    if len(out) >= MAX_PROBLEMS or depth > 64:
        return
    if schema is True or schema is None:
        return
    if schema is False:
        out.append(f"{_where(path)}: no value is allowed here")
        return
    if not isinstance(schema, Mapping):
        return
    if isinstance(schema.get("$ref"), str):
        target = _resolve(schema["$ref"], root)
        if target is None:
            out.append(f"{_where(path)}: the schema's reference {schema['$ref']!r} "
                       "cannot be resolved")
            return
        _check(value, target, path, root, out, depth + 1)
    where = _where(path)
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_is(value, n) for n in names):
            out.append(f"{where}: must be {' or '.join(map(str, names))}")
            return
    if "enum" in schema and isinstance(schema["enum"], list) \
            and not any(_equal(value, e) for e in schema["enum"]):
        out.append(f"{where}: must be one of the allowed values {schema['enum']!r}")
    if "const" in schema and not _equal(value, schema["const"]):
        out.append(f"{where}: must be equal to {schema['const']!r}")
    _combinators(value, schema, path, root, out, depth)
    if isinstance(value, Mapping):
        _object(value, schema, path, root, out, depth)
    elif isinstance(value, list):
        _array(value, schema, path, root, out, depth)
    elif isinstance(value, str):
        _string(value, schema, where, out)
    elif _is(value, "number"):
        _number(value, schema, where, out)


def _combinators(value: Any, schema: Mapping[str, Any], path: str, root: Any,
                 out: list[str], depth: int) -> None:
    where = _where(path)
    for sub in schema.get("allOf") or []:
        _check(value, sub, path, root, out, depth + 1)
    if isinstance(schema.get("anyOf"), list) and schema["anyOf"] and not any(
            not _sub_errors(value, s, path, root, depth) for s in schema["anyOf"]):
        out.append(f"{where}: must match a schema in anyOf")
    if isinstance(schema.get("oneOf"), list) and schema["oneOf"]:
        matched = sum(1 for s in schema["oneOf"] if not _sub_errors(value, s, path, root, depth))
        if matched != 1:
            out.append(f"{where}: must match exactly one schema in oneOf (matched {matched})")
    if "not" in schema and not _sub_errors(value, schema["not"], path, root, depth):
        out.append(f"{where}: must not match the schema in not")


def _sub_errors(value: Any, schema: Any, path: str, root: Any, depth: int) -> list[str]:
    found: list[str] = []
    _check(value, schema, path, root, found, depth + 1)
    return found


def _object(value: Mapping[str, Any], schema: Mapping[str, Any], path: str, root: Any,
            out: list[str], depth: int) -> None:
    where = _where(path)
    for name in schema.get("required") or []:
        if isinstance(name, str) and name not in value:
            out.append(f"{where}: must have required property {name!r}")
    props: Mapping[str, Any] = schema["properties"] \
        if isinstance(schema.get("properties"), Mapping) else {}
    patterns: Mapping[str, Any] = schema["patternProperties"] \
        if isinstance(schema.get("patternProperties"), Mapping) else {}
    extra = schema.get("additionalProperties", True)
    for key, item in value.items():
        child = f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}"
        matched = False
        if key in props:
            matched = True
            _check(item, props[key], child, root, out, depth + 1)
        for pattern, sub in patterns.items():
            if re.search(pattern, str(key)):
                matched = True
                _check(item, sub, child, root, out, depth + 1)
        if not matched:
            if extra is False:
                out.append(f"{where}: must not have additional property {key!r}")
            elif isinstance(extra, Mapping):
                _check(item, extra, child, root, out, depth + 1)
    if isinstance(schema.get("minProperties"), int) and len(value) < schema["minProperties"]:
        out.append(f"{where}: must have at least {schema['minProperties']} properties")
    if isinstance(schema.get("maxProperties"), int) and len(value) > schema["maxProperties"]:
        out.append(f"{where}: must have at most {schema['maxProperties']} properties")


def _array(value: list[Any], schema: Mapping[str, Any], path: str, root: Any,
           out: list[str], depth: int) -> None:
    where = _where(path)
    prefix: list[Any] = schema["prefixItems"] \
        if isinstance(schema.get("prefixItems"), list) else []
    items = schema.get("items")
    if isinstance(items, list):  # the older tuple form
        prefix, items = items, schema.get("additionalItems", True)
    for i, item in enumerate(value):
        sub = prefix[i] if i < len(prefix) else items
        if sub is False:
            out.append(f"{where}: must have at most {len(prefix)} items")
            break
        _check(item, sub, f"{path}/{i}", root, out, depth + 1)
    if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
        out.append(f"{where}: must have at least {schema['minItems']} items")
    if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
        out.append(f"{where}: must have at most {schema['maxItems']} items")
    if schema.get("uniqueItems") is True:
        for i, a in enumerate(value):
            if any(_equal(a, b) for b in value[:i]):
                out.append(f"{where}: must not have duplicate items (item {i})")
                break
    if "contains" in schema and not any(
            not _sub_errors(v, schema["contains"], path, root, depth) for v in value):
        out.append(f"{where}: must contain an item matching the schema in contains")


def _string(value: str, schema: Mapping[str, Any], where: str, out: list[str]) -> None:
    if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
        out.append(f"{where}: must have at least {schema['minLength']} characters")
    if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
        out.append(f"{where}: must have at most {schema['maxLength']} characters")
    if isinstance(schema.get("pattern"), str):
        try:
            if re.search(schema["pattern"], value) is None:
                out.append(f"{where}: must match pattern {schema['pattern']!r}")
        except re.error:
            pass


def _number(value: float, schema: Mapping[str, Any], where: str, out: list[str]) -> None:
    def bound(key: str) -> float | None:
        b = schema.get(key)
        return b if isinstance(b, (int, float)) and not isinstance(b, bool) else None

    if (b := bound("minimum")) is not None and value < b:
        out.append(f"{where}: must be >= {b}")
    if (b := bound("maximum")) is not None and value > b:
        out.append(f"{where}: must be <= {b}")
    if (b := bound("exclusiveMinimum")) is not None and value <= b:
        out.append(f"{where}: must be > {b}")
    if (b := bound("exclusiveMaximum")) is not None and value >= b:
        out.append(f"{where}: must be < {b}")
    if (b := bound("multipleOf")) is not None and b > 0:
        ratio = value / b
        if not math.isclose(ratio, round(ratio), rel_tol=0, abs_tol=1e-6):
            out.append(f"{where}: must be a multiple of {b}")
