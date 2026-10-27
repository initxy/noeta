"""Provider-neutral model catalog: one :class:`ModelSpec` per model, and the
pricing built on it.

Anthropic and OpenAI rows share the same dataclass and no vendor wire key
(``cache_creation_input_tokens`` / ``prompt_tokens`` / ``total_tokens``) is
ever a field name here — each adapter has already mapped its wire usage into
the neutral :class:`~noeta.types.Usage` that :func:`price` charges. Prices are
USD per 1,000,000 tokens.

**An unknown model is a warning, never a silent default.** A selector this
table does not carry used to degrade several ways at once — ``$0`` cost, a
hard image refusal, compaction switched off — without a single log line. The
catalog logs once per model id on each of those paths and answers with a
conservative default instead (:data:`UNKNOWN_MODEL_CONTEXT_WINDOW` /
:data:`UNKNOWN_MODEL_MAX_OUTPUT_TOKENS`), so a gateway model nobody catalogued
still compacts and still bills visibly-as-unknown. The warn-once bookkeeping is
process-global and touches no returned value.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass

from noeta.types import Usage

__all__ = [
    "ALIASES",
    "CATALOG",
    "UNKNOWN_MODEL_CONTEXT_WINDOW",
    "UNKNOWN_MODEL_MAX_OUTPUT_TOKENS",
    "ModelSpec",
    "catalog_models",
    "find_spec",
    "model_family",
    "model_limits",
    "price",
    "register_models",
    "resolve_alias",
    "spec_for",
]


_LOGGER = logging.getLogger(__name__)

#: Model ids already warned about, keyed by ``(concern, model_id)`` so each
#: concern gets one line per model rather than one line per round-trip.
#: Diagnostics only: nothing reads it back to decide a value.
_WARNED: set[tuple[str, str]] = set()


def _warn_once(concern: str, model_id: str, message: str) -> None:
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

    **Absent pricing is a state, not a zero.** Every price field is optional:
    a row served by a gateway that publishes no rate card leaves them ``None``,
    which :func:`price` reports as unknown (warn once, charge 0.0) instead of
    quietly asserting the model is free. A literal ``0.0`` means "genuinely
    free", and only that.
    """

    real_model_id: str
    context_window: int
    max_output_tokens: int
    input_price_per_mtok: float | None = None
    output_price_per_mtok: float | None = None
    cache_read_price_per_mtok: float | None = None
    cache_write_price_per_mtok: float | None = None
    is_reasoning: bool = False
    #: Defaults to False so a model nobody marked as vision-capable is treated
    #: as text-only. Note the asymmetry the adapters implement: a CATALOGUED
    #: ``supports_vision=False`` is a decision and their vision guard refuses
    #: the image up front, while a model absent from the table is merely
    #: *unknown* and images are let through for the provider to judge.
    supports_vision: bool = False
    #: Explicit vendor family (``"anthropic"`` / ``"openai"``) for a row whose
    #: real id carries no recognisable prefix — a gateway routing name that IS
    #: a Claude behind a relay declares it here. Consumed by
    #: :func:`model_family` ahead of prefix inference and validated at
    #: registration. ``None`` (every shipped row) falls back to real-id prefix
    #: inference. This is the *vendor*, not the wire an adapter speaks
    #: (``Provider.family``).
    family: str | None = None

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
        cache_read_price_per_mtok=0.50,  # ≈ 0.1x input
        cache_write_price_per_mtok=6.25,  # ≈ 1.25x input (5-min TTL)
        is_reasoning=True,
        supports_vision=True,
    ),
    "claude-sonnet-5": ModelSpec(
        real_model_id="claude-sonnet-5",
        context_window=1_000_000,
        max_output_tokens=128_000,
        # Standard rates. An introductory $2.00 / $10.00 ran to 2026-08-31;
        # this table has no date dimension, so it carries the rate that
        # outlives the promotion — over-reporting cost is the safe direction
        # for a budget ceiling.
        input_price_per_mtok=3.00,
        output_price_per_mtok=15.00,
        cache_read_price_per_mtok=0.30,  # ≈ 0.1x input
        cache_write_price_per_mtok=3.75,  # ≈ 1.25x input (5-min TTL)
        is_reasoning=True,
        supports_vision=True,
    ),
    "claude-opus-4-8": ModelSpec(
        real_model_id="claude-opus-4-8",
        context_window=1_000_000,
        max_output_tokens=128_000,
        input_price_per_mtok=5.00,
        output_price_per_mtok=25.00,
        cache_read_price_per_mtok=0.50,  # ≈ 0.1x input
        cache_write_price_per_mtok=6.25,  # ≈ 1.25x input (5-min TTL)
        is_reasoning=True,
        supports_vision=True,
    ),
    "claude-sonnet-4-6": ModelSpec(
        real_model_id="claude-sonnet-4-6",
        context_window=1_000_000,
        max_output_tokens=128_000,
        input_price_per_mtok=3.00,
        output_price_per_mtok=15.00,
        cache_read_price_per_mtok=0.30,  # ≈ 0.1x input
        cache_write_price_per_mtok=3.75,  # ≈ 1.25x input (5-min TTL)
        is_reasoning=True,
        supports_vision=True,
    ),
    "claude-haiku-4-5": ModelSpec(
        real_model_id="claude-haiku-4-5",
        context_window=200_000,
        max_output_tokens=64_000,
        input_price_per_mtok=1.00,
        output_price_per_mtok=5.00,
        cache_read_price_per_mtok=0.10,  # ≈ 0.1x input
        cache_write_price_per_mtok=1.25,  # ≈ 1.25x input (5-min TTL)
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
        cache_read_price_per_mtok=1.25,  # OpenAI cached input ≈ 0.5x input
        cache_write_price_per_mtok=2.50,  # OpenAI has no write tier → = input
        is_reasoning=False,
        supports_vision=True,
    ),
    "gpt-4o-mini": ModelSpec(
        real_model_id="gpt-4o-mini",
        context_window=128_000,
        max_output_tokens=16_384,
        input_price_per_mtok=0.15,
        output_price_per_mtok=0.60,
        cache_read_price_per_mtok=0.075,  # OpenAI cached input ≈ 0.5x input
        cache_write_price_per_mtok=0.15,  # no write tier → = input
        is_reasoning=False,
        supports_vision=True,
    ),
    # --- OpenAI Responses gateway models (reasoning + vision) ---------------
    # Served by an internal gateway that publishes no pricing, so every rate is
    # left ABSENT (``None``) rather than faked as 0.0: price() then reports
    # them as unknown-and-warned instead of asserting they are free. They are
    # catalogued anyway, because a row is what carries the window/output knobs
    # and the vision capability bit. Anything marked "placeholder" is a guess
    # the gateway has not confirmed; a context window guessed too small starves
    # the verbatim tail and forces the model to re-read files through tools.
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


#: Alias → real model id. The aliases track the CURRENT generation, so a host
#: that binds ``"opus"`` follows the vendor forward without a code change; the
#: superseded rows stay addressable by their full id. There is no haiku 5 row,
#: so ``haiku`` stays on 4.5.
ALIASES: dict[str, str] = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}


#: The window an UNCATALOGUED model is assumed to have. Deliberately at the
#: small end of what any current model ships, because the failure modes are
#: asymmetric: guessing too small compacts earlier than necessary — a bounded
#: cost in fidelity — while guessing too large lets the history run past the
#: real window and the task dies on an overflow nobody anticipated.
UNKNOWN_MODEL_CONTEXT_WINDOW = 128_000
#: Matching conservative output cap. Unlike the window guess this one is not
#: free when it rides ``ModelRequest.max_tokens``: an uncatalogued model then
#: genuinely cannot answer past it. Add a catalog row to lift it.
UNKNOWN_MODEL_MAX_OUTPUT_TOKENS = 16_384


# ---------------------------------------------------------------------------
# Operator extensions. The shipped table above is a transcription of public
# vendor pages; deployments also run models it cannot know — internal gateway
# routing names, self-hosted models. Those rows are HOST WIRING, registered at
# process start, and they live in this overlay so the public table stays a
# pure transcription. Every lookup consults the union via ``find_spec`` /
# ``resolve_alias``. A host must register the SAME extensions on every run;
# there is deliberately no unregister, because removing a row mid-process
# would change the knobs derived for a live task.
# ---------------------------------------------------------------------------

_EXTENSIONS: dict[str, ModelSpec] = {}
_EXTENSION_ALIASES: dict[str, str] = {}
_REGISTER_LOCK = threading.Lock()

#: Families a row may declare explicitly. Validated at registration: a typo'd
#: family silently picking the wrong conventions later is exactly the class of
#: failure this module refuses up front.
_KNOWN_FAMILIES = frozenset({"anthropic", "openai"})


def register_models(
    models: Mapping[str, ModelSpec],
    aliases: Mapping[str, str] | None = None,
) -> None:
    """Register operator model rows (and aliases) into the catalog overlay.

    Collisions refuse loudly: a name already present anywhere — shipped table,
    shipped aliases, registered extensions or extension aliases — or already
    registered with a *different* spec, raises ``ValueError`` naming the id;
    an extension never silently overrides existing data. Re-registering the
    *identical* spec is a no-op, so a process may build several clients from
    the same config. Alias targets are resolved through the shipped aliases
    and must land on a shipped or registered row (rows registered in the same
    call count).

    Registration is all-or-nothing and thread-safe: every row and alias in one
    call is validated first, then committed under a module lock, so a refused
    call leaves the overlay untouched. Every refusal is ``ValueError``.
    """
    alias_items = dict(aliases) if aliases is not None else {}
    with _REGISTER_LOCK:
        staged_models: dict[str, ModelSpec] = {}
        for name, spec in models.items():
            if not isinstance(spec, ModelSpec):
                raise ValueError(
                    f"extra model {name!r}: expected a ModelSpec, got "
                    f"{type(spec).__name__}"
                )
            if not name:
                raise ValueError("extra model id must be a non-empty string")
            if name in CATALOG or name in ALIASES or name in _EXTENSION_ALIASES:
                raise ValueError(
                    f"extra model {name!r} collides with an existing catalog "
                    "row/alias; extensions never override existing data"
                )
            if spec.family is not None and spec.family not in _KNOWN_FAMILIES:
                raise ValueError(
                    f"extra model {name!r}: unknown family {spec.family!r}; "
                    f"expected one of {sorted(_KNOWN_FAMILIES)}"
                )
            existing = _EXTENSIONS.get(name)
            if existing is not None:
                if existing != spec:
                    raise ValueError(
                        f"extra model {name!r} is already registered with a "
                        "different spec; re-register the identical spec or "
                        "pick another name"
                    )
                continue  # identical re-registration — a no-op
            staged_models[name] = spec
        staged_aliases: dict[str, str] = {}
        for alias, target in alias_items.items():
            if not alias:
                raise ValueError("extra alias must be a non-empty string")
            if (
                alias in CATALOG
                or alias in ALIASES
                or alias in _EXTENSIONS
                or alias in staged_models
            ):
                raise ValueError(
                    f"extra alias {alias!r} collides with an existing catalog "
                    "row/alias; extensions never override existing data"
                )
            # Resolve through the shipped aliases so a target may be shipped
            # shorthand ("opus"); the RESOLVED id is stored, keeping
            # ``resolve_alias`` single-hop.
            real_target = ALIASES.get(target, target)
            if (
                real_target not in CATALOG
                and real_target not in _EXTENSIONS
                and real_target not in staged_models
            ):
                raise ValueError(
                    f"extra alias {alias!r} points at unknown model "
                    f"{target!r}; register the ModelSpec row first"
                )
            existing_target = _EXTENSION_ALIASES.get(alias)
            if existing_target is not None:
                if existing_target != real_target:
                    raise ValueError(
                        f"extra alias {alias!r} is already registered for "
                        f"{existing_target!r}"
                    )
                continue
            staged_aliases[alias] = real_target
        _EXTENSIONS.update(staged_models)
        _EXTENSION_ALIASES.update(staged_aliases)


def catalog_models() -> dict[str, ModelSpec]:
    """The merged catalog — shipped rows plus registered extensions (a fresh
    copy; mutating it changes nothing). A host building a model picker lists
    THIS, not ``CATALOG``, which is only the shipped table."""
    return {**CATALOG, **_EXTENSIONS}


def resolve_alias(selector: str) -> str:
    """Map a friendly alias to its real model id, passing anything else
    through unchanged (a real id, or a test sentinel). Shipped aliases win over
    extension aliases by construction — ``register_models`` refuses the
    collision."""
    return ALIASES.get(selector, _EXTENSION_ALIASES.get(selector, selector))


def find_spec(model_id: str) -> ModelSpec | None:
    """The merged-catalog lookup, alias-resolved; ``None`` for an unknown
    selector. Every consumer goes through here so operator extensions are
    visible everywhere the shipped rows are."""
    real = resolve_alias(model_id)
    spec = CATALOG.get(real)
    if spec is None:
        spec = _EXTENSIONS.get(real)
    return spec


def spec_for(model_id: str) -> ModelSpec:
    """Look up a model's spec, by real id or alias. Raises :class:`KeyError`
    for an unknown id so a mis-typed or unpriced model surfaces loudly."""
    spec = find_spec(model_id)
    if spec is None:
        raise KeyError(resolve_alias(model_id))
    return spec


def model_family(model: str) -> str | None:
    """Classify a model selector as ``"anthropic"``, ``"openai"`` or ``None``.

    The family comes from a **catalogued** model's declared ``family``, else
    its real-id prefix, so only a known model classifies at all. ``None`` — an
    uncatalogued selector — means "unknown": callers must not narrow anything
    on it.
    """
    spec = find_spec(model)
    if spec is None:
        return None
    if spec.family is not None:
        return spec.family
    if spec.real_model_id.startswith("claude"):
        return "anthropic"
    if spec.real_model_id.startswith("gpt"):
        return "openai"
    return None


def model_limits(model: str) -> tuple[int, int]:
    """``(context_window, max_output_tokens)`` for ``model``.

    An uncatalogued model gets the conservative
    :data:`UNKNOWN_MODEL_CONTEXT_WINDOW` / :data:`UNKNOWN_MODEL_MAX_OUTPUT_TOKENS`
    pair with one warning per model id — never "no limits", which used to
    switch compaction off for exactly the population most likely to need it
    (a self-hosted or gateway model nobody added a row for). The compaction
    margin itself is the compaction extension's business; this is only the
    data it derives from.
    """
    real = resolve_alias(model)
    spec = find_spec(real)
    if spec is None:
        _warn_once(
            "limits",
            real,
            f"model {real!r} is not in the catalog: assuming a conservative "
            f"{UNKNOWN_MODEL_CONTEXT_WINDOW}-token context window and a "
            f"{UNKNOWN_MODEL_MAX_OUTPUT_TOKENS}-token output cap. Add a "
            "ModelSpec row (noeta.models.register_models) to use the model's "
            "real limits.",
        )
        return UNKNOWN_MODEL_CONTEXT_WINDOW, UNKNOWN_MODEL_MAX_OUTPUT_TOKENS
    return spec.context_window, spec.max_output_tokens


def price(model_id: str, usage: Usage) -> float:
    """Cost in USD for one round-trip's :class:`Usage` on ``model_id``.

    Each token bucket carries its own per-MTok rate. ``reasoning`` tokens are
    deliberately absent from the sum: they are hidden completion tokens already
    counted in ``output`` and billed at the output rate, so adding them would
    double-charge.

    **Unknown price ⇒ warn once, charge 0.0.** Two rows land there: a model
    absent from the catalog, and a catalogued row whose rates the vendor does
    not publish. Returning 0.0 keeps cost folding and budget arithmetic total;
    the warning is what makes the state visible.
    """
    real = resolve_alias(model_id)
    spec = find_spec(real)
    if spec is None:
        _warn_once(
            "pricing",
            real,
            f"model {real!r} is not in the catalog: charging $0.00 for it. "
            "Cost accounting and any cost budget will under-report until a "
            "ModelSpec row is added.",
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
            "charging $0.00 for it. Cost accounting and any cost budget will "
            "under-report until its rates are filled in.",
        )
        return 0.0
    return (
        usage.uncached / 1_000_000 * spec.input_price_per_mtok
        + usage.cache_read / 1_000_000 * spec.cache_read_price_per_mtok
        + usage.cache_write / 1_000_000 * spec.cache_write_price_per_mtok
        + usage.output / 1_000_000 * spec.output_price_per_mtok
    )
