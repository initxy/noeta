"""``noeta.sdk.providers`` — the official LLM provider adapters, re-exported.

The app layer (and the new backend) is provider-neutral but must construct ONE
concrete provider to inject (``Client(provider=…)`` / ``serve_backend``). The
adapters live in ``noeta.providers`` (noeta-runtime); routing the import through
``noeta.sdk`` keeps the encapsulation weld intact — the backend reaches the
engine only through ``noeta.sdk``, and ``noeta.sdk`` sits above ``noeta.providers``
in the layer order.

Kept in this submodule (not the ``noeta.sdk`` root) so importing the SDK stays
light: only callers that actually build a network provider pull ``httpx`` in.

    from noeta.sdk.providers import OpenAICompatProvider      # ByteDance Ark, etc.
    from noeta.sdk.providers import OpenAIResponsesProvider   # AIDP / Azure-style
    from noeta.sdk.providers import AnthropicProvider
"""

from __future__ import annotations

from noeta.providers.anthropic import AnthropicProvider
from noeta.providers.openai_compat import OpenAICompatProvider
from noeta.providers.openai_responses import OpenAIResponsesProvider


__all__ = [
    "OpenAICompatProvider",
    "OpenAIResponsesProvider",
    "AnthropicProvider",
]
