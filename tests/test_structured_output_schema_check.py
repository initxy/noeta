"""The ``structured_output`` payload check — what it catches, and what it won't.

The checker exists because a provider's tool ``parameters`` only steer a model;
the ``agent(goal, schema=...)`` caller parses the returned object for real. It
is deliberately a *floor*, not a JSON Schema implementation, so both halves of
that contract are pinned here: the violations it must report, and the
constructs it must stay silent about rather than guess at. A false rejection
costs a real task a turn, so silence is the safe direction and is tested as
carefully as detection.
"""

from __future__ import annotations

from noeta.builtins.react.impl.schema_check import (
    MAX_REPORTED_VIOLATIONS,
    validate_structured_payload,
)


def _joined(payload: object, schema: object) -> str:
    return "\n".join(validate_structured_payload(payload, schema))


# ---------------------------------------------------------------------------
# What it catches
# ---------------------------------------------------------------------------


def test_conforming_payload_has_no_violations() -> None:
    schema = {
        "type": "object",
        "required": ["title", "count"],
        "properties": {"title": {"type": "string"}, "count": {"type": "integer"}},
    }
    assert validate_structured_payload({"title": "a", "count": 1}, schema) == ()


def test_missing_required_property_is_named_by_path() -> None:
    schema = {"type": "object", "required": ["title"], "properties": {}}
    out = _joined({"other": 1}, schema)
    assert "$.title" in out and "required property is missing" in out


def test_root_type_mismatch() -> None:
    out = _joined(["not", "an", "object"], {"type": "object"})
    assert "$: expected type object, got array" in out


def test_nested_type_mismatch_keeps_the_path() -> None:
    schema = {
        "type": "object",
        "properties": {
            "inner": {"type": "object", "properties": {"n": {"type": "integer"}}}
        },
    }
    out = _joined({"inner": {"n": "twelve"}}, schema)
    assert "$.inner.n: expected type integer, got string" in out


def test_boolean_is_not_an_integer() -> None:
    # ``isinstance(True, int)`` is true in Python; the checker must not inherit
    # that, or a bool silently satisfies an integer field.
    out = _joined({"n": True}, {"type": "object", "properties": {"n": {"type": "integer"}}})
    assert "$.n: expected type integer, got boolean" in out


def test_integer_satisfies_number_but_not_the_other_way() -> None:
    number = {"type": "object", "properties": {"n": {"type": "number"}}}
    integer = {"type": "object", "properties": {"n": {"type": "integer"}}}
    assert validate_structured_payload({"n": 3}, number) == ()
    assert validate_structured_payload({"n": 3.5}, number) == ()
    assert "expected type integer, got number" in _joined({"n": 3.5}, integer)


def test_null_and_type_unions() -> None:
    schema = {"type": "object", "properties": {"n": {"type": ["string", "null"]}}}
    assert validate_structured_payload({"n": None}, schema) == ()
    assert validate_structured_payload({"n": "x"}, schema) == ()
    assert "expected type string or null, got integer" in _joined({"n": 1}, schema)


def test_enum_violation() -> None:
    schema = {"type": "object", "properties": {"s": {"enum": ["a", "b"]}}}
    assert validate_structured_payload({"s": "a"}, schema) == ()
    assert "is not one of the allowed values" in _joined({"s": "c"}, schema)


def test_additional_properties_false_rejects_extras() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": False,
    }
    assert validate_structured_payload({"a": "x"}, schema) == ()
    assert "$.b: property is not allowed" in _joined({"a": "x", "b": 1}, schema)


def test_array_items_are_checked_with_the_index_in_the_path() -> None:
    schema = {
        "type": "object",
        "properties": {"xs": {"type": "array", "items": {"type": "integer"}}},
    }
    assert validate_structured_payload({"xs": [1, 2]}, schema) == ()
    assert "$.xs[1]: expected type integer, got string" in _joined({"xs": [1, "b"]}, schema)


def test_tuple_form_items() -> None:
    schema = {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]}
    assert validate_structured_payload(["a", 1], schema) == ()
    assert "$[1]: expected type integer, got string" in _joined(["a", "b"], schema)


# ---------------------------------------------------------------------------
# What it deliberately stays silent about
# ---------------------------------------------------------------------------


def test_combinator_and_ref_subtrees_are_skipped_not_guessed() -> None:
    # Descending into one branch would produce confident errors about a branch
    # the payload never claimed. Skipping is the conservative direction.
    for schema in (
        {"anyOf": [{"type": "object"}, {"type": "string"}]},
        {"oneOf": [{"type": "integer"}]},
        {"allOf": [{"type": "object"}]},
        {"not": {"type": "string"}},
        {"$ref": "#/$defs/Thing"},
    ):
        assert validate_structured_payload(12345, schema) == (), schema


def test_a_ref_property_is_skipped_while_required_still_applies() -> None:
    schema = {
        "type": "object",
        "required": ["x", "y"],
        "properties": {"x": {"$ref": "#/$defs/T"}},
    }
    out = validate_structured_payload({"x": "anything at all"}, schema)
    assert out == ("$.y: required property is missing",)


def test_unmodelled_keywords_and_types_are_ignored() -> None:
    # Numeric bounds / string formats / pattern properties are not implemented;
    # a payload that violates them is accepted rather than falsely rejected.
    schema = {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "minimum": 10, "maximum": 20},
            "s": {"type": "string", "format": "email", "pattern": "^a"},
        },
    }
    assert validate_structured_payload({"n": 1, "s": "zzz"}, schema) == ()
    # An unknown ``type`` name cannot produce a violation either.
    assert validate_structured_payload({"n": 1}, {"type": "widget"}) == ()


def test_a_non_dict_schema_yields_nothing() -> None:
    assert validate_structured_payload({"a": 1}, None) == ()
    assert validate_structured_payload({"a": 1}, True) == ()
    assert validate_structured_payload({"a": 1}, {}) == ()


def test_a_mismatched_container_does_not_restate_itself_field_by_field() -> None:
    # One clear error beats N derived ones: once the value is the wrong shape,
    # per-property complaints add noise the model cannot act on.
    schema = {
        "type": "object",
        "required": ["a", "b", "c"],
        "properties": {"a": {"type": "string"}},
    }
    assert validate_structured_payload("a string", schema) == (
        "$: expected type object, got string",
    )


# ---------------------------------------------------------------------------
# Bounded and deterministic — the report lands in the recorded conversation
# ---------------------------------------------------------------------------


def test_report_is_capped() -> None:
    names = [f"f{i}" for i in range(MAX_REPORTED_VIOLATIONS * 3)]
    schema = {"type": "object", "required": names, "properties": {}}
    assert len(validate_structured_payload({}, schema)) == MAX_REPORTED_VIOLATIONS


def test_order_follows_declaration_not_payload_order() -> None:
    schema = {
        "type": "object",
        "required": ["b", "a"],
        "properties": {"z": {"type": "string"}, "y": {"type": "string"}},
    }
    payload = {"y": 1, "z": 2}
    expected = (
        "$.b: required property is missing",
        "$.a: required property is missing",
        "$.z: expected type string, got integer",
        "$.y: expected type string, got integer",
    )
    assert validate_structured_payload(payload, schema) == expected
    # Same inputs, same bytes — the text is recorded, so a re-run must match.
    assert validate_structured_payload(dict(payload), schema) == expected
