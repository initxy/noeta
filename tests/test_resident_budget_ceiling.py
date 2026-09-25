"""The host-derived skill-menu and memory-index budgets are 1 % of the model's
window, capped at an absolute ceiling — a 1M-token window no longer admits
~10K tokens of roster plus ~10K of index at the head of every request."""

from __future__ import annotations

from noeta.builtins.providers.impl.catalog import find_spec
from noeta.client.host import (
    SKILL_MENU_BUDGET_CEILING_TOKENS,
    SKILL_MENU_BUDGET_FRACTION,
    memory_index_budget_tokens,
    skill_menu_budget_tokens,
)

_LARGE_WINDOW_MODEL = "claude-sonnet-5"


def test_large_windows_hit_the_ceiling() -> None:
    spec = find_spec(_LARGE_WINDOW_MODEL)
    assert spec is not None
    fraction = int(spec.context_window * SKILL_MENU_BUDGET_FRACTION)
    assert fraction > SKILL_MENU_BUDGET_CEILING_TOKENS, spec.context_window
    assert skill_menu_budget_tokens(_LARGE_WINDOW_MODEL) == SKILL_MENU_BUDGET_CEILING_TOKENS
    assert (
        memory_index_budget_tokens(_LARGE_WINDOW_MODEL)
        == SKILL_MENU_BUDGET_CEILING_TOKENS
    )


def test_small_windows_keep_the_fraction() -> None:
    # An uncatalogued model takes the conservative 128K window: 1 % is 1,280,
    # well under the ceiling, so the fraction is what applies.
    derived = skill_menu_budget_tokens("stub-model")
    assert 0 < derived < SKILL_MENU_BUDGET_CEILING_TOKENS
    assert derived == memory_index_budget_tokens("stub-model")
