"""The numbers compaction measures against, derived from the model catalog.

Claude Code's margin (ADR-066): the usable window is
``window - min(output ceiling, 20_000) - 13_000``, whether the window is the
model's or an ``auto_compact_window`` cap (ADR-060). A third of it is the
verbatim tail a summary never covers (ADR-003); old tool results start being
cleared at half of it, or at ``clear_tool_outputs_at`` (ADR-050, 061). An
uncatalogued model gets the catalog's conservative 128K / 16,384 pair, never
"compaction off".
"""

from __future__ import annotations

from dataclasses import dataclass

from noeta.models import model_limits, resolve_alias

#: Most of the output ceiling held back under the window.
OUTPUT_RESERVE_MAX_TOKENS = 20_000
#: Where the summarize trigger sits under that.
TRIGGER_MARGIN_TOKENS = 13_000
#: ``tail = usable // 3``.
TAIL_FRACTION_DENOM = 3
#: The second clearing valve opens at this fraction of the usable window.
MICROCOMPACT_FRACTION = 0.5


@dataclass(frozen=True)
class Window:
    #: The window the numbers derive from (the model's, or the cap).
    context_window: int
    #: The model's output ceiling.
    max_output_tokens: int
    #: The summarize trigger and the relief valve's water mark.
    usable: int
    #: The verbatim tail a summary never covers, in real tokens.
    tail_budget: int
    #: The fraction of ``usable`` at which the second valve opens.
    micro_fraction: float


def check_positive(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive int or None; got {value!r}")


def derive_window(
    model: str,
    *,
    auto_compact_window: int | None = None,
    clear_tool_outputs_at: int | None = None,
) -> Window:
    """The compaction numbers for ``model``. Raises ``ValueError`` when the
    cap leaves no usable window under the model's reserve."""
    check_positive("auto_compact_window", auto_compact_window)
    check_positive("clear_tool_outputs_at", clear_tool_outputs_at)
    context_window, max_output_tokens = model_limits(model)
    reserve = min(max_output_tokens, OUTPUT_RESERVE_MAX_TOKENS) + TRIGGER_MARGIN_TOKENS
    if auto_compact_window is not None:
        if auto_compact_window - reserve <= 0:
            raise ValueError(
                f"auto_compact_window {auto_compact_window} leaves no usable "
                f"window under model {resolve_alias(model)!r}'s {reserve}-token compaction "
                f"reserve (min(output ceiling {max_output_tokens}, "
                f"{OUTPUT_RESERVE_MAX_TOKENS}) plus the "
                f"{TRIGGER_MARGIN_TOKENS}-token trigger margin)"
            )
        context_window = min(context_window, auto_compact_window)
    usable = max(0, context_window - reserve)
    fraction = MICROCOMPACT_FRACTION
    if clear_tool_outputs_at is not None and usable > 0:
        fraction = min(1.0, clear_tool_outputs_at / usable)
    return Window(
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        usable=usable,
        tail_budget=usable // TAIL_FRACTION_DENOM,
        micro_fraction=fraction,
    )
