"""The CJK-aware token estimate budgets are counted in, and the 1 % of a
model's window a resident listing may take."""

from __future__ import annotations

import re

#: The 1 % of a model's window a resident listing (skills, memory index) may
#: take, and its ceiling: at a 1M-token window the fraction alone would admit
#: ~10K tokens of roster on every request.
RESIDENT_BUDGET_FRACTION = 0.01
RESIDENT_BUDGET_CEILING_TOKENS = 4096
#: The budget when no model is known: 1 % of a 200k-token window.
DEFAULT_RESIDENT_BUDGET_TOKENS = 2000

_CHARS_PER_TOKEN = 4
#: One character = one token for these scripts; ``chars/4`` would undercount
#: a Chinese summary three- to four-fold.
_CJK_CHAR = re.compile(
    "["
    "ᄀ-ᇿ"  # Hangul Jamo
    "　-㏿"  # CJK symbols, kana, bopomofo, compatibility jamo, ...
    "㐀-䶿"  # CJK unified ideographs extension A
    "一-鿿"  # CJK unified ideographs
    "가-힯"  # Hangul syllables
    "豈-﫿"  # CJK compatibility ideographs
    "＀-￯"  # halfwidth and fullwidth forms
    "\U00020000-\U0002ffff"  # CJK unified ideographs extensions B-F
    "]"
)


#: Claude models whose tokens Claude Code counts as four characters; it
#: counts three for every other model.
_FOUR_CHAR_MODELS = frozenset((
    "claude-3-opus", "claude-3-sonnet", "claude-3-haiku", "claude-3-5-sonnet",
    "claude-3-5-haiku", "claude-3-7-sonnet", "claude-opus-4-0", "claude-opus-4-1",
    "claude-opus-4-5", "claude-opus-4-6", "claude-sonnet-4-0", "claude-sonnet-4-5",
    "claude-sonnet-4-6", "claude-haiku-4-5"))


def chars_per_token(model: str) -> int:
    """Characters per token in Claude Code's size limits: 4 for its older
    models (and an unknown one), 3 for the rest."""
    name = model.rsplit("/", 1)[-1].lower().replace(".", "-").replace("_", "-")
    if not name:
        return 4
    return 4 if any(name == m or name.startswith(m + "-") for m in _FOUR_CHAR_MODELS) else 3


def has_cjk(text: str) -> bool:
    """Whether ``text`` holds a CJK character (prose with no spaces)."""
    return _CJK_CHAR.search(text) is not None


def estimate_tokens(text: str) -> int:
    """Deterministic, CJK-aware token estimate: CJK characters count one
    each, the rest ``ceil(n / 4)``. A budgeting unit, not a billed count."""
    cjk = len(_CJK_CHAR.findall(text))
    other = len(text) - cjk
    return cjk + (other + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def prefix_within(text: str, max_tokens: int, suffix: str = "") -> int:
    """The length of the longest prefix of ``text`` that, followed by
    ``suffix``, estimates at most ``max_tokens``."""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid] + suffix) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return lo


def resident_budget(model: str) -> int:
    """1 % of ``model``'s catalog window, capped at 4,096 tokens."""
    if not model:
        return DEFAULT_RESIDENT_BUDGET_TOKENS
    from noeta.core.models import model_limits

    window, _ = model_limits(model)
    return min(max(1, int(window * RESIDENT_BUDGET_FRACTION)), RESIDENT_BUDGET_CEILING_TOKENS)
