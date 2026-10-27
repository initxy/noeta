"""Retry with backoff around one provider call.

Adapters never retry: each raises one neutral :mod:`noeta.errors` failure.
This helper is the one place that decides whether to try again:

* :class:`~noeta.errors.Transient` (network failures, 5xx, overloaded, a
  stream cut before its terminal event) and :class:`~noeta.errors.RateLimited`
  are retried;
* :class:`~noeta.errors.ContextOverflow` and :class:`~noeta.errors.Fatal`
  (including :class:`~noeta.errors.MalformedToolArguments`) surface at once —
  overflow's recovery is compaction, and re-sending a refused request cannot
  help.

Cancellation is plain ``asyncio`` cancellation: a cancel during the call or
during a backoff sleep propagates immediately.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from noeta.errors import ModelError, RateLimited, Transient
from noeta.providers.base import OnDelta, Provider
from noeta.types import ModelRequest, ModelResponse

__all__ = ["DEFAULT_MAX_ATTEMPTS", "backoff_delay", "complete_with_retries"]

#: Total attempts (1 call + 8 retries). Sized so a persistent rate limit gets
#: a real recovery window (~1+2+4+8+16+30+30+30 s ⇒ ~2 min of waiting at the
#: backoff midpoint) without stalling a task indefinitely. A provider-supplied
#: ``Retry-After`` overrides the backoff for that attempt.
DEFAULT_MAX_ATTEMPTS = 9

#: Exponential-backoff base (seconds) and ceiling.
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 30.0

OnRetry = Callable[[int, float, ModelError], object]


def backoff_delay(
    error: ModelError,
    *,
    attempt: int,
    rng: Callable[[], float] = random.random,
) -> float | None:
    """Seconds to wait before retry number ``attempt + 1``, or ``None`` for
    "do not retry".

    * :class:`RateLimited` with a ``retry_after`` → that value verbatim (the
      provider's hint is authoritative; no jitter on top).
    * Any other :class:`Transient` → ``base * 2**attempt`` capped at 30 s, with
      **equal jitter**: half fixed, half uniformly random. The fixed half keeps
      a meaningful floor (a rate limit needs a real wait); the random half
      decorrelates sibling retriers — two subagents that hit the same 429 in
      lockstep no longer retry in unison and re-collide.
    * Anything else → ``None``.
    """
    if not isinstance(error, Transient):
        return None
    if isinstance(error, RateLimited) and error.retry_after is not None:
        return error.retry_after
    temp = min(_BACKOFF_BASE_SECONDS * float(2**attempt), _BACKOFF_CAP_SECONDS)
    return temp / 2.0 + rng() * (temp / 2.0)


async def complete_with_retries(
    provider: Provider,
    request: ModelRequest,
    on_delta: OnDelta | None = None,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    on_retry: OnRetry | None = None,
    rng: Callable[[], float] = random.random,
) -> ModelResponse:
    """``provider.complete`` with retries on transient failures.

    ``on_retry(attempt, delay, error)`` is called before each backoff sleep
    (``attempt`` is the 1-based number of the retry about to happen) so a
    caller can surface "rate-limited, retrying" instead of a silent stall.
    Deltas from a failed attempt have already been delivered; a caller that
    renders them should reset on ``on_retry``. The last error is re-raised
    once ``max_attempts`` calls have failed. ``sleep`` and ``rng`` are
    injectable so tests never wall-clock through a backoff.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    attempt = 0
    while True:
        try:
            return await provider.complete(request, on_delta)
        except ModelError as exc:
            delay = backoff_delay(exc, attempt=attempt, rng=rng)
            if delay is None or attempt + 1 >= max_attempts:
                raise
            attempt += 1
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            await sleep(delay)
