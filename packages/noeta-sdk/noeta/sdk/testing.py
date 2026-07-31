"""``noeta.sdk.testing`` — test doubles for SDK consumers.

:class:`FakeLLMProvider` is the deterministic, network-free provider a product
drives in its offline suite and demo modes — the same double the engine's own
tests run on; :class:`FakeStreamingLLMProvider` is its streaming twin, the one a
host needs to exercise a token-streaming wire (``StreamDelta`` → SSE) without a
network. Both sit in a submodule rather than the ``noeta.sdk`` root so
production imports never pull test material in by accident.
"""

from __future__ import annotations

from noeta.testing.fake_llm import FakeLLMProvider, FakeStreamingLLMProvider


__all__ = ["FakeLLMProvider", "FakeStreamingLLMProvider"]
