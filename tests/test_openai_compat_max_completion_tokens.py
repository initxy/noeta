"""``OpenAICompatProvider(max_tokens_param=...)``: which body key carries the
output cap.

OpenAI's own API refuses ``max_tokens`` for its reasoning models and wants
``max_completion_tokens``; compatible gateways mostly still read
``max_tokens``. ``"auto"`` decides per request from the catalog; the two
literals force one key.
"""

from __future__ import annotations

from typing import Any

import pytest

from noeta.builtins.providers.impl import catalog
from noeta.builtins.providers.impl.catalog import ModelSpec, register_models
from noeta.builtins.providers.impl.openai_compat import OpenAICompatProvider
from noeta.protocols.messages import LLMRequest, Message, TextBlock


@pytest.fixture(autouse=True)
def _clean_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "_EXTENSIONS", {})
    monkeypatch.setattr(catalog, "_EXTENSION_ALIASES", {})
    monkeypatch.setattr(catalog, "_WARNED", set())


def _body(model: str, **kwargs: Any) -> dict[str, Any]:
    provider = OpenAICompatProvider("https://x/v1", api_key="k", **kwargs)
    request = LLMRequest(
        model=model,
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        max_tokens=77,
    )
    return provider._build_request_body(request)  # noqa: SLF001


def _cap_key(body: dict[str, Any]) -> str:
    keys = {"max_tokens", "max_completion_tokens"} & body.keys()
    assert len(keys) == 1, body
    (key,) = keys
    assert body[key] == 77
    return key


def test_catalogued_openai_reasoning_row_gets_max_completion_tokens() -> None:
    assert catalog.find_spec("gpt-5.5-2026-04-24") is not None
    assert _cap_key(_body("gpt-5.5-2026-04-24")) == "max_completion_tokens"


def test_catalogued_non_reasoning_row_keeps_max_tokens() -> None:
    assert _cap_key(_body("gpt-4o")) == "max_tokens"


def test_catalogued_reasoning_row_of_another_family_keeps_max_tokens() -> None:
    # A Claude row is reasoning but not OpenAI: the key stays ``max_tokens``.
    assert _cap_key(_body("claude-opus-5")) == "max_tokens"


def test_registered_reasoning_row_resolved_through_an_alias() -> None:
    register_models(
        {"o3-relay": ModelSpec(real_model_id="o3-relay", context_window=200_000,
                               max_output_tokens=100_000, is_reasoning=True)},
        aliases={"thinker": "o3-relay"},
    )
    assert _cap_key(_body("thinker")) == "max_completion_tokens"


def test_registered_reasoning_row_declared_openai_family() -> None:
    register_models(
        {"gateway/big": ModelSpec(real_model_id="gateway/big", context_window=200_000,
                                  max_output_tokens=100_000, is_reasoning=True,
                                  provider_family="openai")}
    )
    assert _cap_key(_body("gateway/big")) == "max_completion_tokens"


def test_catalogued_row_that_is_not_reasoning_wins_over_the_id_pattern() -> None:
    register_models(
        {"gpt-5-chat-relay": ModelSpec(real_model_id="gpt-5-chat-relay",
                                       context_window=200_000,
                                       max_output_tokens=16_384,
                                       is_reasoning=False)}
    )
    assert _cap_key(_body("gpt-5-chat-relay")) == "max_tokens"


@pytest.mark.parametrize("model", ["gpt-5-mini-2026-01-01", "o1-preview", "o3", "o4-mini"])
def test_uncatalogued_openai_reasoning_id_gets_max_completion_tokens(model: str) -> None:
    assert catalog.find_spec(model) is None
    assert _cap_key(_body(model)) == "max_completion_tokens"


@pytest.mark.parametrize("model", ["deepseek-reasoner", "llama-3-70b", "gpt-4.1", "qwen-o3"])
def test_uncatalogued_other_id_keeps_max_tokens(model: str) -> None:
    assert _cap_key(_body(model)) == "max_tokens"


@pytest.mark.parametrize("model", ["gpt-5.5-2026-04-24", "gpt-4o", "o3", "llama-3-70b"])
def test_forced_max_tokens(model: str) -> None:
    assert _cap_key(_body(model, max_tokens_param="max_tokens")) == "max_tokens"


@pytest.mark.parametrize("model", ["gpt-5.5-2026-04-24", "gpt-4o", "o3", "llama-3-70b"])
def test_forced_max_completion_tokens(model: str) -> None:
    body = _body(model, max_tokens_param="max_completion_tokens")
    assert _cap_key(body) == "max_completion_tokens"


def test_no_cap_sends_neither_key() -> None:
    provider = OpenAICompatProvider("https://x/v1", api_key="k")
    request = LLMRequest(
        model="o3",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
    )
    body = provider._build_request_body(request)  # noqa: SLF001
    assert "max_tokens" not in body and "max_completion_tokens" not in body


def test_unknown_setting_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="max_tokens_param"):
        OpenAICompatProvider("https://x/v1", api_key="k", max_tokens_param="max_output_tokens")  # type: ignore[arg-type]
