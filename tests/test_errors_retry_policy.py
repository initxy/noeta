"""② error recovery — neutral error taxonomy + pure backoff policy.

README D-2 / D-2b. Three provider-neutral error classes
(``TransientError`` / ``ContextOverflowError`` / ``FatalError``) plus a
pure ``retry_policy(error, *, attempt)`` function live at L0
(``noeta.protocols.errors``) so both providers (sdk, L2) and the runtime
LLM wrapper (L2) can share one vocabulary without violating the import
topology or pinning any vendor wire shape.

The policy reads no clock. An explicit ``retry_after`` is honored verbatim;
the hint-free backoff branch layers **equal jitter** over the exponential
schedule (half fixed floor, half uniform random) to decorrelate sibling
retriers. The randomness draw is injectable (``rng``) so tests pin it; the
retry loop is LIVE-only and writes no events, so the chosen delay is never
folded/observed downstream.
"""

from __future__ import annotations

import pytest

from noeta.protocols.errors import (
    CATEGORY_FATAL,
    CATEGORY_OVERFLOW,
    CATEGORY_TRANSIENT,
    ContextOverflowError,
    FatalError,
    NoetaError,
    TransientError,
    retry_policy,
)


# ---------------------------------------------------------------------------
# Construction + taxonomy
# ---------------------------------------------------------------------------


def test_three_classes_subclass_noetaerror() -> None:
    assert issubclass(TransientError, NoetaError)
    assert issubclass(ContextOverflowError, NoetaError)
    assert issubclass(FatalError, NoetaError)


def test_transient_carries_optional_retry_after() -> None:
    err = TransientError("rate limited", retry_after=5.0)
    assert err.retry_after == 5.0
    assert "rate limited" in str(err)


def test_transient_retry_after_defaults_to_none() -> None:
    assert TransientError("boom").retry_after is None
    assert TransientError().retry_after is None


def test_overflow_and_fatal_also_accept_retry_after_none_by_default() -> None:
    # The field exists on the shared base so callers can introspect it
    # uniformly; overflow / fatal simply leave it None.
    assert ContextOverflowError("too long").retry_after is None
    assert FatalError("bad request").retry_after is None


def test_category_constants_are_stable_strings() -> None:
    # raw['category'] and policy share these literals — byte-stable.
    assert CATEGORY_TRANSIENT == "transient"
    assert CATEGORY_OVERFLOW == "overflow"
    assert CATEGORY_FATAL == "fatal"


def test_each_error_exposes_its_category() -> None:
    assert TransientError().category == CATEGORY_TRANSIENT
    assert ContextOverflowError().category == CATEGORY_OVERFLOW
    assert FatalError().category == CATEGORY_FATAL


def test_malformed_tool_arguments_is_transient_and_value_error() -> None:
    # A truncated/garbled tool-call arguments string is treated as a transient
    # transport failure (retried by the runtime), while staying a ValueError so
    # the codec's historical wording/type contract and any ``except ValueError``
    # caller keep matching.
    from noeta.protocols.errors import MalformedToolArgumentsError

    err = MalformedToolArgumentsError("function_call arguments not JSON-decodable: x")
    assert isinstance(err, TransientError)
    assert isinstance(err, ValueError)
    assert isinstance(err, NoetaError)
    assert err.category == CATEGORY_TRANSIENT
    assert err.retry_after is None
    assert "not JSON-decodable" in str(err)
    # It rides the transient backoff path like any other TransientError.
    assert retry_policy(err, attempt=0, rng=lambda: 0.0) == 0.5


# ---------------------------------------------------------------------------
# retry_policy — pure function
# ---------------------------------------------------------------------------


def test_retry_after_takes_precedence_over_backoff() -> None:
    assert retry_policy(TransientError(retry_after=5.0), attempt=0) == 5.0
    # Even on a later attempt, an explicit retry_after wins.
    assert retry_policy(TransientError(retry_after=7.5), attempt=3) == 7.5


def test_exponential_backoff_floor_when_jitter_draws_zero() -> None:
    # rng()==0 → the fixed half only: temp/2 of the exponential schedule
    # (base ~1.0 doubling per attempt). This is the guaranteed minimum wait.
    floors = [
        retry_policy(TransientError(), attempt=n, rng=lambda: 0.0)
        for n in range(6)
    ]
    assert floors == [0.5, 1.0, 2.0, 4.0, 8.0, 15.0]  # last = 30/2 (capped)


def test_exponential_backoff_ceiling_when_jitter_draws_one() -> None:
    # rng()==1 → the full exponential schedule: temp (== the old no-jitter
    # values). This is the maximum wait per attempt.
    ceils = [
        retry_policy(TransientError(), attempt=n, rng=lambda: 1.0)
        for n in range(6)
    ]
    assert ceils == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]


def test_jitter_stays_within_half_and_full_band() -> None:
    # With the default rng, every draw lands in [temp/2, temp]: the floor
    # keeps a real wait, the band decorrelates lockstep siblings.
    for attempt in range(8):
        ceil = min(1.0 * float(2**attempt), 30.0)
        for _ in range(50):
            delay = retry_policy(TransientError(), attempt=attempt)
            assert ceil / 2.0 <= delay <= ceil


def test_backoff_capped_at_thirty_seconds() -> None:
    # Far-out attempts stay clamped at the cap (jitter band [15, 30]),
    # never running away.
    assert retry_policy(TransientError(), attempt=100, rng=lambda: 1.0) == 30.0
    assert retry_policy(TransientError(), attempt=100, rng=lambda: 0.0) == 15.0


def test_overflow_returns_none() -> None:
    assert retry_policy(ContextOverflowError("too long"), attempt=0) is None


def test_fatal_returns_none() -> None:
    assert retry_policy(FatalError("nope"), attempt=0) is None


def test_non_transient_noetaerror_returns_none() -> None:
    # A bare NoetaError (not one of the three categories) is not retryable.
    assert retry_policy(NoetaError("misc"), attempt=0) is None
