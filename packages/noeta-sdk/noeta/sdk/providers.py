"""``noeta.sdk.providers`` — the official LLM provider adapters, re-exported.

The app layer (and the new backend) is provider-neutral but must construct ONE
concrete provider to inject (``Client(provider=…)`` / ``serve_backend``). The
adapters live in the ``providers`` built-in plugin
(``noeta.builtins.providers.impl`` — microkernel M2); routing the import
through ``noeta.sdk`` keeps the encapsulation weld intact — the backend
reaches the engine only through ``noeta.sdk``.

Kept in this submodule (not the ``noeta.sdk`` root) so importing the SDK stays
light: only callers that actually build a network provider pull ``httpx`` in.
The re-export is **lazy** (PEP 562 module ``__getattr__``): the universal
microkernel rule says nothing statically imports ``noeta.builtins`` — the
loader's dynamic-import doorway is the only path — so the adapter modules load
on first attribute access, exactly when a caller builds one.

    from noeta.sdk.providers import OpenAICompatProvider      # chat-completions gateways
    from noeta.sdk.providers import OpenAIResponsesProvider   # responses-API gateways
    from noeta.sdk.providers import AnthropicProvider

The model catalog (``ModelSpec`` rows keyed by model id in ``CATALOG``) is the
read-only companion surface: a product that lets users pick models needs the
specs (context window, output cap, pricing) for the models it wires up.
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

#: Public name → the impl module (under ``_IMPL``) that defines it.
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
