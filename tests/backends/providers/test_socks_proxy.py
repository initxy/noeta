"""A SOCKS proxy in the environment (``ALL_PROXY=socks5://…``, as Clash and
similar tools set it) does not stop a model adapter from starting: httpx
needs ``socksio`` for it, which noeta's ``httpx[socks]`` brings."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from noeta.backends.providers.anthropic import AnthropicProvider
from noeta.backends.providers.openai_compat import OpenAICompatProvider
from noeta.backends.providers.openai_responses import OpenAIResponsesProvider


URL = "https://models.example.com"
ADAPTERS: list[Callable[[], Any]] = [
    lambda: AnthropicProvider("key", base_url=URL),
    lambda: OpenAICompatProvider(URL, "key"),
    lambda: OpenAIResponsesProvider(URL, "key"),
]


@pytest.mark.parametrize("make", ADAPTERS, ids=["anthropic", "openai_compat", "responses"])
async def test_an_adapter_starts_behind_a_socks_proxy(
        make: Callable[[], Any], monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "socks5://127.0.0.1:7897")
    provider = make()
    await provider._client.aclose()
