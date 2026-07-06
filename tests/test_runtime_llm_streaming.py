"""Token-streaming seam tests: the RuntimeLLMClient probe + delta sink.

Pins the token-streaming decision's runtime half: streaming happens only
when a ``delta_sink`` is injected AND the provider is a ``StreamingProvider``
AND the call site allows it; every other combination takes the historical
batch paths byte-identically. Deltas are ephemeral — the ledger trio is the
same whether or not anyone streamed.
"""

from __future__ import annotations

from typing import Optional

import pytest

from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    Message,
    StreamDelta,
    StreamingProvider,
    TextBlock,
    Usage,
)
from noeta.protocols.step_context import StepContext
from noeta.providers._sse import iter_sse_events
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.fake_llm import FakeLLMProvider, FakeStreamingLLMProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(task_id: str = "task-1") -> StepContext:
    return StepContext(task_id=task_id, lease_id="lease-1", trace_id="trace-1")


def _req(text: str = "hi", model: str = "gpt-x") -> LLMRequest:
    return LLMRequest(
        model=model,
        messages=[Message(role="user", content=[TextBlock(text=text)])],
    )


def _ok_response(text: str = "ok") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "resp-stub"},
    )


_DELTAS = [
    StreamDelta(kind="thinking", text="hmm", index=0),
    StreamDelta(kind="text", text="he", index=1),
    StreamDelta(kind="text", text="llo", index=1),
]


class _SinkRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[StepContext, str, StreamDelta]] = []

    def __call__(
        self, ctx: StepContext, call_id: str, delta: StreamDelta
    ) -> None:
        self.calls.append((ctx, call_id, delta))


def _streaming_provider(text: str = "hello") -> FakeStreamingLLMProvider:
    return FakeStreamingLLMProvider(
        responses=[_ok_response(text)], deltas=[list(_DELTAS)]
    )


# ---------------------------------------------------------------------------
# Probe matrix
# ---------------------------------------------------------------------------


def test_fake_streaming_provider_matches_protocol() -> None:
    assert isinstance(_streaming_provider(), StreamingProvider)
    assert not isinstance(FakeLLMProvider(), StreamingProvider)


def test_sink_plus_streaming_provider_streams() -> None:
    log, cs = InMemoryEventLog(), InMemoryContentStore()
    provider = _streaming_provider()
    sink = _SinkRecorder()
    client = RuntimeLLMClient(
        provider=provider, event_log=log, content_store=cs, delta_sink=sink
    )

    resp = client.complete(_req(), _ctx())

    assert resp.content == [TextBlock(text="hello")]
    assert provider.streamed_calls == 1
    assert provider.batch_calls == 0
    assert [d for (_, _, d) in sink.calls] == _DELTAS
    # Every delta is bound to this trio's identity: the ctx of the step and
    # the call_id the ledger's LLMRequestStarted carries.
    started = log.read("task-1")[0]
    assert all(cid == started.payload.call_id for (_, cid, _) in sink.calls)
    assert all(c.task_id == "task-1" for (c, _, _) in sink.calls)


def test_no_sink_takes_batch_path() -> None:
    log, cs = InMemoryEventLog(), InMemoryContentStore()
    provider = _streaming_provider()
    client = RuntimeLLMClient(provider=provider, event_log=log, content_store=cs)

    resp = client.complete(_req(), _ctx())

    assert resp.stop_reason == "end_turn"
    assert provider.streamed_calls == 0
    assert provider.batch_calls == 1


def test_allow_stream_false_takes_batch_path() -> None:
    log, cs = InMemoryEventLog(), InMemoryContentStore()
    provider = _streaming_provider()
    sink = _SinkRecorder()
    client = RuntimeLLMClient(
        provider=provider, event_log=log, content_store=cs, delta_sink=sink
    )

    client.complete(_req(), _ctx(), allow_stream=False)

    assert provider.streamed_calls == 0
    assert provider.batch_calls == 1
    assert sink.calls == []


def test_non_streaming_provider_with_sink_takes_plain_complete() -> None:
    log, cs = InMemoryEventLog(), InMemoryContentStore()
    provider = FakeLLMProvider(responses=[_ok_response("plain")])
    sink = _SinkRecorder()
    client = RuntimeLLMClient(
        provider=provider, event_log=log, content_store=cs, delta_sink=sink
    )

    resp = client.complete(_req(), _ctx())

    assert resp.content == [TextBlock(text="plain")]
    assert sink.calls == []


def test_streaming_receives_provider_headers() -> None:
    log, cs = InMemoryEventLog(), InMemoryContentStore()
    provider = _streaming_provider()
    client = RuntimeLLMClient(
        provider=provider,
        event_log=log,
        content_store=cs,
        delta_sink=_SinkRecorder(),
        provider_headers=lambda ctx: {"x-task": ctx.task_id},
    )

    client.complete(_req(), _ctx("task-9"))

    assert provider.streamed_headers == [{"x-task": "task-9"}]


def test_streaming_without_header_callable_passes_none() -> None:
    log, cs = InMemoryEventLog(), InMemoryContentStore()
    provider = _streaming_provider()
    client = RuntimeLLMClient(
        provider=provider,
        event_log=log,
        content_store=cs,
        delta_sink=_SinkRecorder(),
    )

    client.complete(_req(), _ctx())

    assert provider.streamed_headers == [None]


# ---------------------------------------------------------------------------
# Ephemerality / robustness
# ---------------------------------------------------------------------------


def test_sink_exception_never_fails_the_call() -> None:
    log, cs = InMemoryEventLog(), InMemoryContentStore()
    provider = _streaming_provider()

    def exploding_sink(
        ctx: StepContext, call_id: str, delta: StreamDelta
    ) -> None:
        raise RuntimeError("slow consumer blew up")

    client = RuntimeLLMClient(
        provider=provider,
        event_log=log,
        content_store=cs,
        delta_sink=exploding_sink,
    )

    resp = client.complete(_req(), _ctx())

    assert resp.stop_reason == "end_turn"
    assert [e.type for e in log.read("task-1")] == [
        "LLMRequestStarted",
        "LLMResponseRecorded",
        "LLMRequestFinished",
    ]


def test_ledger_identical_with_and_without_streaming() -> None:
    """A streamed exchange and a batch exchange of the same content produce
    byte-identical EventLog + ContentStore records (the recording invariant
    the streaming decision pins)."""

    def run(delta_sink: Optional[_SinkRecorder]) -> tuple[list, bytes, bytes]:
        log, cs = InMemoryEventLog(), InMemoryContentStore()
        provider = FakeStreamingLLMProvider(
            responses=[_ok_response("same")], deltas=[list(_DELTAS)]
        )
        client = RuntimeLLMClient(
            provider=provider,
            event_log=log,
            content_store=cs,
            id_factory=lambda: "call-FIXED",
            clock=lambda: 0.0,
            delta_sink=delta_sink,
        )
        client.complete(_req(), _ctx())
        events = log.read("task-1")
        started, recorded = events[0], events[1]
        return (
            [e.type for e in events],
            cs.get(started.payload.request_ref),
            cs.get(recorded.payload.response_ref),
        )

    streamed = run(_SinkRecorder())
    batch = run(None)
    assert streamed == batch


# ---------------------------------------------------------------------------
# Shared SSE line parser
# ---------------------------------------------------------------------------


def test_sse_named_events() -> None:
    lines = [
        "event: message_start",
        'data: {"a": 1}',
        "",
        "event: message_stop",
        "data: {}",
        "",
    ]
    assert list(iter_sse_events(lines)) == [
        ("message_start", '{"a": 1}'),
        ("message_stop", "{}"),
    ]


def test_sse_nameless_data_events_and_done_sentinel() -> None:
    lines = ['data: {"delta": "h"}', "", "data: [DONE]", ""]
    assert list(iter_sse_events(lines)) == [
        (None, '{"delta": "h"}'),
        (None, "[DONE]"),
    ]


def test_sse_multiline_data_joins_with_newline() -> None:
    lines = ["data: line1", "data: line2", ""]
    assert list(iter_sse_events(lines)) == [(None, "line1\nline2")]


def test_sse_comments_and_dataless_events_are_skipped() -> None:
    lines = [": keep-alive", "", "event: ping", "", "data: x", ""]
    assert list(iter_sse_events(lines)) == [(None, "x")]


def test_sse_crlf_and_no_space_after_colon() -> None:
    lines = ["event:delta\r", "data:{}\r", "\r"]
    assert list(iter_sse_events(lines)) == [("delta", "{}")]


def test_sse_unterminated_final_event_still_dispatches() -> None:
    lines = ["event: done", 'data: {"ok": true}']
    assert list(iter_sse_events(lines)) == [("done", '{"ok": true}')]
