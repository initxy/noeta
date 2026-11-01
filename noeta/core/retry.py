"""Retry with backoff around one provider call.

Adapters never retry: each raises one neutral :mod:`noeta.core.errors` failure.
This helper is the one place that decides whether to try again:

* :class:`~noeta.core.errors.Transient` (network failures, 5xx, overloaded, a
  stream cut before its terminal event) and
  :class:`~noeta.core.errors.RateLimited` are retried;
* :class:`~noeta.core.errors.StreamBroken` — a stream that broke after it
  began — goes on as a call answered in one piece when the provider offers
  ``complete_whole`` (Claude Code's fallback from a failed stream, for every
  call it makes): at once, or after the stream was tried again as often as
  the error allows; that call is made even when the budget is spent, and
  every later retry of the call stays in one piece;
* :class:`~noeta.core.errors.MalformedToolArguments` — a tool call whose
  arguments did not parse — is asked for again once, as Claude Code falls
  back to one more request when a streamed reply cannot be read;
* :class:`~noeta.core.errors.ContextOverflow` and :class:`~noeta.core.errors.Fatal`
  surface at once — overflow's recovery is compaction, and re-sending a
  refused request cannot help.

Cancellation is plain ``asyncio`` cancellation: a cancel during the call or
during a backoff sleep propagates immediately.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from noeta.core.errors import (
    MalformedToolArguments,
    ModelError,
    RateLimited,
    StreamBroken,
    Transient,
)
from noeta.core.provider import OnDelta, Provider
from noeta.core.types import ModelRequest, ModelResponse

__all__ = ["DEFAULT_MAX_ATTEMPTS", "backoff_delay", "complete_with_retries"]

#: Total attempts (1 call + 8 retries). Sized so a persistent rate limit gets
#: a real recovery window (~1+2+4+8+16+30+30+30 s ⇒ ~2 min of waiting at the
#: backoff midpoint) without stalling a task indefinitely. A provider-supplied
#: ``Retry-After`` overrides the backoff for that attempt.
DEFAULT_MAX_ATTEMPTS = 9

#: Retries of a reply whose tool-call arguments did not parse.
MALFORMED_RETRIES = 1

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
    malformed = 0
    whole = getattr(provider, "complete_whole", None)
    in_one_piece = False
    streamed_again = 0
    while True:
        try:
            if in_one_piece and whole is not None:
                response: ModelResponse = await whole(request)
                return response
            return await provider.complete(request, on_delta)
        except ModelError as exc:
            if isinstance(exc, StreamBroken) and whole is not None and not in_one_piece:
                spent = attempt + 1 >= max_attempts
                if spent or (exc.stream_retries is not None
                             and streamed_again >= exc.stream_retries):
                    # No wait: the stream's failure says nothing about the
                    # one-piece call.
                    in_one_piece = True
                    attempt += 1
                    if on_retry is not None:
                        on_retry(attempt, 0.0, exc)
                    attempt = min(attempt, max_attempts - 1)
                    continue
                streamed_again += 1
            delay = backoff_delay(exc, attempt=attempt, rng=rng)
            if isinstance(exc, MalformedToolArguments):
                malformed += 1
                if malformed > MALFORMED_RETRIES:
                    raise
            if delay is None or attempt + 1 >= max_attempts:
                raise
            attempt += 1
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            await sleep(delay)
