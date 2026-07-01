"""The new backend's late-bound image resolver (image-input T3).

The vision-capable ``openai-responses`` adapter needs a ``ContentRef → bytes``
resolver at construction, but the content store only exists once the engine room
is built (the provider is an argument to it). ``_LateImageResolver`` is handed to
the provider up front and bound after — so a vision request can deref an
``ImageBlock`` instead of the adapter raising "no image_resolver configured".
"""

from __future__ import annotations

import pytest

from noeta.agent.backend import BackendConfig
from noeta.agent.backend.lifecycle import _LateImageResolver, build_provider
from noeta.sdk import ContentRef


def _ref(h: str = "abc123") -> ContentRef:
    return ContentRef(hash=h, size=3, media_type="image/png")


def test_resolver_raises_before_bind() -> None:
    box = _LateImageResolver()
    with pytest.raises(RuntimeError):
        box(_ref())


def test_resolver_returns_bytes_after_bind() -> None:
    store = {"abc123": b"PNGBYTES"}
    box = _LateImageResolver()
    box.bind(lambda h: store.get(h))

    assert box(_ref("abc123")) == b"PNGBYTES"


def test_resolver_raises_on_missing_content() -> None:
    box = _LateImageResolver()
    box.bind(lambda h: None)  # engine room has no bytes for this hash
    with pytest.raises(LookupError):
        box(_ref("missing"))


def test_build_provider_injects_resolver_into_responses_adapter() -> None:
    box = _LateImageResolver()
    provider = build_provider(
        BackendConfig(
            provider_id="openai-responses",
            base_url="https://aidp/responses",
            api_key="K",
        ),
        image_resolver=box,
    )
    assert type(provider).__name__ == "OpenAIResponsesProvider"
    # The adapter holds exactly the box we passed, so binding it later wires the
    # whole deref path.
    assert provider._image_resolver is box


def test_build_provider_resolver_is_optional_for_non_vision() -> None:
    # The non-vision adapter ignores the resolver (rejects ImageBlock by design).
    provider = build_provider(
        BackendConfig(provider_id="openai", base_url="https://ark/v3", api_key="K"),
        image_resolver=_LateImageResolver(),
    )
    assert type(provider).__name__ == "OpenAICompatProvider"
