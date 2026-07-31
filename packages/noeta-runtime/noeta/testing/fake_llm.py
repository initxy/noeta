"""Scripted ``LLMProvider`` doubles — the reference implementations of the
Protocol shape.

They hand back pre-scripted responses and record every request, so a test can
assert on the call pattern; determinism, not realism, is the point.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from noeta.protocols.messages import LLMRequest, LLMResponse, StreamDelta


@dataclass
class FakeLLMProvider:
    """Returns a pre-scripted sequence of :class:`LLMResponse`.

    ``responses`` is iterated in order and ``received_requests`` records every
    call. An exhausted script raises :class:`IndexError` from ``complete`` so a
    runaway test fails loudly instead of looping on the last response.

    ``complete`` is thread-safe (one lock guards the cursor and the request
    log), but the positional cursor is inherently order-dependent and therefore
    unusable when callers run concurrently: the members of a concurrent group
    would race for the cursor and pick each other's answers. Such tests pass a
    ``responder(request) -> LLMResponse`` that routes by request *content*
    instead. The responder runs **outside** the lock, so a deliberately
    blocking responder (a barrier proving wall-clock overlap) cannot serialise
    its own callers.
    """

    responses: list[LLMResponse] = field(default_factory=list)
    received_requests: list[LLMRequest] = field(default_factory=list)
    responder: Optional[Callable[[LLMRequest], LLMResponse]] = None
    _cursor: int = 0
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Records ``request`` before dispatching, so ``received_requests``
        still shows the call that exhausted the script."""
        with self._lock:
            self.received_requests.append(request)
            if self.responder is None:
                if self._cursor >= len(self.responses):
                    raise IndexError(
                        "FakeLLMProvider responses exhausted: scripted "
                        f"{len(self.responses)} response(s) but received "
                        f"{len(self.received_requests)} request(s)"
                    )
                response = self.responses[self._cursor]
                self._cursor += 1
                return response
            responder = self.responder
        # Outside the lock: a blocking responder (barrier / event) must not
        # serialise its concurrent callers.
        return responder(request)


@dataclass
class FakeStreamingLLMProvider:
    """Scripted reference implementation of ``StreamingProvider``.

    Each scripted response may carry a parallel script of
    :class:`StreamDelta` fragments, pushed through ``on_delta`` before the full
    response is returned — the contract real streaming adapters implement.
    ``complete`` serves the same script without deltas, so one instance can
    prove both the streamed and the fallback path; ``streamed_calls`` /
    ``batch_calls`` are how a test tells which path was taken, and
    ``streamed_headers`` is ``None`` for a call the runtime attached no headers
    to.
    """

    responses: list[LLMResponse] = field(default_factory=list)
    deltas: list[list[StreamDelta]] = field(default_factory=list)
    received_requests: list[LLMRequest] = field(default_factory=list)
    streamed_headers: list[Optional[dict[str, str]]] = field(
        default_factory=list
    )
    streamed_calls: int = 0
    batch_calls: int = 0
    _cursor: int = 0
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def _next_scripted(self, request: LLMRequest) -> tuple[LLMResponse, list[StreamDelta]]:
        with self._lock:
            self.received_requests.append(request)
            if self._cursor >= len(self.responses):
                raise IndexError(
                    "FakeStreamingLLMProvider responses exhausted: scripted "
                    f"{len(self.responses)} response(s) but received "
                    f"{len(self.received_requests)} request(s)"
                )
            response = self.responses[self._cursor]
            scripted = (
                self.deltas[self._cursor]
                if self._cursor < len(self.deltas)
                else []
            )
            self._cursor += 1
            return response, scripted

    def complete(self, request: LLMRequest) -> LLMResponse:
        response, _ = self._next_scripted(request)
        with self._lock:
            self.batch_calls += 1
        return response

    def complete_streaming(
        self,
        request: LLMRequest,
        on_delta: Callable[[StreamDelta], None],
        request_headers: Optional[dict[str, str]] = None,
    ) -> LLMResponse:
        response, scripted = self._next_scripted(request)
        with self._lock:
            self.streamed_calls += 1
            self.streamed_headers.append(request_headers)
        for delta in scripted:
            on_delta(delta)
        return response
