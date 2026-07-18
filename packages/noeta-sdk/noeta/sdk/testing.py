"""``noeta.sdk.testing`` — test doubles for SDK consumers.

A product embedding the SDK needs a deterministic, network-free provider for
its offline suite and demo modes. :class:`FakeLLMProvider` is the same double
the engine's own tests run on, re-exported so a product reaches it through
``noeta.sdk`` instead of the runtime-internal ``noeta.testing`` package.

Kept in a submodule (not the ``noeta.sdk`` root) so production imports never
pull test material by accident::

    from noeta.sdk.testing import FakeLLMProvider
"""

from __future__ import annotations

from noeta.testing.fake_llm import FakeLLMProvider


__all__ = ["FakeLLMProvider"]
