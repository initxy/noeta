"""``noeta.sdk.providers`` — the official LLM provider adapters and model catalog.

A host is provider-neutral but must construct ONE concrete provider to inject
(``Client(provider=…)``), and ``CATALOG`` carries the ``ModelSpec`` row (context
window, output cap, pricing) for each model it may offer::

    from noeta.sdk.providers import OpenAICompatProvider      # chat-completions gateways
    from noeta.sdk.providers import OpenAIResponsesProvider   # responses-API gateways
    from noeta.sdk.providers import AnthropicProvider

The adapters live in the ``providers`` built-in and are re-exported **lazily**
(PEP 562 module ``__getattr__``) so nothing statically imports ``noeta.builtins``
and only a caller that actually builds a network provider pays for ``httpx``.
Hence a submodule rather than the ``noeta.sdk`` root, keeping SDK import light.
"""

from __future__ import annotations

import importlib
from typing import Any


__all__ = [
    "OpenAICompatProvider",
    "OpenAIResponsesProvider",
    "AnthropicProvider",
    "CATALOG",
    "ModelSpec",
]

_IMPL = "noeta.builtins.providers.impl"

_EXPORTS = {
    "AnthropicProvider": "anthropic",
    "OpenAICompatProvider": "openai_compat",
    "OpenAIResponsesProvider": "openai_responses",
    "CATALOG": "catalog",
    "ModelSpec": "catalog",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{_IMPL}.{module_name}"), name)


def __dir__() -> list[str]:
    return sorted(__all__)
