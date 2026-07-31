"""``noeta.sdk.testing`` — test doubles for SDK consumers.

:class:`FakeLLMProvider` is the deterministic, network-free provider a product
drives in its offline suite and demo modes — the same double the engine's own
tests run on. It sits in a submodule rather than the ``noeta.sdk`` root so
production imports never pull test material in by accident.
"""

from __future__ import annotations

from noeta.testing.fake_llm import FakeLLMProvider


__all__ = ["FakeLLMProvider"]
