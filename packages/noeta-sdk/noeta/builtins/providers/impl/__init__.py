"""The official LLM provider adapters.

Each adapter module (``anthropic`` / ``openai_compat`` / ``openai_responses``)
implements the :class:`noeta.protocols.messages.LLMProvider` Protocol,
translating between the Noeta-shape internal protocol and one vendor wire
format, and imports nothing beyond ``noeta.protocols.*``, stdlib and ``httpx``.
The kernel never imports an adapter: ``RuntimeLLMClient`` receives an
``LLMProvider`` by dependency injection, as does the pricing callback. This
``__init__`` re-exports nothing so importing the package does not drag
``httpx`` in for a caller that only wants the catalog.
"""

from __future__ import annotations

__all__: list[str] = []
