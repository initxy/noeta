"""RuntimeLLMClient behaviour tests.

Drives the ``complete(req, ctx) -> LLMResponse`` interface end-to-end.
Each test isolates one behaviour of the runtime client: the three-event
recording contract, error translation, and the transient backoff loop.
"""

from __future__ import annotations

from typing import Any

import pytest

from noeta.protocols.canonical import from_canonical_bytes, to_canonical_bytes
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.step_context import StepContext
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.fake_llm import FakeLLMProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(task_id: str = "task-1") -> StepContext:
    return StepContext(
        task_id=task_id, lease_id="lease-1", trace_id="trace-1"
    )


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


def _tool_use_response(call_id: str = "tc-1") -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=call_id,
                tool_name="echo",
                arguments={"text": "hi"},
            )
        ],
        usage=Usage(uncached=2, output=3),
        raw={"id": "resp-tool"},
    )


# ---------------------------------------------------------------------------
# RuntimeLLMClient — success path
# ---------------------------------------------------------------------------


def test_normal_client_emits_three_events_on_success() -> None:
    from noeta.runtime.llm import RuntimeLLMClient

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    provider = FakeLLMProvider(responses=[_ok_response("hello")])
    client = RuntimeLLMClient(
        provider=provider, event_log=log, content_store=cs
    )

    resp = client.complete(_req(), _ctx())

    assert isinstance(resp, LLMResponse)
    assert resp.stop_reason == "end_turn"
    assert resp.content == [TextBlock(text="hello")]

    events = log.read("task-1")
    assert [e.type for e in events] == [
        "LLMRequestStarted",
        "LLMResponseRecorded",
        "LLMRequestFinished",
    ]
    # All three events share the same call_id.
    call_ids = {e.payload.call_id for e in events}
    assert len(call_ids) == 1
    # Envelope filled from ctx.
    assert all(e.task_id == "task-1" for e in events)
    assert all(e.trace_id == "trace-1" for e in events)
    # LLMRequestFinished.success=True on success.
    finished = events[2]
    assert finished.payload.success is True
    # Request body resolvable from ContentStore.
    started = events[0]
    body = cs.get(started.payload.request_ref)
    rebuilt_req = from_canonical_bytes(body)
    assert rebuilt_req["model"] == "gpt-x"
    # Response body resolvable from ContentStore.
    recorded = events[1]
    resp_body = cs.get(recorded.payload.response_ref)
    rebuilt_resp = from_canonical_bytes(resp_body)
    assert rebuilt_resp["stop_reason"] == "end_turn"


def test_normal_client_uses_injected_id_factory_for_call_id() -> None:
    """call_id is mintable via ``id_factory``."""
    from noeta.runtime.llm import RuntimeLLMClient

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    provider = FakeLLMProvider(responses=[_ok_response("h")])
    minted = iter(["call-FIXED-1"])
    client = RuntimeLLMClient(
        provider=provider,
        event_log=log,
        content_store=cs,
        id_factory=lambda: next(minted),
    )

    client.complete(_req(), _ctx())

    events = log.read("task-1")
    assert all(e.payload.call_id == "call-FIXED-1" for e in events)


def test_normal_client_persists_latency_ms_on_finished() -> None:
    """LLMRequestFinished carries the wall-clock provider duration so cost
    / observability consumers can fold it without re-running the call.
    ``clock`` is injected."""
    from noeta.runtime.llm import RuntimeLLMClient

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    provider = FakeLLMProvider(responses=[_ok_response("h")])
    # Clock advances by 0.250s between the two reads bracketing the
    # provider call (RuntimeLLMClient.complete reads twice).
    ticks = iter([0.000, 0.250])
    client = RuntimeLLMClient(
        provider=provider,
        event_log=log,
        content_store=cs,
        clock=lambda: next(ticks),
    )

    client.complete(_req(), _ctx())

    finished = log.read("task-1")[2]
    assert finished.type == "LLMRequestFinished"
    assert finished.payload.latency_ms == 250


# ---------------------------------------------------------------------------
# RuntimeLLMClient — failure path
# ---------------------------------------------------------------------------


class _ExplodingProvider:
    """Provider whose ``complete`` raises a predictable RuntimeError."""

    def complete(self, request: LLMRequest) -> LLMResponse:  # noqa: ARG002
        raise RuntimeError("kaboom")


class _HeaderAwareProvider:
    def __init__(self) -> None:
        self.headers: list[dict[str, str]] = []

    def complete(self, request: LLMRequest) -> LLMResponse:  # noqa: ARG002
        raise AssertionError("complete_with_headers should be used")

    def complete_with_headers(
        self,
        request: LLMRequest,  # noqa: ARG002
        headers: dict[str, str],
    ) -> LLMResponse:
        self.headers.append(dict(headers))
        return _ok_response("header-ok")


def test_normal_client_passes_task_id_provider_headers_without_changing_request() -> None:
    """Deployment wire headers are derived from StepContext.task_id per call,
    not persisted into the canonical LLMRequest."""
    from noeta.runtime.llm import RuntimeLLMClient

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    provider = _HeaderAwareProvider()
    client = RuntimeLLMClient(
        provider=provider,
        event_log=log,
        content_store=cs,
        provider_headers=lambda ctx: {
            "extra": f'{{"session_id":"{ctx.task_id}"}}',
            "X-TT-logid": ctx.task_id,
        },
    )

    client.complete(_req(), _ctx(task_id="task-abc"))

    assert provider.headers == [
        {
            "extra": '{"session_id":"task-abc"}',
            "X-TT-logid": "task-abc",
        }
    ]
    started = log.read("task-abc")[0]
    body = cs.get(started.payload.request_ref)
    assert body == to_canonical_bytes(_req())
    assert "session_id" not in body.decode("utf-8")


def test_normal_client_emits_three_events_on_provider_exception() -> None:
    """Failure path translates exception → error response and still writes
    the three-event contract; nothing propagates upward."""
    from noeta.runtime.llm import RuntimeLLMClient

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    client = RuntimeLLMClient(
        provider=_ExplodingProvider(), event_log=log, content_store=cs
    )

    resp = client.complete(_req(), _ctx())

    # No raise upward; caller sees a typed error response.
    assert isinstance(resp, LLMResponse)
    assert resp.stop_reason == "error"
    assert resp.content == []
    # ② error recovery: a bare (untranslated) provider exception is bucketed
    # as fatal so Policy can branch on raw['category']; the original message
    # is preserved under 'error'.
    assert resp.raw == {
        "error": "kaboom",
        "category": "fatal",
        "retry_after": None,
    }

    events = log.read("task-1")
    assert [e.type for e in events] == [
        "LLMRequestStarted",
        "LLMResponseRecorded",
        "LLMRequestFinished",
    ]
    # Recorded reflects the error response shape.
    assert events[1].payload.stop_reason == "error"
    # Finished.success = False on error.
    assert events[2].payload.success is False


# ---------------------------------------------------------------------------
# RuntimeLLMClient — ② error recovery: transient backoff + category buckets
# ---------------------------------------------------------------------------
#
# The transient backoff loop is a LIVE-only loop *around the provider call*
# inside ``complete``. One logical request records exactly one trio (Started
# once / Recorded + Finished once); intermediate failed provider calls write
# no request/response trio — each scheduled backoff records only an
# observational ``LLMRetryScheduled`` marker (a fold no-op) so a live
# consumer can see the stall.


class _ScriptedProvider:
    """Provider whose ``complete`` raises / returns from a scripted list.

    Each entry is either an ``Exception`` (raised) or an ``LLMResponse``
    (returned). Tracks how many times ``complete`` was invoked.
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:  # noqa: ARG002
        item = self._script[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def _fake_sleep_recorder() -> tuple[Any, list[float]]:
    slept: list[float] = []

    def _sleep(delay: float) -> None:
        slept.append(delay)

    return _sleep, slept


def test_transient_retried_until_success_records_one_trio() -> None:
    """Provider throws TransientError once, then succeeds → RuntimeLLMClient
    sleeps once (delay from retry_policy) and returns the success response;
    the EventLog carries exactly one Started/Recorded/Finished trio plus one
    observational ``LLMRetryScheduled`` marker for the scheduled backoff."""
    from noeta.protocols.errors import TransientError
    from noeta.runtime.llm import RuntimeLLMClient

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    provider = _ScriptedProvider(
        [TransientError("rate limited"), _ok_response("recovered")]
    )
    sleep, slept = _fake_sleep_recorder()
    client = RuntimeLLMClient(
        provider=provider,
        event_log=log,
        content_store=cs,
        max_retries=2,
        sleep=sleep,
    )

    resp = client.complete(_req(), _ctx())

    assert resp.stop_reason == "end_turn"
    assert resp.content == [TextBlock(text="recovered")]
    assert provider.calls == 2
    # Slept exactly once, with the attempt=0 backoff delay. Equal jitter puts
    # it in the band [0.5, 1.0] (temp/2 floor .. temp ceil for base 1.0).
    assert len(slept) == 1
    assert 0.5 <= slept[0] <= 1.0
    # Exactly one trio; the failed attempt left only its retry marker.
    events = log.read("task-1")
    assert [e.type for e in events] == [
        "LLMRequestStarted",
        "LLMRetryScheduled",
        "LLMResponseRecorded",
        "LLMRequestFinished",
    ]
    started = events[0].payload
    marker = events[1].payload
    assert marker.call_id == started.call_id
    assert marker.attempt == 1
    assert marker.max_retries == 2
    assert marker.delay_seconds == slept[0]
    assert marker.category == "transient"
    assert "rate limited" in marker.error


def test_malformed_tool_arguments_is_retried_then_succeeds() -> None:
    """A provider that truncates a tool call's arguments JSON
    (``MalformedToolArgumentsError``) is bucketed transient: the runtime
    re-issues the request and recovers, rather than failing the task on one
    flaky response. Mirrors the plain-transient retry contract — one trio."""
    from noeta.protocols.errors import MalformedToolArgumentsError
    from noeta.runtime.llm import RuntimeLLMClient

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    provider = _ScriptedProvider(
        [
            MalformedToolArgumentsError(
                "function_call arguments not JSON-decodable: "
                "Unterminated string starting at: line 1 column 92 (char 91)"
            ),
            _ok_response("recovered"),
        ]
    )
    sleep, slept = _fake_sleep_recorder()
    client = RuntimeLLMClient(
        provider=provider,
        event_log=log,
        content_store=cs,
        max_retries=2,
        sleep=sleep,
    )

    resp = client.complete(_req(), _ctx())

    assert resp.stop_reason == "end_turn"
    assert resp.content == [TextBlock(text="recovered")]
    assert provider.calls == 2
    assert len(slept) == 1
    assert [e.type for e in log.read("task-1")] == [
        "LLMRequestStarted",
        "LLMRetryScheduled",
        "LLMResponseRecorded",
        "LLMRequestFinished",
    ]


def test_malformed_tool_arguments_budget_exhausted_is_transient_not_fatal() -> None:
    """Persisting malformed arguments past the budget → error response stamped
    ``category='transient'`` (NOT 'fatal'): the bug this fixes was a truncated
    tool call being bucketed fatal and killing the task with zero retries."""
    from noeta.protocols.errors import MalformedToolArgumentsError
    from noeta.runtime.llm import RuntimeLLMClient

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    provider = _ScriptedProvider(
        [MalformedToolArgumentsError("function_call arguments not JSON-decodable: x")
         for _ in range(5)]
    )
    sleep, _slept = _fake_sleep_recorder()
    client = RuntimeLLMClient(
        provider=provider,
        event_log=log,
        content_store=cs,
        max_retries=2,
        sleep=sleep,
    )

    resp = client.complete(_req(), _ctx())

    assert resp.stop_reason == "error"
    assert resp.raw is not None
    assert resp.raw["category"] == "transient"
    assert provider.calls == 3  # 1 initial + 2 retries


def test_transient_honours_retry_after_for_sleep_delay() -> None:
    from noeta.protocols.errors import TransientError
    from noeta.runtime.llm import RuntimeLLMClient

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    provider = _ScriptedProvider(
        [TransientError("slow down", retry_after=4.0), _ok_response()]
    )
    sleep, slept = _fake_sleep_recorder()
    client = RuntimeLLMClient(
        provider=provider,
        event_log=log,
        content_store=cs,
        max_retries=2,
        sleep=sleep,
    )

    client.complete(_req(), _ctx())

    assert slept == [4.0]


def test_transient_budget_exhausted_becomes_error_response() -> None:
    """Provider keeps throwing TransientError past the retry budget →
    translated to an error response stamped raw['category']='transient',
    still exactly one trio, success=False."""
    from noeta.protocols.errors import TransientError
    from noeta.runtime.llm import RuntimeLLMClient

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    provider = _ScriptedProvider(
        [TransientError("nope") for _ in range(5)]
    )
    sleep, slept = _fake_sleep_recorder()
    client = RuntimeLLMClient(
        provider=provider,
        event_log=log,
        content_store=cs,
        max_retries=2,
        sleep=sleep,
    )

    resp = client.complete(_req(), _ctx())

    assert resp.stop_reason == "error"
    assert resp.raw is not None
    assert resp.raw["category"] == "transient"
    # max_retries=2 → 1 initial + 2 retries = 3 provider calls, 2 sleeps.
    assert provider.calls == 3
    # Equal-jitter bands per attempt: [0.5, 1.0] (attempt 0), [1.0, 2.0]
    # (attempt 1). The floor keeps each wait real; the band decorrelates.
    assert len(slept) == 2
    # One retry marker per scheduled backoff (attempt 1, 2); still one trio.
    markers = [e.payload for e in log.read("task-1") if e.type == "LLMRetryScheduled"]
    assert [m.attempt for m in markers] == [1, 2]
    assert [
        e.type for e in log.read("task-1") if e.type != "LLMRetryScheduled"
    ] == ["LLMRequestStarted", "LLMResponseRecorded", "LLMRequestFinished"]
    assert 0.5 <= slept[0] <= 1.0
    assert 1.0 <= slept[1] <= 2.0
    assert [e.type for e in log.read("task-1")] == [
        "LLMRequestStarted",
        "LLMRetryScheduled",
        "LLMRetryScheduled",
        "LLMResponseRecorded",
        "LLMRequestFinished",
    ]


def test_overflow_not_retried_carries_category_and_retry_after() -> None:
    from noeta.protocols.errors import ContextOverflowError
    from noeta.runtime.llm import RuntimeLLMClient

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    provider = _ScriptedProvider([ContextOverflowError("prompt too long")])
    sleep, slept = _fake_sleep_recorder()
    client = RuntimeLLMClient(
        provider=provider,
        event_log=log,
        content_store=cs,
        max_retries=2,
        sleep=sleep,
    )

    resp = client.complete(_req(), _ctx())

    assert resp.stop_reason == "error"
    assert resp.raw is not None
    assert resp.raw["category"] == "overflow"
    assert resp.raw["retry_after"] is None
    # No retry on overflow.
    assert provider.calls == 1
    assert slept == []


def test_fatal_not_retried_carries_category() -> None:
    from noeta.protocols.errors import FatalError
    from noeta.runtime.llm import RuntimeLLMClient

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    provider = _ScriptedProvider([FatalError("unauthorized")])
    sleep, slept = _fake_sleep_recorder()
    client = RuntimeLLMClient(
        provider=provider,
        event_log=log,
        content_store=cs,
        max_retries=2,
        sleep=sleep,
    )

    resp = client.complete(_req(), _ctx())

    assert resp.stop_reason == "error"
    assert resp.raw is not None
    assert resp.raw["category"] == "fatal"
    assert provider.calls == 1
    assert slept == []


def test_client_does_not_expose_reset_or_per_task_dict() -> None:
    """The client is a per-task instance — no reset API and no
    dict[task_id, cursor] index."""
    from noeta.runtime.llm import RuntimeLLMClient

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    provider = FakeLLMProvider(responses=[_ok_response()])
    client = RuntimeLLMClient(
        provider=provider, event_log=log, content_store=cs
    )

    assert not hasattr(client, "reset")
    # No dict[task_id, cursor] index → cursor (if any) is a bare attr.
    for name, val in vars(client).items():
        assert not (
            isinstance(val, dict)
            and val
            and isinstance(next(iter(val.keys())), str)
            and "task" in next(iter(val.keys()))
        ), f"{name} looks like a per-task dict index"


# ---------------------------------------------------------------------------
# OpenAICompatProvider end-to-end integration (respx + Normal)
# ---------------------------------------------------------------------------


@pytest.fixture
def openai_normal_recording() -> tuple[
    InMemoryEventLog, InMemoryContentStore, LLMRequest, LLMResponse
]:
    """Run RuntimeLLMClient against an OpenAICompatProvider with a respx
    HTTP mock; return the resulting log + store + the request/response
    that was driven."""
    import respx
    import httpx

    from noeta.providers.openai_compat import OpenAICompatProvider
    from noeta.runtime.llm import RuntimeLLMClient

    base_url = "https://example.test/v1"
    payload = {
        "id": "chatcmpl-1",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "hello back",
                    "tool_calls": [],
                },
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }

    req = LLMRequest(
        model="gpt-x",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
    )

    log = InMemoryEventLog()
    cs = InMemoryContentStore()

    with respx.mock(assert_all_called=False) as router:
        router.post(f"{base_url}/chat/completions").mock(
            return_value=httpx.Response(200, json=payload)
        )
        provider = OpenAICompatProvider(base_url=base_url, api_key="sk-test")
        client = RuntimeLLMClient(
            provider=provider, event_log=log, content_store=cs
        )
        resp = client.complete(req, _ctx())

    return log, cs, req, resp


def test_openai_compat_end_to_end_normal_records_three_events(
    openai_normal_recording: tuple[
        InMemoryEventLog, InMemoryContentStore, LLMRequest, LLMResponse
    ],
) -> None:
    log, cs, _req_used, resp = openai_normal_recording

    events = log.read("task-1")
    assert [e.type for e in events] == [
        "LLMRequestStarted",
        "LLMResponseRecorded",
        "LLMRequestFinished",
    ]
    assert resp.stop_reason == "end_turn"
    assert resp.content == [TextBlock(text="hello back")]
    # Both refs resolve to bodies in ContentStore.
    cs.get(events[0].payload.request_ref)
    cs.get(events[1].payload.response_ref)
