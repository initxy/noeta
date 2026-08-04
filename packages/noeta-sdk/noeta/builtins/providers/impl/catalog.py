"""Provider-neutral model catalog: one :class:`ModelSpec` per model, the
pricing built on it, and the compaction knobs derived from it.

Anthropic and OpenAI rows share the same dataclass and no vendor wire key
(``cache_creation_input_tokens`` / ``prompt_tokens`` / ``total_tokens``) is
ever a field name here — each adapter has already mapped its wire usage into
the neutral :class:`Usage` that :func:`price` charges. The kernel does not
import this module: pricing, alias resolution, the edit-tool family and the
compaction knobs all reach it as injected callbacks or pre-resolved values.
Prices are USD per 1,000,000 tokens.

**An unknown model is a warning, never a silent default.** A selector this
table does not carry used to degrade three ways at once — ``$0`` cost,
``supports_vision=False`` (a hard image refusal in the adapters), and
``COMPACTION_OFF`` (compaction *and* the composer's tail prune disabled) — all
without a single log line. The catalog now logs once per model id on each of
those paths and answers with a conservative default instead, so a gateway model
nobody catalogued still compacts and still bills visibly-as-unknown. The
warn-once bookkeeping is process-global and touches no returned value, so live
and resume derive identical knobs regardless of who warned first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from noeta.context.composer import COMPOSER_VERSION
from noeta.execution.builder import CompactionConfig
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


_LOGGER = logging.getLogger(__name__)

#: Model ids already warned about, keyed by ``(concern, model_id)`` so the
#: pricing warning and the compaction warning each get one line per model
#: rather than one line per round-trip. Diagnostics only: nothing reads it back
#: to decide a value, so it can never make two processes derive different knobs.
_WARNED: set[tuple[str, str]] = set()


def _warn_once(concern: str, model_id: str, message: str) -> None:
    """Log ``message`` at WARNING the first time ``(concern, model_id)`` is seen."""
    key = (concern, model_id)
    if key in _WARNED:
        return
    _WARNED.add(key)
    _LOGGER.warning(message)


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Provider-neutral description of one model; prices are USD per 1M tokens.

    ``cache_read`` (cheaper) and ``cache_write`` (dearer) stay distinct from
    fresh ``input`` because Anthropic bills the three buckets at different
    rates. An OpenAI row, having no cache-write tier, sets the cache prices so
    the same arithmetic degrades cleanly.

    **Absent pricing is a state, not a zero.** Every price field is
    ``Optional``: a row served by a gateway that publishes no rate card sets
    them to ``None``, which :func:`price` reports as unknown (warn once, charge
    0.0) instead of quietly asserting the model is free. A literal ``0.0``
    therefore means "genuinely free", and only that.
    """

    real_model_id: str
    context_window: int
    max_output_tokens: int
    input_price_per_mtok: Optional[float] = None
    output_price_per_mtok: Optional[float] = None
    cache_read_price_per_mtok: Optional[float] = None
    cache_write_price_per_mtok: Optional[float] = None
    is_reasoning: bool = False
    #: Defaults to False so a model nobody marked as vision-capable is treated
    #: as text-only. Note the asymmetry the adapters implement: a CATALOGUED
    #: ``supports_vision=False`` is a decision and their vision guard refuses
    #: the image up front, while a model absent from the table is merely
    #: *unknown* and images are let through for the provider to judge.
    supports_vision: bool = False

    @property
    def is_priced(self) -> bool:
        """True when all four rates are known, i.e. :func:`price` can charge."""
        return None not in (
            self.input_price_per_mtok,
            self.output_price_per_mtok,
            self.cache_read_price_per_mtok,
            self.cache_write_price_per_mtok,
        )


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
    "claude-opus-5": ModelSpec(
        real_model_id="claude-opus-5",
        context_window=1_000_000,
        max_output_tokens=128_000,
        input_price_per_mtok=5.00,
        output_price_per_mtok=25.00,
        cache_read_price_per_mtok=0.50,  # ≈ 0.1× input
        cache_write_price_per_mtok=6.25,  # ≈ 1.25× input (5-min TTL)
        is_reasoning=True,
        supports_vision=True,
    ),
    "claude-sonnet-5": ModelSpec(
        real_model_id="claude-sonnet-5",
        context_window=1_000_000,
        max_output_tokens=128_000,
        # Standard rates. An introductory $2.00 / $10.00 runs to 2026-08-31;
        # this table has no date dimension, so it carries the rate that
        # outlives the promotion — over-reporting cost is the safe direction
        # for a budget ceiling.
        input_price_per_mtok=3.00,
        output_price_per_mtok=15.00,
        cache_read_price_per_mtok=0.30,  # ≈ 0.1× input
        cache_write_price_per_mtok=3.75,  # ≈ 1.25× input (5-min TTL)
        is_reasoning=True,
        supports_vision=True,
    ),
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
    # every rate is left ABSENT (``None``) rather than faked as 0.0: price()
    # then reports them as unknown-and-warned instead of asserting they are
    # free. They are catalogued anyway, because a row is what carries the
    # window/output knobs and the vision capability bit.
    # Anything marked "placeholder" below is a guess the gateway has not
    # confirmed; a context window guessed too small starves the verbatim tail
    # and forces the model to re-read files through tools.
    "gpt-5.4-2026-03-05": ModelSpec(
        real_model_id="gpt-5.4-2026-03-05",
        context_window=128_000,  # placeholder
        max_output_tokens=16_384,  # placeholder
        is_reasoning=True,
        supports_vision=True,
    ),
    "gpt-5.5-2026-04-24": ModelSpec(
        real_model_id="gpt-5.5-2026-04-24",
        context_window=200_000,  # confirmed by the gateway
        max_output_tokens=16_384,  # placeholder
        is_reasoning=True,
        supports_vision=True,
    ),
}


# Alias → real model-id. Translation only: which selectors a principal may
# bind is the driver's allowlist decision, not this table's.
#
# The aliases track the CURRENT generation, so a host that binds ``"opus"``
# follows the vendor forward without a code change; the superseded 4.x rows
# stay in the table and stay addressable by their full id, which is what a
# caller who deliberately pinned a generation gets. There is no haiku 5, so
# ``haiku`` stays on 4.5.
ALIASES: dict[str, str] = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
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
    double-charge.

    **Unknown price ⇒ warn once, charge 0.0.** Two rows land there: a model
    absent from the catalog, and a catalogued row whose rates the vendor does
    not publish (``is_priced`` false). Both used to be indistinguishable from
    "free" — the callers that fold this into a budget saw a plain ``0.0`` and
    the one caller that could have noticed (the SDK's pricing callback) swallows
    the old ``KeyError`` by design. Returning 0.0 keeps that contract and the
    return type stable, so cost folding and budget arithmetic are unchanged;
    the warning is what makes the state visible, and it is emitted here so
    every caller — including the swallowing one — inherits it.
    """
    real = resolve_alias(model_id)
    spec = CATALOG.get(real)
    if spec is None:
        _warn_once(
            "pricing",
            real,
            f"model {real!r} is not in the catalog: charging $0.00 for it. "
            "Cost accounting and any max_cost_usd budget will under-report "
            "until a ModelSpec row is added.",
        )
        return 0.0
    if (
        spec.input_price_per_mtok is None
        or spec.output_price_per_mtok is None
        or spec.cache_read_price_per_mtok is None
        or spec.cache_write_price_per_mtok is None
    ):
        _warn_once(
            "pricing",
            real,
            f"model {real!r} is catalogued without published prices: "
            "charging $0.00 for it. Cost accounting and any max_cost_usd "
            "budget will under-report until its rates are filled in.",
        )
        return 0.0
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

#: The window an UNCATALOGUED model is assumed to have. Deliberately at the
#: small end of what any current model ships (128 K is the floor of the
#: generation, not an average), because the failure modes are asymmetric:
#: guessing too small compacts earlier than necessary — a bounded cost in
#: fidelity — while guessing too large lets the history run past the real
#: window and the task dies on an overflow the policy never anticipated.
_UNKNOWN_MODEL_CONTEXT_WINDOW = 128_000
#: Matching conservative output cap. Unlike the window guess this one is NOT
#: free: the value also rides ``LLMRequest.max_tokens``, so an uncatalogued
#: model genuinely cannot answer past it — hitting the cap surfaces as a
#: retryable ``llm_truncated`` failure, and the derive-time warn-once names
#: the number. Add a catalog row to lift the cap to the model's real ceiling.
_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS = 16_384


def derive_compaction_config(model: str) -> CompactionConfig:
    """Derive the compaction knobs for ``model``.

    The alias is resolved before the lookup: an unresolved alias — the common
    selector a host passes — would miss the catalog and take the unknown-model
    path for a model this table actually knows.

    **An uncatalogued model gets conservative knobs, not ``COMPACTION_OFF``.**
    Returning ``COMPACTION_OFF`` disables compaction *and* the composer's tail
    prune, so the history grows unbounded until the provider rejects it — and
    it did so silently, for exactly the population most likely to need the
    protection (a self-hosted or gateway model nobody added a row for). One
    warning per model id, then a config derived from
    :data:`_UNKNOWN_MODEL_CONTEXT_WINDOW` /
    :data:`_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS`. ``COMPACTION_OFF`` remains
    reachable only by a caller that asks for it explicitly.

    Every number is a deterministic function of the spec (or of those two
    module constants) and live and resume resolve the same ``model`` string, so
    both paths derive identical knobs.
    """
    real = resolve_alias(model)
    spec = CATALOG.get(real)
    if spec is None:
        _warn_once(
            "compaction",
            real,
            f"model {real!r} is not in the catalog: falling back to a "
            f"conservative {_UNKNOWN_MODEL_CONTEXT_WINDOW}-token context "
            f"window and a {_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS}-token output "
            "cap for compaction. Add a ModelSpec row to use the model's real "
            "window.",
        )
        context_window = _UNKNOWN_MODEL_CONTEXT_WINDOW
        max_output_tokens = _UNKNOWN_MODEL_MAX_OUTPUT_TOKENS
    else:
        context_window = spec.context_window
        max_output_tokens = spec.max_output_tokens
    available = max(
        0,
        context_window - max_output_tokens - _COMPACTION_BUFFER_TOKENS,
    )
    # Strictly smaller than the available window, so summarising always has a
    # non-empty prefix to collapse when the trigger fires.
    tail = available // _TAIL_FRACTION_DENOM
    return CompactionConfig(
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        compaction_buffer=_COMPACTION_BUFFER_TOKENS,
        tail_token_budget=tail,
        composer_version=COMPOSER_VERSION,
    )
