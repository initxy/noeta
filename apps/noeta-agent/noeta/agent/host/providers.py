"""LLM provider assembly: openai (an OpenAI Responses-compatible gateway) or
mock (FakeLLM); when the secondary gateway is configured, the two are stacked
into a RoutingProvider (per-model dispatch across gateways).

Verified in a spike: OpenAIResponsesProvider's base_url must be the full
responses endpoint (gateway root + '/responses'), and auth goes through the
api-key header. The secondary gateway speaks the same OpenAI Responses
protocol and differs only in host and auth (Authorization: Bearer), so the
same adapter is reused with a different base_url + the Bearer header injected
via extra_headers (with api-key left empty).
"""
from __future__ import annotations

import logging
from typing import Any

from noeta.agent.config import Settings

logger = logging.getLogger(__name__)


def _build_openai(settings: Settings) -> Any:
    """Primary-gateway Responses provider (api-key header)."""
    from noeta.sdk.providers import OpenAIResponsesProvider

    base = settings.llm_base_url.rstrip("/") + "/responses"
    return OpenAIResponsesProvider(
        base_url=base,
        api_key=settings.llm_api_key,
        timeout_seconds=settings.llm_request_timeout,
    )


def _build_secondary(settings: Settings) -> Any:
    """Secondary-gateway Responses provider: Bearer auth + reasoning
    continuation off.

    - api_key is left empty (the adapter still sends an ``api-key: ""``
      header; the gateway authenticates on Authorization and ignores the empty
      api-key); the real credential goes through extra_headers as
      ``Authorization: Bearer``.
    - reasoning_continuation="off": do not send back
      ``include:[reasoning.encrypted_content]``. ``reasoning.effort`` itself is
      verified to work against the secondary gateway (low/medium/high all
      return 200 and produce reasoning segments at the requested level), so
      models.json enables low/medium/high for the models it serves; but
      cross-turn replay of encrypted reasoning (the ``include`` parameter) is
      unverified and models outside the primary gateway's catalog may not
      accept it — turning it off is the safe choice: each turn carries its own
      effort independently, losing only cross-turn reasoning reuse.
    """
    from noeta.sdk.providers import OpenAIResponsesProvider

    base = settings.secondary_llm_base_url.rstrip("/") + "/responses"
    return OpenAIResponsesProvider(
        base_url=base,
        api_key="",
        timeout_seconds=settings.llm_request_timeout,
        extra_headers={"Authorization": f"Bearer {settings.secondary_llm_api_key}"},
        reasoning_continuation="off",
    )


def _register_catalog_specs(models: list["ModelDef"]) -> None:
    """Register the models.json entries that declare a context_window into
    noeta's ModelSpec catalog, so the SDK's ``derive_compaction_config`` sees
    the window and enables context compaction for that model (the composer's
    prune + the policy's summarize).

    Background: noeta ``derive_compaction_config(model)`` goes through
    ``spec_for``; a model missing from the catalog returns ``COMPACTION_OFF``
    (context_window=None ⇒ compaction disabled entirely). The GPT series ships
    authoritative rows in the SDK and gets compaction as usual; but
    custom-gateway models the SDK does not know have no spec, so the context
    only ever grows, eventually pushing the model into repeatedly emitting
    truncated tool calls → llm_error (observed in a production trace).

    Only fills models absent from the catalog (``id not in CATALOG``): never
    overrides the SDK's authoritative rows. Prices are all 0 (internal
    gateways have no public pricing, matching the SDK's gpt-5.4/5.5 rows; the
    host layer's ``KeyError→0.0`` already keeps cost accounting from
    regressing). ``supports_vision=False`` keeps the status quo (an
    unregistered model is treated as text-only by the vision guard; this
    change does not touch vision behavior). ``real_model_id`` is the id itself
    (it does not start with claude/gpt → ``provider_family`` still resolves to
    None → the tool surface is unchanged).
    """
    from noeta.providers.catalog import CATALOG, ModelSpec

    for m in models:
        if m.context_window is None or m.id in CATALOG:
            continue
        CATALOG[m.id] = ModelSpec(
            real_model_id=m.id,
            context_window=m.context_window,
            max_output_tokens=m.max_output_tokens or 0,
            input_price_per_mtok=0.0,
            output_price_per_mtok=0.0,
            cache_read_price_per_mtok=0.0,
            cache_write_price_per_mtok=0.0,
        )


def build_provider(settings: Settings) -> tuple[Any, str]:
    """Build (provider, name) from the configuration. The "auto" fallback
    lives in Settings.effective_provider.

    The returned name is still effective_provider ("openai"/"mock"): both the
    /health semantics and service.py's provider_headers gate (== "openai"
    injects the per-task session header) read it; routing is a transparent
    overlay on top of openai and changes neither. Both gateways accept the
    same ``x-session-id`` session header, so no per-gateway header transform
    is registered.
    """
    effective = settings.effective_provider
    if effective != "openai":
        from noeta.agent.host.mock_llm import build_mock_provider

        logger.info("LLM provider: mock (FakeLLM, offline)")
        return build_mock_provider(), "mock"

    from noeta.agent.models_config import get_models

    models = get_models(settings)
    # First register custom-gateway models into the SDK catalog to enable
    # context compaction (see _register_catalog_specs). Independent of whether
    # the second gateway is fully configured: the GPT series is already in the
    # catalog, and secondary-gateway models deserve a spec whether or not
    # routing is active.
    _register_catalog_specs(models)

    primary = _build_openai(settings)
    if not settings.secondary_gateway_configured:
        logger.info(
            "LLM provider: openai (Responses) endpoint=%s",
            settings.llm_base_url.rstrip("/") + "/responses",
        )
        return primary, "openai"

    # Secondary gateway ready: route by the models.json gateway field.
    from noeta.agent.host.routing_provider import RoutingProvider

    routes = {
        "openai": (primary, None),
        "secondary": (_build_secondary(settings), None),
    }
    provider = RoutingProvider(routes, default_gateway="openai")
    for m in models:
        provider.register_model(m.id, m.gateway)
    logger.info(
        "LLM provider: routing (openai + secondary) secondary_endpoint=%s models=%s",
        settings.secondary_llm_base_url.rstrip("/") + "/responses",
        {m.id: m.gateway for m in models},
    )
    return provider, "openai"
