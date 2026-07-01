"""The one failing test the bug-fixer Agent must turn green."""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from src.math_ops import add, double  # noqa: E402


def test_add_returns_sum() -> None:
    assert add(2, 3) == 5


def test_double_is_unaffected() -> None:
    # A second passing test so the suite isn't reduced to one assertion.
    assert double(4) == 8
