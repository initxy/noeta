"""Retrying a model call. Placeholder until the ported 0.6 retry policy lands."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from noeta.errors import RateLimited, Transient
from noeta.providers.base import OnDelta, Provider
from noeta.types import ModelRequest, ModelResponse


async def complete_with_retries(
    provider: Provider,
    request: ModelRequest,
    on_delta: OnDelta | None = None,
    *,
    max_attempts: int = 5,
    sleep: Callable[[float], Any],
    on_retry: Callable[[int, Exception], None] | None = None,
) -> ModelResponse:
    attempt = 0
    while True:
        attempt += 1
        try:
            return await provider.complete(request, on_delta)
        except Transient as exc:
            if attempt >= max_attempts:
                raise
            if on_retry is not None:
                on_retry(attempt, exc)
            delay = 2.0**attempt
            if isinstance(exc, RateLimited) and exc.retry_after:
                delay = exc.retry_after
            await sleep(delay)
