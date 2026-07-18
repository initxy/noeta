"""Multi-gateway routing provider unit tests.

Covers: dispatch by request.model to different sub-providers with the
per-gateway header rewrite applied only to the route that registered one,
build_provider returning a RoutingProvider when the secondary gateway is
configured and building the mapping from models.json's gateway field, and
falling back to the single openai provider when unset. Sub-providers are
fakes; nothing goes over the network.

Note: the target registers no concrete header transform (both gateways accept
the same x-session-id header — see providers.py), so the transform tests here
use a local stand-in to keep the RoutingProvider mechanism covered.
"""
from __future__ import annotations

import json

from noeta.protocols.messages import (
    HeaderAwareProvider,
    LLMRequest,
    LLMResponse,
    StreamingProvider,
    TextBlock,
)

from noeta.agent.host.routing_provider import RoutingProvider


def _req(model: str) -> LLMRequest:
    return LLMRequest(model=model, messages=[])


def _resp(tag: str) -> LLMResponse:
    return LLMResponse(stop_reason="end_turn", content=[TextBlock(text=tag)])


class _FakeSub:
    """Records how it was called + the headers received; the body carries its
    own tag so assertions can tell who handled the request."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.calls: list[tuple[str, dict | None]] = []

    def complete(self, request):
        self.calls.append(("complete", None))
        return _resp(self.tag)

    def complete_with_headers(self, request, headers):
        self.calls.append(("headers", headers))
        return _resp(self.tag)

    def complete_streaming(self, request, on_delta, headers=None):
        self.calls.append(("stream", headers))
        return _resp(self.tag)


def _mark_transform(headers):
    """Local stand-in for a per-gateway header rewrite."""
    out = dict(headers or {})
    out["x-transformed"] = "yes"
    return out


# --------------------------------------------------------------------------
# routing dispatch
# --------------------------------------------------------------------------
def _routing():
    primary, secondary = _FakeSub("openai"), _FakeSub("secondary")
    rp = RoutingProvider(
        {"openai": (primary, None), "secondary": (secondary, _mark_transform)},
        default_gateway="openai",
    )
    rp.register_model("gpt-x", "openai")
    rp.register_model("sec-model", "secondary")
    return rp, primary, secondary


def test_dispatch_by_model():
    rp, primary, secondary = _routing()
    assert rp.complete(_req("gpt-x")).content[0].text == "openai"
    assert rp.complete(_req("sec-model")).content[0].text == "secondary"
    # Unregistered models fall back to the default (openai)
    assert rp.complete(_req("unknown")).content[0].text == "openai"


def test_streaming_transforms_headers_only_for_secondary():
    rp, primary, secondary = _routing()
    hdr = {"x-session-id": "t1"}
    rp.complete_streaming(_req("sec-model"), lambda d: None, hdr)
    rp.complete_streaming(_req("gpt-x"), lambda d: None, hdr)
    # secondary route: the registered transform rewrote the headers
    assert secondary.calls == [
        ("stream", {"x-session-id": "t1", "x-transformed": "yes"})
    ]
    # openai route: headers pass through untouched (no transform)
    assert primary.calls == [("stream", hdr)]


def test_header_path_routes_and_transforms():
    rp, _primary, secondary = _routing()
    rp.complete_with_headers(_req("sec-model"), {"x-session-id": "s"})
    assert secondary.calls == [
        ("headers", {"x-session-id": "s", "x-transformed": "yes"})
    ]


def test_unknown_gateway_falls_back_to_default():
    rp, _primary, _secondary = _routing()
    rp.register_model("weird", "no-such-gateway")
    assert rp.complete(_req("weird")).content[0].text == "openai"


def test_routing_provider_is_streaming_and_header_aware():
    rp, _p, _s = _routing()
    # The runtime probes capabilities via isinstance — with all three methods
    # present, both optional protocols match
    assert isinstance(rp, StreamingProvider)
    assert isinstance(rp, HeaderAwareProvider)


# --------------------------------------------------------------------------
# build_provider assembly
# --------------------------------------------------------------------------
def _models_file(tmp_path):
    p = tmp_path / "models.json"
    p.write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-x", "label": "GPT X", "default": True},
                    {
                        "id": "custom/60b-sota",
                        "label": "Custom 60B",
                        "gateway": "secondary",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return p


def _settings(tmp_path, **over):
    from noeta.agent.config import Settings

    base = dict(
        llm_provider="auto",
        llm_base_url="https://gateway.test/api",
        llm_api_key="gw-key",
        # Explicitly cleared to isolate from the developer's .env (a secondary
        # gateway may really be configured on the dev machine)
        secondary_llm_base_url="",
        secondary_llm_api_key="",
        models_config=str(_models_file(tmp_path)),
    )
    base.update(over)
    return Settings(**base)


def test_build_provider_routing_when_secondary_configured(tmp_path):
    from noeta.agent.host.providers import build_provider

    settings = _settings(
        tmp_path,
        secondary_llm_base_url="https://secondary.test/v1",
        secondary_llm_api_key="plat-xyz",
    )
    provider, name = build_provider(settings)

    assert name == "openai"  # name semantics unchanged (provider_headers gate / /health)
    assert isinstance(provider, RoutingProvider)
    assert provider._model_to_gateway == {
        "gpt-x": "openai",
        "custom/60b-sota": "secondary",
    }
    # The secondary sub-provider points at the secondary gateway's /responses
    secondary_provider = provider._routes["secondary"][0]
    assert secondary_provider._endpoint.endswith("secondary.test/v1/responses")


def test_build_provider_single_openai_when_secondary_unset(tmp_path):
    from noeta.agent.host.providers import build_provider

    settings = _settings(tmp_path)  # no secondary gateway credentials
    provider, name = build_provider(settings)
    assert name == "openai"
    assert not isinstance(provider, RoutingProvider)


def test_build_provider_mock_without_primary(tmp_path):
    from noeta.agent.host.providers import build_provider

    settings = _settings(
        tmp_path,
        llm_base_url="",
        llm_api_key="",
        secondary_llm_base_url="https://secondary.test/v1",
        secondary_llm_api_key="x",
    )
    provider, name = build_provider(settings)
    assert name == "mock"
    assert not isinstance(provider, RoutingProvider)


# --------------------------------------------------------------------------
# ModelSpec catalog registration (enables context compaction)
# --------------------------------------------------------------------------
def _models_file_with_window(tmp_path, entries):
    p = tmp_path / "models.json"
    p.write_text(json.dumps({"models": entries}), encoding="utf-8")
    return p


def _settings_models(tmp_path, entries, **over):
    from noeta.agent.config import Settings

    base = dict(
        llm_provider="auto",
        llm_base_url="https://gateway.test/api",
        llm_api_key="gw-key",
        secondary_llm_base_url="",
        secondary_llm_api_key="",
        models_config=str(_models_file_with_window(tmp_path, entries)),
    )
    base.update(over)
    return Settings(**base)


def test_build_provider_registers_compaction_spec(tmp_path):
    """A custom-gateway model declaring context_window is added to the SDK
    catalog, and derive_compaction_config then returns a "compaction on"
    config for it (otherwise COMPACTION_OFF)."""
    from noeta.agent.host.providers import build_provider
    from noeta.execution.builder import derive_compaction_config
    from noeta.providers.catalog import CATALOG

    mid = "custom/60b-sota"
    had = mid in CATALOG
    original = CATALOG.get(mid)
    try:
        settings = _settings_models(
            tmp_path,
            [
                {"id": "gpt-x", "label": "GPT X", "default": True},
                {
                    "id": mid,
                    "label": "Custom 60B",
                    "gateway": "secondary",
                    "context_window": 200000,
                    "max_output_tokens": 32000,
                },
            ],
            secondary_llm_base_url="https://secondary.test/v1",
            secondary_llm_api_key="plat-xyz",
        )
        build_provider(settings)

        assert mid in CATALOG
        assert CATALOG[mid].context_window == 200000
        # Key assertion: compaction flips from "off" to "on" — context_window
        # is no longer None, and a non-empty protected tail window is reserved
        # (usable window = 200000 - 32000 - buffer, then 1/3).
        cfg = derive_compaction_config(mid)
        assert cfg.context_window == 200000
        assert cfg.max_output_tokens == 32000
        assert cfg.tail_token_budget and cfg.tail_token_budget > 0
    finally:
        if had:
            CATALOG[mid] = original
        else:
            CATALOG.pop(mid, None)


def test_build_provider_does_not_clobber_existing_catalog_row(tmp_path):
    """If models.json declares context_window for a model already in the SDK
    catalog, the SDK's authoritative row is not overridden (only missing
    models are filled in)."""
    from noeta.agent.host.providers import build_provider
    from noeta.providers.catalog import CATALOG

    existing = "gpt-5.5-2026-04-24"  # authoritative row shipped by the SDK
    assert existing in CATALOG
    before = CATALOG[existing]
    settings = _settings_models(
        tmp_path,
        [
            {
                "id": existing,
                "label": "GPT 5.5",
                "default": True,
                "context_window": 123,  # deliberately bogus; must not be adopted
            }
        ],
    )
    build_provider(settings)
    assert CATALOG[existing] is before  # untouched


def test_build_provider_skips_models_without_window(tmp_path):
    """Models that do not declare context_window stay out of the catalog
    (legacy behavior preserved: compaction off, same as unknown non-GPT
    models)."""
    from noeta.agent.host.providers import build_provider
    from noeta.providers.catalog import CATALOG

    mid = "custom/nospec"
    had = mid in CATALOG
    try:
        settings = _settings_models(
            tmp_path,
            [
                {"id": "gpt-x", "label": "GPT X", "default": True},
                {"id": mid, "label": "NoSpec", "gateway": "secondary"},
            ],
            secondary_llm_base_url="https://secondary.test/v1",
            secondary_llm_api_key="plat-xyz",
        )
        build_provider(settings)
        assert mid not in CATALOG
    finally:
        if not had:
            CATALOG.pop(mid, None)
