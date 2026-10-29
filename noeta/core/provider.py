"""The provider interface: one async call from a request to a response."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from noeta.core.types import Delta, ModelRequest, ModelResponse

OnDelta = Callable[[Delta], None]


class Provider(Protocol):
    """A model backend.

    Optional members: ``async count_tokens(model, text) -> int | None`` (the
    exact token count of one user message holding ``text``; without it
    callers estimate) and ``async aclose()`` (release connections).
    """

    #: Which vendor wire the adapter speaks (``anthropic``, ``openai_compat``,
    #: ``openai_responses``). Thinking blocks are only replayed to their family.
    family: str

    async def complete(
        self, request: ModelRequest, on_delta: OnDelta | None = None
    ) -> ModelResponse:
        """Send ``request``; stream deltas to ``on_delta`` when given. Raises a
        ``noeta.core.errors.ModelError`` subclass on failure. Cancellation is plain
        ``asyncio`` cancellation and must close the connection."""
        ...
