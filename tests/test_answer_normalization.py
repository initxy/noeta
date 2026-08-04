"""``normalize_answer_document`` — the ask answer codec.

Answers are keyed by the question's index key and hold
``{"selected": [labels...], "other": text}``: ``selected`` must be labels of
the question's options (at most one unless ``multiSelect``), and ``other`` is
the always-available free-text slot (the auto-appended "Other" option). The
codec fails loudly on anything malformed — a HITL answer silently dropped is a
decision silently invented.
"""

from __future__ import annotations

import pytest

from noeta.builtins.ask_user_question.impl import (
    AnswerValidationError,
    normalize_answer_document,
)


_Q = [
    {
        "id": "0",
        "question": "Deploy to which target?",
        "header": "Target",
        "options": [
            {"label": "Staging", "description": "safe"},
            {"label": "Prod", "description": "live"},
        ],
        "multiSelect": False,
    }
]

_Q_MULTI = [
    {
        "id": "0",
        "question": "Enable which features?",
        "header": "Features",
        "options": [
            {"label": "A", "description": "a"},
            {"label": "B", "description": "b"},
        ],
        "multiSelect": True,
    }
]


def _norm(raw, questions=_Q):
    return normalize_answer_document(raw, questions)


def test_selected_label_alone() -> None:
    assert _norm({"0": {"selected": ["Staging"]}}) == {
        "0": {"selected": ["Staging"], "other": None}
    }


def test_other_text_alone() -> None:
    assert _norm({"0": {"other": "go faster"}}) == {
        "0": {"selected": [], "other": "go faster"}
    }


def test_selected_and_other_coexist() -> None:
    assert _norm({"0": {"selected": ["Staging"], "other": "but only EU"}}) == {
        "0": {"selected": ["Staging"], "other": "but only EU"}
    }


def test_blank_other_counts_as_absent() -> None:
    assert _norm({"0": {"selected": ["Staging"], "other": "  "}}) == {
        "0": {"selected": ["Staging"], "other": None}
    }


def test_empty_answer_rejected() -> None:
    with pytest.raises(
        AnswerValidationError, match="selected labels or 'other' text"
    ):
        _norm({"0": {}})


def test_unknown_label_rejected() -> None:
    with pytest.raises(AnswerValidationError, match="is not an option"):
        _norm({"0": {"selected": ["Nope"]}})


def test_multi_labels_need_multiselect() -> None:
    with pytest.raises(AnswerValidationError, match="multiSelect is off"):
        _norm({"0": {"selected": ["Staging", "Prod"]}})
    assert _norm({"0": {"selected": ["A", "B"]}}, _Q_MULTI) == {
        "0": {"selected": ["A", "B"], "other": None}
    }


def test_duplicate_label_rejected() -> None:
    with pytest.raises(AnswerValidationError, match="same label twice"):
        _norm({"0": {"selected": ["A", "A"]}}, _Q_MULTI)


def test_answers_envelope_accepted() -> None:
    assert _norm({"answers": {"0": {"selected": ["Staging"]}}}) == {
        "0": {"selected": ["Staging"], "other": None}
    }


def test_missing_answer_rejected() -> None:
    with pytest.raises(AnswerValidationError, match="missing answer"):
        _norm({})


def test_unknown_answer_key_rejected() -> None:
    with pytest.raises(AnswerValidationError, match="unknown answer id"):
        _norm({"0": {"selected": ["Staging"]}, "ghost": {"other": "x"}})


def test_non_object_answer_rejected() -> None:
    with pytest.raises(AnswerValidationError, match="must be an object"):
        _norm({"0": "Staging"})


def test_non_list_selected_rejected() -> None:
    with pytest.raises(AnswerValidationError, match="list of strings"):
        _norm({"0": {"selected": "Staging"}})


def test_non_string_other_rejected_not_silently_dropped() -> None:
    with pytest.raises(AnswerValidationError, match="other must be a string"):
        _norm({"0": {"selected": ["Staging"], "other": 123}})


def test_too_long_other_is_rejected() -> None:
    with pytest.raises(AnswerValidationError, match="too long"):
        _norm({"0": {"other": "x" * 5000}})


def test_non_dict_body_rejected() -> None:
    with pytest.raises(AnswerValidationError, match="must be an object"):
        _norm(["Staging"])
