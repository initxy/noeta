"""The schema check every call passes before any gate, in Claude Code's
``InputValidationError`` words."""

from __future__ import annotations

from typing import Any

from noeta.core.tool_input import input_problems, tool_use_error

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string"},
        "limit": {"type": "integer"},
        "flag": {"type": "boolean"},
        "mode": {"type": "string", "enum": ["a", "b"]},
        "items": {"type": "array", "items": {
            "type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"],
            "additionalProperties": False}},
    },
    "required": ["file_path"],
    "additionalProperties": False,
}


def test_a_fitting_input_has_no_problem() -> None:
    assert input_problems("T", SCHEMA, {"file_path": "a", "limit": 3, "items": [{"q": "x"}]}) \
        is None


def test_claude_codes_coercions_and_a_null_optional_pass() -> None:
    assert input_problems("T", SCHEMA, {"file_path": "a", "limit": "3", "flag": "true",
                                        "items": '[{"q": "x"}]', "mode": None}) is None


def test_problems_are_listed_in_claude_codes_order_and_words() -> None:
    text = input_problems("T", SCHEMA, {"limit": "x", "extra": 1, "mode": "c",
                                        "items": [{"z": 1}]})
    assert text == (
        "InputValidationError: T failed due to the following issues:\n"
        "The required parameter `file_path` is missing\n"
        "The required parameter `items[0].q` is missing\n"
        "An unexpected parameter `extra` was provided\n"
        "An unexpected parameter `items[0].z` was provided\n"
        "The parameter `limit` type is expected as `number` but provided as `string`\n"
        'The parameter `mode` is invalid: Invalid option: expected one of "a"|"b"')


def test_one_problem_says_issue() -> None:
    assert input_problems("Read", SCHEMA, {}) == (
        "InputValidationError: Read failed due to the following issue:\n"
        "The required parameter `file_path` is missing")
    assert tool_use_error("x") == "<tool_use_error>x</tool_use_error>"
