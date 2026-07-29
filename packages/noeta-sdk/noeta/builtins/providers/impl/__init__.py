"""``providers`` built-in — the official LLM provider adapters (impl).

Microkernel M2: moved here from ``noeta.providers`` (that band is gone).
Each adapter module (``anthropic`` / ``openai_compat`` / ``openai_responses``)
implements the :class:`noeta.protocols.messages.LLMProvider` Protocol and
translates between the Noeta-shape internal protocol and a vendor wire
format. The contract survives the move unchanged:

* The kernel never imports an adapter — ``RuntimeLLMClient`` receives an
  ``LLMProvider`` via dependency injection so the wrapper stays
  vendor-agnostic, and the pricing callback is injected the same way
  (``catalog.price``, wired by the SDK host).
* An adapter module imports only ``noeta.protocols.*`` + stdlib + the
  vendor's own client library (``httpx``). ``catalog`` additionally reaches
  the kernel builder's :class:`~noeta.execution.builder.CompactionConfig`
  for :func:`~noeta.builtins.providers.impl.catalog.derive_compaction_config`
  — a builtins→kernel edge, the allowed direction.

The ``provider`` surface is single-valued and host-wired (the parent
manifest is declaration-only); hosts construct one adapter through
``noeta.sdk.providers`` (a lazy re-export of this package) or
``Options.provider``.

This ``__init__`` deliberately re-exports nothing so importing the package
does not pull in heavy adapter dependencies (``httpx``) for callers that
only want, say, the catalog.
"""

from __future__ import annotations

__all__: list[str] = []
