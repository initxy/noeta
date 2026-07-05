"""Provider-neutral model spec catalog + pricing (catalog work item, D-C1..D-C4).

A single Noeta-shape :class:`ModelSpec` describes one model regardless of
vendor: real model-id, context window, output cap, per-MTok prices (with
cache read/write priced distinctly per Foundation-A's split), and a reasoning
flag. Cost accounting consumes :func:`price` (typed :class:`Usage` → USD);
context management consumes :func:`spec_for` for ``context_window`` /
``max_output_tokens``.

Provider neutrality: both Anthropic and OpenAI rows live in the
**same** dataclass; no vendor wire key (``cache_creation_input_tokens`` /
``total_tokens`` / ``prompt_tokens``) is ever a field name here. Each
adapter already maps its wire usage into the neutral :class:`Usage`
(Foundation-A); the catalog only prices that neutral shape.

import-linter: this module sits in ``noeta.providers`` and so may import
**only** ``noeta.protocols`` + stdlib (``providers-only-protocols`` contract).
``noeta.runtime`` must NOT import this — pricing reaches ``RuntimeLLMClient``
as an injected callback (see ``noeta.agent.wiring.engine``).

Pricing provenance (D-C2): all public-model rows (Anthropic + OpenAI) were
verified against the vendors' official pricing/model pages on 2026-07-05;
each row cites its source. The two internal-gateway models (``gpt-5.4-*`` /
``gpt-5.5-*``) have NO published pricing — their rates are 0.0 and cost
accounting reports $0 for them; treat those two rows as unverified until the
gateway publishes numbers. Prices are USD per 1,000,000 tokens.
"""

from __future__ import annotations

from dataclasses import dataclass

from noeta.protocols.messages import Usage


__all__ = [
    "ModelSpec",
    "CATALOG",
    "ALIASES",
    "spec_for",
    "resolve_alias",
    "price",
    "provider_family",
]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Noeta-shape, provider-neutral description of one model.

    Prices are USD per 1,000,000 tokens. ``cache_read`` is cheaper than
    fresh ``input`` and ``cache_write`` is more expensive — the three are
    kept distinct because Foundation-A splits the token buckets and Anthropic
    bills them at different rates. OpenAI rows (no cache tier) set the
    cache prices equal to the input price so the math degrades cleanly.
    """

    real_model_id: str
    context_window: int
    max_output_tokens: int
    input_price_per_mtok: float
    output_price_per_mtok: float
    cache_read_price_per_mtok: float
    cache_write_price_per_mtok: float
    is_reasoning: bool = False
    #: Vision-capability flag (same nature as
    #: ``is_reasoning``). Defaults to False: any model not explicitly marked
    #: as vision-capable is treated as text-only, so an ``ImageBlock`` request
    #: hitting it is blocked up front by the Responses adapter's vision guard
    #: (don't send images to a model that can't read them).
    supports_vision: bool = False


# ---------------------------------------------------------------------------
# Catalog data — FACTUAL; public rows verified 2026-07-05 (sources per row).
# ---------------------------------------------------------------------------
#
# Anthropic prices/IDs: platform.claude.com/docs/en/about-claude/pricing
# (docs.claude.com redirects there), verified 2026-07-05. Cache write =
# 1.25x input (5-min TTL), cache read = 0.1x input — the derived numbers
# below match the published per-model cache columns exactly.
#
# OpenAI prices/IDs: developers.openai.com/api/docs/models/gpt-4o and
# /gpt-4o-mini (platform.openai.com/docs/pricing redirects to
# developers.openai.com, whose main table no longer lists 4o-generation
# models — the per-model pages carry the prices), verified 2026-07-05.
# OpenAI has no separate cache-write tier; cached input = 0.5x input.

CATALOG: dict[str, ModelSpec] = {
    # --- Anthropic ---------------------------------------------------------
    # verified 2026-07-05 against platform.claude.com/docs/en/about-claude/pricing
    # ($5 in / $25 out / $0.50 cache read / $6.25 5m cache write) and
    # /models/overview (1M context, 128k max output)
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
    # verified 2026-07-05 against platform.claude.com/docs/en/about-claude/pricing
    # ($3 in / $15 out / $0.30 cache read / $3.75 5m cache write) and
    # /models/overview (1M context, 128k max output — was wrongly 64k here)
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
    # verified 2026-07-05 against platform.claude.com/docs/en/about-claude/pricing
    # ($1 in / $5 out / $0.10 cache read / $1.25 5m cache write) and
    # /models/overview (200k context, 64k max output)
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
    # --- OpenAI (proves the dataclass is provider-neutral) -----------------
    # verified 2026-07-05 against developers.openai.com/api/docs/models/gpt-4o
    # ($2.50 in / $10 out / $1.25 cached input; 128k context, 16,384 max output)
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
    # verified 2026-07-05 against developers.openai.com/api/docs/models/gpt-4o-mini
    # ($0.15 in / $0.60 out / $0.075 cached input; 128k context, 16,384 max output)
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
    # Reasoning + vision models served over an OpenAI Responses-API gateway.
    # Probed evidence: high effort really emits reasoning (encrypted_content
    # ~21.6KB), and both base64 image-input forms return HTTP 200 with the
    # model actually seeing the image (probe evidence).
    "gpt-5.4-2026-03-05": ModelSpec(
        real_model_id="gpt-5.4-2026-03-05",
        # Internal-gateway model: pricing unpublished — all rates are 0.0, so
        # cost accounting reports $0 for this model (ModelSpec has no
        # unknown-price representation; price() multiplies the rates as-is).
        # context_window / max_output_tokens are gateway placeholders
        # (128k / 16k) — unconfirmed; the gateway has not published limits.
        context_window=128_000,  # placeholder — unconfirmed by gateway
        max_output_tokens=16_384,  # placeholder — unconfirmed by gateway
        input_price_per_mtok=0.0,  # unpublished — reports $0
        output_price_per_mtok=0.0,  # unpublished — reports $0
        cache_read_price_per_mtok=0.0,  # unpublished — reports $0
        cache_write_price_per_mtok=0.0,  # unpublished — reports $0
        is_reasoning=True,
        supports_vision=True,
    ),
    # The next-gen GPT (gpt-5.5) on the same aidp Responses gateway. Like
    # gpt-5.4, pricing is unpublished — all rates are 0.0 and cost accounting
    # reports $0 for this model. Registering it in the catalog is what keeps
    # price() from raising KeyError and lets the vision guard recognise it can
    # read images (otherwise text-only would run but image chains would be
    # blocked). ``context_window`` confirmed 200k per the gateway — drives the
    # compaction window + tail budget (a placeholder that is too small starves
    # the verbatim window and forces tool re-reads).
    "gpt-5.5-2026-04-24": ModelSpec(
        real_model_id="gpt-5.5-2026-04-24",
        context_window=200_000,
        max_output_tokens=16_384,  # placeholder — unconfirmed by gateway
        input_price_per_mtok=0.0,  # unpublished — reports $0
        output_price_per_mtok=0.0,  # unpublished — reports $0
        cache_read_price_per_mtok=0.0,  # unpublished — reports $0
        cache_write_price_per_mtok=0.0,  # unpublished — reports $0
        is_reasoning=True,
        supports_vision=True,
    ),
}


# Alias → real model-id (D-C3). The driver/runner allowlist still gates which
# selectors a principal may bind; this table only translates the friendly name.
ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}


def resolve_alias(selector: str) -> str:
    """Map a friendly alias (``opus``/``sonnet``/``haiku``) to its real
    model-id; pass any non-alias value through unchanged.

    Pass-through is deliberate: a real model-id, or the test-only
    ``stub-model``, stays as-is so the stub path and per-turn switches to
    concrete ids keep working. Authorisation (allowlist ∩ principal) is the
    driver's job, not this table's.
    """
    return ALIASES.get(selector, selector)


def spec_for(model_id: str) -> ModelSpec:
    """Return the :class:`ModelSpec` for a **real** model-id.

    Raises :class:`KeyError` for an unknown id (fixed semantics) so a mis-typed or
    unpriced model surfaces loudly rather than silently costing $0. A friendly
    alias is resolved first (like ``provider_family``), so callers may pass
    either a real id or an alias.
    """
    return CATALOG[resolve_alias(model_id)]


def provider_family(model: str) -> str | None:
    """Classify a model selector into a vendor *family* — ``"anthropic"``,
    ``"openai"``, or ``None`` for anything not recognised.

    This is the **only** model→family judgment in the codebase; the
    assembly layer (``noeta.execution.builder``) consults it to pick the
    provider-appropriate edit tool (``edit`` for Anthropic,
    ``apply_patch`` for OpenAI/GPT) WITHOUT writing the difference into any
    tool field or prompt. Provider-neutral by construction: the family is
    derived from the **catalogued** model's real-id prefix (after
    ``resolve_alias``), not from a vendor wire key or a tool attribute.

    The classification is gated on **catalog membership** so only a real,
    registered model ever switches the tool set:

    * a catalogued model whose real id starts with ``claude`` →
      ``"anthropic"``;
    * a catalogued model whose real id starts with ``gpt`` → ``"openai"``;
    * anything not in the catalog (or a catalogued id with neither prefix)
      → ``None``.

    Returning ``None`` for an unrecognised selector is load-bearing: every
    test/stub sentinel (``gpt-test``, ``stub-model``, ``test-model``,
    uncatalogued ``claude-sonnet-4-5``) is NOT in the catalog, so it
    classifies ``None``. Callers treat ``None`` as "do not filter" — both
    edit variants stay in the tool set, so an uncatalogued selector never
    loses a tool and the prompt's tool list stays unchanged for existing
    sessions.
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
    """Cost in USD for one round-trip's typed :class:`Usage` on ``model_id``.

    Each token bucket is multiplied by its own per-MTok rate: fresh input
    (``uncached``), cache read, cache write, and output are priced
    independently (the cache buckets differ from fresh input — Foundation-A's
    split is what makes this possible). ``reasoning_tokens`` are already part of
    ``output`` (they are hidden completion tokens billed at the output
    rate), so they are not added again. A friendly alias is resolved first
    (like ``provider_family``), so callers may price by either a real id or an
    alias. Raises :class:`KeyError` for an unknown model.
    """
    spec = CATALOG[resolve_alias(model_id)]
    return (
        usage.uncached / 1_000_000 * spec.input_price_per_mtok
        + usage.cache_read / 1_000_000 * spec.cache_read_price_per_mtok
        + usage.cache_write / 1_000_000 * spec.cache_write_price_per_mtok
        + usage.output / 1_000_000 * spec.output_price_per_mtok
    )
