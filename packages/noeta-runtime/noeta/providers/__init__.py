"""L2 adapter layer: third-party LLM provider implementations.

Each adapter module (``openai_compat``, future ``anthropic`` etc.)
implements the :class:`noeta.protocols.messages.LLMProvider` Protocol and
translates between the Noeta-shape internal protocol and a vendor wire
format. The contract:

* Adapters live at L2 alongside ``noeta.runtime`` / ``noeta.tools``.
* ``noeta.runtime`` may not import ``noeta.providers`` — RuntimeLLMClient
  receives an ``LLMProvider`` via dependency injection so the wrapper
  stays vendor-agnostic.
* ``noeta.providers`` may import only ``noeta.protocols.*`` + stdlib +
  the vendor's own client library (e.g. ``httpx``). It must not depend
  on any other L2 service or L1 ``core``.

These two rules are enforced as forbidden contracts in ``.importlinter``.

This ``__init__`` deliberately re-exports nothing so importing
``noeta.providers`` does not pull in heavy adapter dependencies (e.g.
``httpx``) for callers that only want, say, the future Anthropic SDK.
"""

from __future__ import annotations

__all__: list[str] = []
