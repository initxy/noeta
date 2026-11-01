"""The JSON Schema check structured answers go through."""

from __future__ import annotations

from typing import Any

import pytest

from noeta.toolkit.json_schema import schema_errors, schema_problem

PERSON = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "pattern": "^[A-Z]"},
        "age": {"type": "integer", "minimum": 0, "exclusiveMaximum": 200},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2,
                 "uniqueItems": True},
        "kind": {"enum": ["a", "b"]},
        "pet": {"$ref": "#/$defs/pet"},
        "score": {"type": ["number", "null"], "multipleOf": 0.5},
    },
    "required": ["name", "age"],
    "additionalProperties": False,
    "$defs": {"pet": {"type": "object", "properties": {"legs": {"const": 4}}}},
}


def test_a_matching_value_has_no_errors() -> None:
    value = {"name": "Ada", "age": 36, "tags": ["x"], "kind": "a", "pet": {"legs": 4},
             "score": 1.5}
    assert schema_errors(value, PERSON) == []
    assert schema_errors({"name": "Ada", "age": 3.0, "score": None}, PERSON) == []


@pytest.mark.parametrize(("value", "problem"), [
    ([], "root: must be object"),
    ({"age": 1}, "root: must have required property 'name'"),
    ({"name": "Ada", "age": 1, "x": 1}, "root: must not have additional property 'x'"),
    ({"name": "ada", "age": 1}, "/name: must match pattern '^[A-Z]'"),
    ({"name": "", "age": 1}, "/name: must have at least 1 characters"),
    ({"name": "A", "age": True}, "/age: must be integer"),
    ({"name": "A", "age": 1.5}, "/age: must be integer"),
    ({"name": "A", "age": -1}, "/age: must be >= 0"),
    ({"name": "A", "age": 200}, "/age: must be < 200"),
    ({"name": "A", "age": 1, "tags": ["a", 1]}, "/tags/1: must be string"),
    ({"name": "A", "age": 1, "tags": ["a", "b", "c"]}, "/tags: must have at most 2 items"),
    ({"name": "A", "age": 1, "tags": ["a", "a"]}, "/tags: must not have duplicate items"),
    ({"name": "A", "age": 1, "kind": "c"}, "/kind: must be one of the allowed values"),
    ({"name": "A", "age": 1, "pet": {"legs": 3}}, "/pet/legs: must be equal to 4"),
    ({"name": "A", "age": 1, "score": 0.3}, "/score: must be a multiple of 0.5"),
])
def test_each_problem_names_where_and_what(value: Any, problem: str) -> None:
    assert any(e.startswith(problem) for e in schema_errors(value, PERSON)), \
        schema_errors(value, PERSON)


def test_combinators() -> None:
    either = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    assert schema_errors("x", either) == [] and schema_errors(1, either) == []
    assert schema_errors(1.5, either) == ["root: must match a schema in anyOf"]
    one = {"oneOf": [{"type": "number"}, {"type": "integer"}]}
    assert schema_errors(1, one) == ["root: must match exactly one schema in oneOf (matched 2)"]
    assert schema_errors(1.5, one) == []
    assert schema_errors(1, {"not": {"type": "integer"}}) == [
        "root: must not match the schema in not"]
    assert schema_errors(1, {"allOf": [{"type": "integer"}, {"minimum": 2}]}) == [
        "root: must be >= 2"]
    assert schema_errors([1, "a"], {"prefixItems": [{"type": "integer"}], "items": False}) == [
        "root: must have at most 1 items"]
    assert schema_errors({"x1": "a"}, {"patternProperties": {"^x": {"type": "integer"}},
                                       "additionalProperties": False}) == [
        "/x1: must be integer"]
    assert schema_errors(1, {"$ref": "#/nowhere"})[0].endswith("cannot be resolved")
    assert schema_errors(1, False) == ["root: no value is allowed here"]
    assert schema_errors({"a": 1}, {"type": "object", "unknownKeyword": 3}) == []


@pytest.mark.parametrize(("schema", "problem"), [
    ({"type": "object"}, None),
    ([], "a schema is a JSON object"),
    ({"type": "thing"}, "unknown type 'thing'"),
    ({"properties": []}, "properties must be an object"),
    ({"required": "a"}, "required must be a list of property names"),
    ({"anyOf": []}, "anyOf must be a non-empty list of schemas"),
    ({"pattern": "("}, "pattern is not a regular expression"),
])
def test_schema_problem(schema: Any, problem: str | None) -> None:
    found = schema_problem(schema)
    assert found == problem if problem is None else (found or "").startswith(problem)
