"""Retry with backoff around one provider call (:mod:`noeta.core.retry`).

``backoff_delay`` is the pure policy (equal-jitter exponential backoff, the
provider's ``Retry-After`` authoritative); ``complete_with_retries`` is the
loop. No HTTP: a scripted fake provider raises or answers in order.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from noeta.core.errors import (
    ContextOverflow,
    Fatal,
    MalformedToolArguments,
    ModelError,
    RateLimited,
    StreamBroken,
    TimedOut,
    Transient,
)
from noeta.core.provider import OnDelta
from noeta.core.retry import (
    DEFAULT_MAX_ATTEMPTS,
    backoff_delay,
    complete_with_retries,
)
from noeta.core.types import Message, ModelRequest, ModelResponse, Text, user

REQUEST = ModelRequest(model="m", messages=[user("hi")])


def _ok(text: str = "ok") -> ModelResponse:
    return ModelResponse(
        message=Message(role="assistant", content=[Text(text=text)]),
        stop_reason="end_turn",
    )


class ScriptedProvider:
    family = "fake"

    def __init__(self, script: list[ModelResponse | Exception]) -> None:
        self._script = list(script)
        self.calls = 0

    async def complete(
        self, request: ModelRequest, on_delta: OnDelta | None = None
    ) -> ModelResponse:
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class SleepRecorder:
    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.slept.append(delay)


# ---------------------------------------------------------------------------
# backoff_delay — pure function
# ---------------------------------------------------------------------------


def test_retry_after_takes_precedence_over_backoff() -> None:
    assert backoff_delay(RateLimited("x", retry_after=5.0), attempt=0) == 5.0
    assert backoff_delay(RateLimited("x", retry_after=7.5), attempt=3) == 7.5


def test_rate_limited_without_hint_backs_off() -> None:
    assert backoff_delay(RateLimited("x"), attempt=0, rng=lambda: 0.0) == 0.5


def test_exponential_backoff_floor_when_jitter_draws_zero() -> None:
    floors = [backoff_delay(Transient("x"), attempt=n, rng=lambda: 0.0) for n in range(6)]
    assert floors == [0.5, 1.0, 2.0, 4.0, 8.0, 15.0]


def test_exponential_backoff_ceiling_when_jitter_draws_one() -> None:
    ceils = [backoff_delay(Transient("x"), attempt=n, rng=lambda: 1.0) for n in range(6)]
    assert ceils == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]


def test_jitter_stays_within_half_and_full_band() -> None:
    for attempt in range(8):
        ceil = min(float(2**attempt), 30.0)
        for _ in range(50):
            delay = backoff_delay(Transient("x"), attempt=attempt)
            assert delay is not None
            assert ceil / 2.0 <= delay <= ceil


def test_backoff_capped_at_thirty_seconds() -> None:
    assert backoff_delay(Transient("x"), attempt=100, rng=lambda: 1.0) == 30.0
    assert backoff_delay(Transient("x"), attempt=100, rng=lambda: 0.0) == 15.0


def test_overflow_fatal_and_bare_model_error_return_none() -> None:
    assert backoff_delay(ContextOverflow("too long"), attempt=0) is None
    assert backoff_delay(Fatal("nope"), attempt=0) is None
    assert backoff_delay(ModelError("misc"), attempt=0) is None


def test_malformed_tool_arguments_is_retried() -> None:
    """A stream cut mid-call leaves broken arguments; retry it (as 0.6 did)."""
    assert backoff_delay(MalformedToolArguments("bad"), attempt=0) is not None


# ---------------------------------------------------------------------------
# complete_with_retries — the loop
# ---------------------------------------------------------------------------


async def test_success_first_try_never_sleeps() -> None:
    provider = ScriptedProvider([_ok()])
    sleep = SleepRecorder()
    resp = await complete_with_retries(provider, REQUEST, sleep=sleep)
    assert resp.message.text == "ok"
    assert provider.calls == 1
    assert sleep.slept == []


async def test_transient_retried_until_success() -> None:
    provider = ScriptedProvider([Transient("rate limited"), _ok("recovered")])
    sleep = SleepRecorder()
    resp = await complete_with_retries(provider, REQUEST, sleep=sleep)
    assert resp.message.text == "recovered"
    assert provider.calls == 2
    assert len(sleep.slept) == 1
    # Equal jitter on attempt 0: the band [0.5, 1.0].
    assert 0.5 <= sleep.slept[0] <= 1.0


async def test_retry_after_honoured_for_sleep_delay() -> None:
    provider = ScriptedProvider([RateLimited("slow down", retry_after=4.0), _ok()])
    sleep = SleepRecorder()
    await complete_with_retries(provider, REQUEST, sleep=sleep)
    assert sleep.slept == [4.0]


async def test_backoff_grows_per_attempt_with_injected_rng() -> None:
    provider = ScriptedProvider([Transient("a"), Transient("b"), Transient("c"), _ok()])
    sleep = SleepRecorder()
    await complete_with_retries(provider, REQUEST, sleep=sleep, rng=lambda: 1.0)
    assert sleep.slept == [1.0, 2.0, 4.0]


async def test_budget_exhausted_reraises_last_error() -> None:
    errors = [Transient(f"nope {i}") for i in range(5)]
    provider = ScriptedProvider(list(errors))
    sleep = SleepRecorder()
    with pytest.raises(Transient) as info:
        await complete_with_retries(provider, REQUEST, max_attempts=3, sleep=sleep)
    assert info.value is errors[2]
    assert provider.calls == 3  # 1 initial + 2 retries
    assert len(sleep.slept) == 2


async def test_default_budget_is_nine_attempts() -> None:
    assert DEFAULT_MAX_ATTEMPTS == 9
    provider = ScriptedProvider([Transient("x") for _ in range(20)])
    with pytest.raises(Transient):
        await complete_with_retries(provider, REQUEST, sleep=SleepRecorder())
    assert provider.calls == 9


async def test_max_attempts_one_never_retries() -> None:
    provider = ScriptedProvider([Transient("x"), _ok()])
    sleep = SleepRecorder()
    with pytest.raises(Transient):
        await complete_with_retries(provider, REQUEST, max_attempts=1, sleep=sleep)
    assert provider.calls == 1
    assert sleep.slept == []


@pytest.mark.parametrize("bad", [0, -1])
async def test_max_attempts_below_one_refused(bad: int) -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        await complete_with_retries(ScriptedProvider([_ok()]), REQUEST, max_attempts=bad)


@pytest.mark.parametrize(
    "error",
    [ContextOverflow("too long"), Fatal("bad key")],
)
async def test_non_transient_not_retried(error: ModelError) -> None:
    provider = ScriptedProvider([error, _ok()])
    sleep = SleepRecorder()
    with pytest.raises(type(error)) as info:
        await complete_with_retries(provider, REQUEST, sleep=sleep)
    assert info.value is error
    assert provider.calls == 1
    assert sleep.slept == []


async def test_non_model_error_propagates_untouched() -> None:
    provider = ScriptedProvider([RuntimeError("bug"), _ok()])
    with pytest.raises(RuntimeError, match="bug"):
        await complete_with_retries(provider, REQUEST, sleep=SleepRecorder())
    assert provider.calls == 1


async def test_on_retry_called_before_each_sleep() -> None:
    first, second = Transient("one"), RateLimited("two", retry_after=2.0)
    provider = ScriptedProvider([first, second, _ok()])
    events: list[tuple[str, Any]] = []
    seen: list[tuple[int, float, ModelError]] = []

    async def sleep(delay: float) -> None:
        events.append(("sleep", delay))

    def on_retry(attempt: int, delay: float, error: ModelError) -> None:
        seen.append((attempt, delay, error))
        events.append(("retry", attempt))

    await complete_with_retries(
        provider, REQUEST, sleep=sleep, on_retry=on_retry, rng=lambda: 0.0
    )
    assert seen == [(1, 0.5, first), (2, 2.0, second)]
    assert events == [("retry", 1), ("sleep", 0.5), ("retry", 2), ("sleep", 2.0)]


async def test_on_delta_forwarded_to_provider() -> None:
    got: list[OnDelta | None] = []

    class Capturing(ScriptedProvider):
        async def complete(
            self, request: ModelRequest, on_delta: OnDelta | None = None
        ) -> ModelResponse:
            got.append(on_delta)
            return await super().complete(request, on_delta)

    def sink(_delta: Any) -> None:
        return None

    await complete_with_retries(Capturing([_ok()]), REQUEST, sink)
    assert got == [sink]


async def test_cancellation_during_backoff_sleep_propagates() -> None:
    provider = ScriptedProvider([Transient("x"), _ok()])
    entered = asyncio.Event()

    async def slow_sleep(delay: float) -> None:
        entered.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(complete_with_retries(provider, REQUEST, sleep=slow_sleep))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.calls == 1  # never re-tried after the cancel


async def test_cancellation_during_call_propagates() -> None:
    started = asyncio.Event()

    class Hanging:
        family = "fake"

        async def complete(
            self, request: ModelRequest, on_delta: OnDelta | None = None
        ) -> ModelResponse:
            started.set()
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

    sleep = SleepRecorder()
    task = asyncio.create_task(complete_with_retries(Hanging(), REQUEST, sleep=sleep))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sleep.slept == []


async def test_a_malformed_tool_call_is_asked_for_again_once() -> None:
    once = ScriptedProvider([MalformedToolArguments("bad"), _ok("fixed")])
    assert (await complete_with_retries(once, REQUEST, sleep=SleepRecorder())).message.text \
        == "fixed"
    twice = ScriptedProvider([MalformedToolArguments("bad"), Transient("blip"),
                              MalformedToolArguments("bad again"), _ok()])
    with pytest.raises(MalformedToolArguments, match="bad again"):
        await complete_with_retries(twice, REQUEST, sleep=SleepRecorder())
    assert twice.calls == 3


# ---------------------------------------------------------------------------
# A broken stream goes on in one piece (Claude Code's non-streaming fallback)
# ---------------------------------------------------------------------------


class WholeProvider(ScriptedProvider):
    """Streams from ``script``; answers in one piece from ``whole``."""

    def __init__(self, script: list[ModelResponse | Exception],
                 whole: list[ModelResponse | Exception]) -> None:
        super().__init__(script)
        self._whole = list(whole)
        self.whole_calls = 0

    async def complete_whole(self, request: ModelRequest) -> ModelResponse:
        self.whole_calls += 1
        item = self._whole.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def test_a_broken_stream_goes_on_in_one_piece_at_once() -> None:
    sleep = SleepRecorder()
    seen: list[tuple[int, float]] = []
    provider = WholeProvider([StreamBroken("server error")],
                             [Transient("504"), _ok("whole")])
    response = await complete_with_retries(
        provider, REQUEST, sleep=sleep, rng=lambda: 0.0,
        on_retry=lambda attempt, delay, _e: seen.append((attempt, delay)))
    assert response.message.content == [Text(text="whole")]
    # No wait before the switch; the later retry stays in one piece.
    assert (provider.calls, provider.whole_calls) == (1, 2)
    assert seen[0] == (1, 0.0) and sleep.slept == [1.0]


async def test_a_stream_is_tried_again_as_often_as_the_error_allows() -> None:
    sleep = SleepRecorder()
    provider = WholeProvider([StreamBroken("silent", stream_retries=1),
                              StreamBroken("silent", stream_retries=1)], [_ok("whole")])
    await complete_with_retries(provider, REQUEST, sleep=sleep, rng=lambda: 0.0)
    assert (provider.calls, provider.whole_calls) == (2, 1)


async def test_a_dropped_connection_streams_again_then_gets_one_more_call() -> None:
    """A dropped connection is streamed again for the whole budget; the
    call in one piece is made even then (Claude Code's)."""
    provider = WholeProvider([StreamBroken("dropped", stream_retries=None)] * 3,
                             [_ok("whole")])
    response = await complete_with_retries(provider, REQUEST, max_attempts=3,
                                           sleep=SleepRecorder(), rng=lambda: 0.0)
    assert response.message.content == [Text(text="whole")]
    assert (provider.calls, provider.whole_calls) == (3, 1)
    failing = WholeProvider([StreamBroken("dropped", stream_retries=None)] * 3,
                            [Transient("still down")])
    with pytest.raises(Transient, match="still down"):
        await complete_with_retries(failing, REQUEST, max_attempts=3,
                                    sleep=SleepRecorder(), rng=lambda: 0.0)
    assert failing.whole_calls == 1


async def test_a_timed_out_call_in_one_piece_goes_back_to_streaming_for_good() -> None:
    """A one-piece call that outlasted its time limit is not sent in one
    piece again: the call streams, and a second broken stream is streamed
    again instead of switching a second time."""
    provider = WholeProvider([StreamBroken("server error"), StreamBroken("server error"),
                              _ok("streamed")],
                             [TimedOut("no answer in 300 s"), _ok("never")])
    response = await complete_with_retries(provider, REQUEST, sleep=SleepRecorder(),
                                           rng=lambda: 0.0)
    assert response.message.content == [Text(text="streamed")]
    assert (provider.calls, provider.whole_calls) == (3, 1)


async def test_a_timed_out_call_in_one_piece_spends_the_budget_once() -> None:
    provider = WholeProvider([StreamBroken("server error")] * 9,
                             [TimedOut("no answer in 300 s")] * 9)
    with pytest.raises(StreamBroken):
        await complete_with_retries(provider, REQUEST, sleep=SleepRecorder(),
                                    rng=lambda: 0.0)
    assert provider.whole_calls == 1
    assert provider.calls + provider.whole_calls == DEFAULT_MAX_ATTEMPTS


async def test_a_provider_without_a_whole_call_streams_again() -> None:
    provider = ScriptedProvider([StreamBroken("cut"), _ok()])
    await complete_with_retries(provider, REQUEST, sleep=SleepRecorder(), rng=lambda: 0.0)
    assert provider.calls == 2
