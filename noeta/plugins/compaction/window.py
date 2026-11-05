"""The auto-compaction threshold, derived from the model catalog as Claude
Code derives it.

The effective window is the model's window (or the ``auto_compact_window``
cap, ``NOETA_AUTO_COMPACT_WINDOW``: Claude Code's knob, noeta's name) less
``min(output ceiling, 20_000)`` held back for the summary. The threshold is
13,000 tokens under that, or ``auto_compact_percent`` of the effective
window (``NOETA_AUTOCOMPACT_PCT_OVERRIDE``) when that is lower. An
uncatalogued model gets the catalog's conservative 128K / 16,384 pair,
never "compaction off".
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from noeta.core.models import model_limits, resolve_alias

#: Most of the output ceiling held back under the window.
OUTPUT_RESERVE_MAX_TOKENS = 20_000
#: How far under the effective window auto-compaction fires.
AUTOCOMPACT_BUFFER_TOKENS = 13_000
#: The share of the effective window under which a summary starts being
#: written ahead of the threshold (Claude Code's precomputed compaction).
PRECOMPUTE_BUFFER_FRACTION = 0.2
#: Claude Code's environment knobs under noeta's prefix, read when the plugin
#: is not given one.
WINDOW_ENV = "NOETA_AUTO_COMPACT_WINDOW"
PERCENT_ENV = "NOETA_AUTOCOMPACT_PCT_OVERRIDE"


@dataclass(frozen=True)
class Window:
    #: The window the numbers derive from (the model's, or the cap).
    context_window: int
    #: The model's output ceiling.
    max_output_tokens: int
    #: ``context_window`` less the summary's output reserve.
    effective: int
    #: Tokens at which auto-compaction fires.
    threshold: int
    #: Tokens from which a summary is written ahead of the threshold.
    precompute: int


def check_positive(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive int or None; got {value!r}")


def check_percent(value: float | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 100:
        raise ValueError(f"auto_compact_percent must be in (0, 100] or None; got {value!r}")


def window_from_env() -> int | None:
    raw = os.environ.get(WINDOW_ENV, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def percent_from_env() -> float | None:
    raw = os.environ.get(PERCENT_ENV, "").strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if 0 < value <= 100 else None


def derive_window(model: str, *, auto_compact_window: int | None = None,
                  auto_compact_percent: float | None = None) -> Window:
    """The compaction numbers for ``model``. Raises ``ValueError`` when the
    cap leaves no window under the output reserve."""
    check_positive("auto_compact_window", auto_compact_window)
    check_percent(auto_compact_percent)
    context_window, max_output_tokens = model_limits(model)
    if auto_compact_window is not None:
        context_window = min(context_window, auto_compact_window)
    reserve = min(max_output_tokens, OUTPUT_RESERVE_MAX_TOKENS)
    effective = context_window - reserve
    if effective <= 0:
        raise ValueError(
            f"auto_compact_window {auto_compact_window} leaves no window under model "
            f"{resolve_alias(model)!r}'s {reserve}-token summary reserve")
    threshold = effective - AUTOCOMPACT_BUFFER_TOKENS
    if auto_compact_percent is not None:
        threshold = min(int(effective * auto_compact_percent / 100), threshold)
    threshold = max(1, threshold)
    return Window(context_window=context_window, max_output_tokens=max_output_tokens,
                  effective=effective, threshold=threshold,
                  precompute=min(effective - round(effective * PRECOMPUTE_BUFFER_FRACTION),
                                 threshold))
