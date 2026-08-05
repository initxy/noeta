"""Interrupt responsiveness of the LLM round-trip (spec: interrupt-responsiveness).

A human stop must land while a provider call is in flight — any phase:
pre-first-byte silence, mid-stream, a failed attempt's backoff — not after the
round returns. The mechanism under test is ``RuntimeLLMClient``'s abandonable
wait: with ``StepContext.cancelled`` wired, the provider call runs on a daemon
I/O thread the step thread can stop waiting for, the retry loop re-checks the
predicate, and the backoff sleep is sliced. Providers are contractually pure,
so the abandoned orphan's eventual result simply has no consumer.

Byte-compat guardrail: with ``cancelled=None`` (resume / replay) the call runs
inline on the calling thread exactly as before — pinned here by thread
identity, and by the untouched pre-existing suite.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from noeta.protocols.errors import AbortedError, TransientError
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    Message,
    StreamDelta,
    TextBlock,
    Usage,
)
from noeta.protocols.step_context import StepContext
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(
    cancelled: Optional[Callable[[], bool]] = None, task_id: str = "task-1"
) -> StepContext:
    return StepContext(
        task_id=task_id,
        lease_id="lease-1",
        trace_id="trace-1",
        cancelled=cancelled,
    )


def _req(text: str = "hi") -> LLMRequest:
    return LLMRequest(
        model="gpt-x",
        messages=[Message(role="user", content=[TextBlock(text=text)])],
    )


def _ok_response(text: str = "ok") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "resp-stub"},
    )


class _Flag:
    """A settable cancel predicate."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def __call__(self) -> bool:
        return self._event.is_set()


class _BlockingProvider:
    """Blocks inside ``complete`` until released; records its calling thread."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.calls = 0
        self.thread_names: list[str] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        self.thread_names.append(threading.current_thread().name)
        self.entered.set()
        assert self.release.wait(timeout=10.0), "test forgot to release"
        self.finished.set()
        return _ok_response()


def _client(provider: Any, **kwargs: Any) -> tuple[RuntimeLLMClient, Any]:
    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    client = RuntimeLLMClient(
        provider=provider,
        event_log=log,
        content_store=cs,
        abandon_poll_seconds=0.01,
        **kwargs,
    )
    return client, log


# ---------------------------------------------------------------------------
# Abandonable wait
# ---------------------------------------------------------------------------


def test_interrupt_mid_call_returns_aborted_while_provider_still_blocked() -> None:
    """The headline behavior: Esc lands while the HTTP wait is still open."""
    provider = _BlockingProvider()
    client, log = _client(provider)
    flag = _Flag()

    result: list[LLMResponse] = []

    def _drive() -> None:
        result.append(client.complete(_req(), _ctx(cancelled=flag)))

    driver = threading.Thread(target=_drive)
    driver.start()
    assert provider.entered.wait(timeout=5.0)
    flag.set()
    driver.join(timeout=5.0)
    assert not driver.is_alive(), "complete() must return without the provider"

    # The provider is STILL blocked — the wait was abandoned, not completed.
    assert not provider.finished.is_set()

    resp = result[0]
    assert resp.stop_reason == "error"
    assert (resp.raw or {})["category"] == "aborted"

    # The trio is recorded and well-formed despite the abandonment.
    events = log.read("task-1")
    assert [e.type for e in events] == [
        "LLMRequestStarted",
        "LLMResponseRecorded",
        "LLMRequestFinished",
    ]
    assert events[2].payload.success is False

    # Releasing the orphan later adds nothing to the stream.
    provider.release.set()
    assert provider.finished.wait(timeout=5.0)
    time.sleep(0.05)
    assert [e.type for e in log.read("task-1")] == [
        "LLMRequestStarted",
        "LLMResponseRecorded",
        "LLMRequestFinished",
    ]


def test_pre_armed_cancel_refuses_the_attempt_without_calling_provider() -> None:
    provider = _BlockingProvider()
    client, _ = _client(provider)
    flag = _Flag()
    flag.set()

    resp = client.complete(_req(), _ctx(cancelled=flag))

    assert (resp.raw or {})["category"] == "aborted"
    assert provider.calls == 0


def test_without_cancel_seam_call_is_inline_on_the_step_thread() -> None:
    """``cancelled=None`` ⇒ the historical inline path, no I/O thread."""
    provider = _BlockingProvider()
    provider.release.set()
    client, _ = _client(provider)

    resp = client.complete(_req(), _ctx(cancelled=None))

    assert resp.stop_reason == "end_turn"
    assert provider.thread_names == [threading.current_thread().name]


def test_with_cancel_seam_call_runs_on_io_thread() -> None:
    provider = _BlockingProvider()
    provider.release.set()
    client, _ = _client(provider)
    flag = _Flag()  # never set — the call completes normally

    resp = client.complete(_req(), _ctx(cancelled=flag))

    assert resp.stop_reason == "end_turn"
    assert provider.thread_names == ["noeta-llm-io"]


# ---------------------------------------------------------------------------
# Retry loop
# ---------------------------------------------------------------------------


class _FailingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        raise TransientError("rate limited", retry_after=5.0)


def test_cancel_after_failed_attempt_skips_retry_and_backoff() -> None:
    provider = _FailingProvider()
    sleeps: list[float] = []
    client, log = _client(provider, sleep=sleeps.append)
    flag = _Flag()
    flag.set()  # armed before the first attempt would even start

    resp = client.complete(_req(), _ctx(cancelled=flag))

    assert (resp.raw or {})["category"] == "aborted"
    assert provider.calls == 0
    assert sleeps == []
    assert not any(e.type == "LLMRetryScheduled" for e in log.read("task-1"))


def test_cancel_during_backoff_aborts_within_one_slice() -> None:
    provider = _FailingProvider()
    flag = _Flag()
    sleeps: list[float] = []

    def _sleep(seconds: float) -> None:
        # The first backoff slice flips the flag — as if Esc landed mid-wait.
        sleeps.append(seconds)
        flag.set()

    client, log = _client(provider, sleep=_sleep)

    resp = client.complete(_req(), _ctx(cancelled=flag))

    assert (resp.raw or {})["category"] == "aborted"
    # One failed attempt, one sliced wait, no second attempt.
    assert provider.calls == 1
    assert len(sleeps) == 1
    assert sleeps[0] <= 0.01 + 1e-9  # a slice, not the provider's 5 s hint
    retries = [e for e in log.read("task-1") if e.type == "LLMRetryScheduled"]
    assert len(retries) == 1  # scheduled before the wait noticed the stop


def test_aborted_error_from_adapter_is_never_retried() -> None:
    class _AbortingProvider:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, request: LLMRequest) -> LLMResponse:
            self.calls += 1
            raise AbortedError("client abort")

    provider = _AbortingProvider()
    client, log = _client(provider)
    flag = _Flag()

    resp = client.complete(_req(), _ctx(cancelled=flag))

    assert (resp.raw or {})["category"] == "aborted"
    assert provider.calls == 1
    assert not any(e.type == "LLMRetryScheduled" for e in log.read("task-1"))


# ---------------------------------------------------------------------------
# Delta muting + should_abort hand-off
# ---------------------------------------------------------------------------


class _StreamingBlockingProvider:
    """Emits one delta, then blocks. New signature (accepts should_abort)."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.first_delta_out = threading.Event()
        self.seen_should_abort: list[Optional[Callable[[], bool]]] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        return _ok_response()

    def complete_streaming(
        self,
        request: LLMRequest,
        on_delta: Callable[[StreamDelta], None],
        request_headers: Optional[dict[str, str]] = None,
        should_abort: Optional[Callable[[], bool]] = None,
    ) -> LLMResponse:
        self.seen_should_abort.append(should_abort)
        on_delta(StreamDelta(kind="text", text="early", index=0))
        self.first_delta_out.set()
        assert self.release.wait(timeout=10.0), "test forgot to release"
        on_delta(StreamDelta(kind="text", text="late", index=0))
        return _ok_response()


def test_abandonment_mutes_late_deltas_and_hands_adapter_the_predicate() -> None:
    provider = _StreamingBlockingProvider()
    deltas: list[str] = []
    client, _ = _client(
        provider,
        delta_sink=lambda ctx, call_id, d: deltas.append(d.text),
    )
    flag = _Flag()

    result: list[LLMResponse] = []
    driver = threading.Thread(
        target=lambda: result.append(
            client.complete(_req(), _ctx(cancelled=flag))
        )
    )
    driver.start()
    assert provider.first_delta_out.wait(timeout=5.0)
    flag.set()
    driver.join(timeout=5.0)
    assert not driver.is_alive()
    assert (result[0].raw or {})["category"] == "aborted"

    # The orphan's late delta must not reach the sink.
    provider.release.set()
    time.sleep(0.1)
    assert deltas == ["early"]

    # The adapter got the live predicate (fast orphan shutdown seam).
    assert provider.seen_should_abort == [flag]


def test_old_signature_streaming_provider_gets_no_should_abort() -> None:
    """A third-party adapter on the pre-abort signature keeps working."""

    class _OldStreamingProvider:
        def complete(self, request: LLMRequest) -> LLMResponse:
            return _ok_response()

        def complete_streaming(
            self,
            request: LLMRequest,
            on_delta: Callable[[StreamDelta], None],
            request_headers: Optional[dict[str, str]] = None,
        ) -> LLMResponse:
            on_delta(StreamDelta(kind="text", text="tok", index=0))
            return _ok_response("streamed")

    provider = _OldStreamingProvider()
    deltas: list[str] = []
    client, _ = _client(
        provider,
        delta_sink=lambda ctx, call_id, d: deltas.append(d.text),
    )
    flag = _Flag()

    resp = client.complete(_req(), _ctx(cancelled=flag))

    assert resp.stop_reason == "end_turn"
    assert resp.content == [TextBlock(text="streamed")]
    assert deltas == ["tok"]
