"""Direct unit tests for ``normalize_answer_document`` (B17 / U6).

The HITL answer contract was loosened so a chosen option and a freeform note can
COEXIST (product direction ①). These pin the validator: each field is checked
independently, at least one is required, the normalized shape stays
``{choice_id, text}`` (None for the absent field) so older single-field
recordings remain byte-identical, and the disallowed cases still raise.
"""

from __future__ import annotations

import pytest

from noeta.policies.control_semantics import (
    AnswerValidationError,
    normalize_answer_document,
)

# One question with choices AND freeform allowed (the case the old "exactly one"
# rule made impossible to answer fully).
_Q_BOTH = [
    {
        "id": "target",
        "question": "Which target?",
        "choices": [{"id": "staging", "label": "Staging"}, {"id": "prod", "label": "Prod"}],
        "allow_freeform": True,
    }
]
# A choices-only question (freeform disabled).
_Q_CHOICE_ONLY = [
    {
        "id": "target",
        "question": "Which target?",
        "choices": [{"id": "staging", "label": "Staging"}],
        "allow_freeform": False,
    }
]


def _norm(answers, questions=_Q_BOTH):
    return normalize_answer_document({"answers": answers}, questions)


def test_choice_only_normalizes_with_null_text():
    assert _norm({"target": {"choice_id": "staging"}}) == {
        "target": {"choice_id": "staging", "text": None}
    }


def test_text_only_normalizes_with_null_choice():
    assert _norm({"target": {"text": "go faster"}}) == {
        "target": {"choice_id": None, "text": "go faster"}
    }


def test_both_fields_coexist():
    # The core B17 fix: a choice AND a freeform note in one answer.
    assert _norm({"target": {"choice_id": "staging", "text": "but only EU"}}) == {
        "target": {"choice_id": "staging", "text": "but only EU"}
    }


def test_choice_with_blank_text_is_treated_as_choice_only():
    # Frontend may submit an empty "other" box alongside a choice.
    assert _norm({"target": {"choice_id": "staging", "text": "  "}}) == {
        "target": {"choice_id": "staging", "text": None}
    }


def test_neither_field_is_rejected():
    with pytest.raises(AnswerValidationError, match="must contain a choice_id or non-empty text"):
        _norm({"target": {}})


def test_invalid_choice_is_rejected():
    with pytest.raises(AnswerValidationError, match="is not allowed"):
        _norm({"target": {"choice_id": "nope"}})


def test_freeform_text_rejected_when_disallowed():
    with pytest.raises(AnswerValidationError, match="does not allow freeform text"):
        normalize_answer_document(
            {"answers": {"target": {"text": "anything"}}}, _Q_CHOICE_ONLY
        )


def test_choice_still_works_when_freeform_disallowed():
    assert normalize_answer_document(
        {"answers": {"target": {"choice_id": "staging"}}}, _Q_CHOICE_ONLY
    ) == {"target": {"choice_id": "staging", "text": None}}


def test_missing_and_unknown_answers_still_rejected():
    with pytest.raises(AnswerValidationError, match="missing answer"):
        _norm({})
    with pytest.raises(AnswerValidationError, match="unknown answer id"):
        _norm({"target": {"choice_id": "staging"}, "ghost": {"text": "x"}})


def test_too_long_text_is_rejected():
    with pytest.raises(AnswerValidationError, match="too long"):
        _norm({"target": {"text": "A" * 4001}})


def test_non_string_text_is_rejected_not_silently_dropped():
    # P2 hardening — a present non-string text is malformed, not absent.
    with pytest.raises(AnswerValidationError, match="text must be a string"):
        _norm({"target": {"choice_id": "staging", "text": 123}})
