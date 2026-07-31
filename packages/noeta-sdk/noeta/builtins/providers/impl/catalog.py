"""Provider-neutral model catalog: one :class:`ModelSpec` per model, the
pricing built on it, and the compaction knobs derived from it.

Anthropic and OpenAI rows share the same dataclass and no vendor wire key
(``cache_creation_input_tokens`` / ``prompt_tokens`` / ``total_tokens``) is
ever a field name here — each adapter has already mapped its wire usage into
the neutral :class:`Usage` that :func:`price` charges. The kernel does not
import this module: pricing, alias resolution, the edit-tool family and the
compaction knobs all reach it as injected callbacks or pre-resolved values.
Prices are USD per 1,000,000 tokens.
"""

from __future__ import annotations

from dataclasses import dataclass

from noeta.context.composer import COMPOSER_VERSION
from noeta.execution.builder import COMPACTION_OFF, CompactionConfig
from noeta.protocols.messages import Usage


__all__ = [
    "ModelSpec",
    "CATALOG",
    "ALIASES",
    "spec_for",
    "resolve_alias",
    "price",
    "provider_family",
    "derive_compaction_config",
]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Provider-neutral description of one model; prices are USD per 1M tokens.

    ``cache_read`` (cheaper) and ``cache_write`` (dearer) stay distinct from
    fresh ``input`` because Anthropic bills the three buckets at different
    rates. An OpenAI row, having no cache-write tier, sets the cache prices so
    the same arithmetic degrades cleanly.
    """

    real_model_id: str
    context_window: int
    max_output_tokens: int
    input_price_per_mtok: float
    output_price_per_mtok: float
    cache_read_price_per_mtok: float
    cache_write_price_per_mtok: float
    is_reasoning: bool = False
    #: Defaults to False so a model nobody marked as vision-capable is treated
    #: as text-only and an ``ImageBlock`` bound for it is refused up front by
    #: the adapters' vision guard, rather than sent to a model that cannot
    #: read it.
    supports_vision: bool = False


# ---------------------------------------------------------------------------
# Catalog data. Every public row is a transcription of a vendor page, so treat
# the numbers as facts to re-check, not as knobs to tune.
#
# Anthropic prices/IDs: platform.claude.com/docs/en/about-claude/pricing and
# /models/overview. Cache write = 1.25x input (5-min TTL), cache read = 0.1x
# input — the derived numbers below match the published per-model columns.
#
# OpenAI prices/IDs: the per-model pages under developers.openai.com/api/docs/
# models/ (the main pricing table omits the 4o generation). OpenAI has no
# separate cache-write tier; cached input = 0.5x input.
# ---------------------------------------------------------------------------

CATALOG: dict[str, ModelSpec] = {
    # --- Anthropic ---------------------------------------------------------
    "claude-opus-4-8": ModelSpec(
        real_model_id="claude-opus-4-8",
        context_window=1_000_000,
        max_output_tokens=128_000,
        input_price_per_mtok=5.00,
        output_price_per_mtok=25.00,
        cache_read_price_per_mtok=0.50,  # ≈ 0.1× input
        cache_write_price_per_mtok=6.25,  # ≈ 1.25× input (5-min TTL)
        is_reasoning=True,
        supports_vision=True,
    ),
    "claude-sonnet-4-6": ModelSpec(
        real_model_id="claude-sonnet-4-6",
        context_window=1_000_000,
        max_output_tokens=128_000,
        input_price_per_mtok=3.00,
        output_price_per_mtok=15.00,
        cache_read_price_per_mtok=0.30,  # ≈ 0.1× input
        cache_write_price_per_mtok=3.75,  # ≈ 1.25× input (5-min TTL)
        is_reasoning=True,
        supports_vision=True,
    ),
    "claude-haiku-4-5": ModelSpec(
        real_model_id="claude-haiku-4-5",
        context_window=200_000,
        max_output_tokens=64_000,
        input_price_per_mtok=1.00,
        output_price_per_mtok=5.00,
        cache_read_price_per_mtok=0.10,  # ≈ 0.1× input
        cache_write_price_per_mtok=1.25,  # ≈ 1.25× input (5-min TTL)
        is_reasoning=False,
        supports_vision=True,
    ),
    # --- OpenAI ------------------------------------------------------------
    "gpt-4o": ModelSpec(
        real_model_id="gpt-4o",
        context_window=128_000,
        max_output_tokens=16_384,
        input_price_per_mtok=2.50,
        output_price_per_mtok=10.00,
        cache_read_price_per_mtok=1.25,  # OpenAI cached input ≈ 0.5× input
        cache_write_price_per_mtok=2.50,  # OpenAI has no write tier → = input
        is_reasoning=False,
    ),
    "gpt-4o-mini": ModelSpec(
        real_model_id="gpt-4o-mini",
        context_window=128_000,
        max_output_tokens=16_384,
        input_price_per_mtok=0.15,
        output_price_per_mtok=0.60,
        cache_read_price_per_mtok=0.075,  # OpenAI cached input ≈ 0.5× input
        cache_write_price_per_mtok=0.15,  # no write tier → = input
        is_reasoning=False,
    ),
    # --- OpenAI Responses gateway models (reasoning + vision) ---------------
    # These are served by an internal gateway that publishes no pricing, so
    # every rate is 0.0 and cost accounting reports $0 for them: ModelSpec has
    # no "unknown price" representation and price() multiplies the rates as
    # given. They are catalogued anyway, because a row is what stops price()
    # from raising KeyError and what lets the vision guard admit an image.
    # Anything marked "placeholder" below is a guess the gateway has not
    # confirmed; a context window guessed too small starves the verbatim tail
    # and forces the model to re-read files through tools.
    "gpt-5.4-2026-03-05": ModelSpec(
        real_model_id="gpt-5.4-2026-03-05",
        context_window=128_000,  # placeholder
        max_output_tokens=16_384,  # placeholder
        input_price_per_mtok=0.0,
        output_price_per_mtok=0.0,
        cache_read_price_per_mtok=0.0,
        cache_write_price_per_mtok=0.0,
        is_reasoning=True,
        supports_vision=True,
    ),
    "gpt-5.5-2026-04-24": ModelSpec(
        real_model_id="gpt-5.5-2026-04-24",
        context_window=200_000,  # confirmed by the gateway
        max_output_tokens=16_384,  # placeholder
        input_price_per_mtok=0.0,
        output_price_per_mtok=0.0,
        cache_read_price_per_mtok=0.0,
        cache_write_price_per_mtok=0.0,
        is_reasoning=True,
        supports_vision=True,
    ),
}


# Alias → real model-id. Translation only: which selectors a principal may
# bind is the driver's allowlist decision, not this table's.
ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}


def resolve_alias(selector: str) -> str:
    """Map a friendly alias to its real model-id, passing anything else through.

    Pass-through is deliberate: a real model-id and the test-only
    ``stub-model`` must survive unchanged, so callers may hand either form to
    every catalog function.
    """
    return ALIASES.get(selector, selector)


def spec_for(model_id: str) -> ModelSpec:
    """Look up a model's spec, by real id or alias.

    Raises :class:`KeyError` for an unknown id so a mis-typed or unpriced model
    surfaces loudly instead of silently costing $0.
    """
    return CATALOG[resolve_alias(model_id)]


def provider_family(model: str) -> str | None:
    """Classify a model selector as ``"anthropic"``, ``"openai"`` or ``None``.

    The only model→family judgment in the codebase: the assembly layer uses it
    to pick the provider-appropriate edit tool without writing the vendor
    difference into any tool field or prompt. The family comes from a
    **catalogued** model's real-id prefix, so only a registered model can
    switch the tool set.

    ``None`` — an uncatalogued selector, including every test sentinel — means
    "do not filter": callers keep both edit variants, so an unknown model never
    silently loses a tool.
    """
    real = resolve_alias(model)
    spec = CATALOG.get(real)
    if spec is None:
        return None
    if spec.real_model_id.startswith("claude"):
        return "anthropic"
    if spec.real_model_id.startswith("gpt"):
        return "openai"
    return None


def price(model_id: str, usage: Usage) -> float:
    """Cost in USD for one round-trip's :class:`Usage` on ``model_id``.

    Each token bucket carries its own per-MTok rate. ``reasoning_tokens`` are
    deliberately absent from the sum: they are hidden completion tokens already
    counted in ``output`` and billed at the output rate, so adding them would
    double-charge. Raises :class:`KeyError` for an unknown model.
    """
    spec = CATALOG[resolve_alias(model_id)]
    return (
        usage.uncached / 1_000_000 * spec.input_price_per_mtok
        + usage.cache_read / 1_000_000 * spec.cache_read_price_per_mtok
        + usage.cache_write / 1_000_000 * spec.cache_write_price_per_mtok
        + usage.output / 1_000_000 * spec.output_price_per_mtok
    )


# ---------------------------------------------------------------------------
# Compaction knobs derived from the catalog
# ---------------------------------------------------------------------------

#: Headroom reserved under the context window beyond the output cap, so the
#: history window still leaves slack for the system prompt, the tool schemas
#: and the next response. A constant, not a measurement: live and resume must
#: derive the same number.
_COMPACTION_BUFFER_TOKENS = 2_000

#: Denominator for the verbatim recent tail (``tail = available // N``). A
#: verbatim tail exists at all because compose cannot re-read disk (resume
#: determinism must hold), but it is expensive window: the summary keeps file
#: paths and the model can re-read with ``read``. A smaller tail trades recent
#: verbatim fidelity for headroom, so compaction fires less often and each
#: summary covers a longer prefix. Constant for the same live/resume reason.
_TAIL_FRACTION_DENOM = 3


def derive_compaction_config(model: str) -> CompactionConfig:
    """Derive the compaction knobs for ``model``, or ``COMPACTION_OFF``.

    The alias is resolved before the lookup: an unresolved alias — the common
    selector a host passes — would miss the catalog and silently disable
    compaction. An uncatalogued model likewise turns compaction off rather than
    guessing a window.

    Every number is a deterministic function of the spec and live and resume
    resolve the same ``model`` string, so both paths derive identical knobs.
    """
    try:
        spec = spec_for(resolve_alias(model))
    except KeyError:
        return COMPACTION_OFF
    available = max(
        0,
        spec.context_window
        - spec.max_output_tokens
        - _COMPACTION_BUFFER_TOKENS,
    )
    # Strictly smaller than the available window, so summarising always has a
    # non-empty prefix to collapse when the trigger fires.
    tail = available // _TAIL_FRACTION_DENOM
    return CompactionConfig(
        context_window=spec.context_window,
        max_output_tokens=spec.max_output_tokens,
        compaction_buffer=_COMPACTION_BUFFER_TOKENS,
        tail_token_budget=tail,
        composer_version=COMPOSER_VERSION,
    )
