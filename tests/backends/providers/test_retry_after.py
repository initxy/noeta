"""Shared ``Retry-After`` parsing (``noeta.backends.providers._common.parse_retry_after``).

All three adapters translate a 429 into ``RateLimited(retry_after=...)``. The
header has two RFC 7231 wire forms — delta-seconds and HTTP-date — and this
helper resolves both to a non-negative seconds delay (or ``None`` when absent
or unparseable, so the retry helper falls back to backoff).
"""

from __future__ import annotations

from datetime import UTC, datetime

from noeta.backends.providers._common import parse_retry_after


def test_integer_seconds() -> None:
    assert parse_retry_after("3") == 3.0


def test_float_seconds_and_whitespace() -> None:
    assert parse_retry_after("  7.5 ") == 7.5


def test_negative_seconds_clamped_to_zero() -> None:
    # A nonsensical negative delta never asks the loop to "sleep backwards".
    assert parse_retry_after("-5") == 0.0


def test_http_date_future_returns_delta_seconds() -> None:
    now = datetime(2026, 10, 21, 7, 28, 0, tzinfo=UTC)
    assert parse_retry_after("Wed, 21 Oct 2026 07:30:00 GMT", now=now) == 120.0


def test_http_date_in_past_clamped_to_zero() -> None:
    now = datetime(2026, 10, 21, 7, 28, 0, tzinfo=UTC)
    assert parse_retry_after("Wed, 21 Oct 2026 07:00:00 GMT", now=now) == 0.0


def test_http_date_without_zone_is_gmt() -> None:
    now = datetime(2026, 10, 21, 7, 28, 0, tzinfo=UTC)
    assert parse_retry_after("Wed, 21 Oct 2026 07:29:00 -0000", now=now) == 60.0


def test_none_header() -> None:
    assert parse_retry_after(None) is None


def test_garbage_header() -> None:
    assert parse_retry_after("not-a-date-or-number") is None
