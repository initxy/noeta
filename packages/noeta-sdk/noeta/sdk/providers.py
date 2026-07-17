"""``noeta.sdk.providers`` — the official LLM provider adapters, re-exported.

The app layer (and the new backend) is provider-neutral but must construct ONE
concrete provider to inject (``Client(provider=…)`` / ``serve_backend``). The
adapters live in ``noeta.providers`` (noeta-runtime); routing the import through
``noeta.sdk`` keeps the encapsulation weld intact — the backend reaches the
engine only through ``noeta.sdk``, and ``noeta.sdk`` sits above ``noeta.providers``
in the layer order.

Kept in this submodule (not the ``noeta.sdk`` root) so importing the SDK stays
light: only callers that actually build a network provider pull ``httpx`` in.

    from noeta.sdk.providers import OpenAICompatProvider      # chat-completions gateways
    from noeta.sdk.providers import OpenAIResponsesProvider   # responses-API gateways
    from noeta.sdk.providers import AnthropicProvider

The model catalog (``ModelSpec`` rows keyed by model id in ``CATALOG``) is the
read-only companion surface: a product that lets users pick models needs the
specs (context window, output cap, pricing) for the models it wires up.
"""

from __future__ import annotations

from noeta.providers.anthropic import AnthropicProvider
from noeta.providers.catalog import CATALOG, ModelSpec
from noeta.providers.openai_compat import OpenAICompatProvider
from noeta.providers.openai_responses import OpenAIResponsesProvider


__all__ = [
    "OpenAICompatProvider",
    "OpenAIResponsesProvider",
    "AnthropicProvider",
    "CATALOG",
    "ModelSpec",
]
