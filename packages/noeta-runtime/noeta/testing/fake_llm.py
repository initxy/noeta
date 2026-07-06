"""FakeLLMProvider: scripted reference implementation of ``LLMProvider``.

Designed for tests and for documenting the Protocol shape. Hands back
pre-scripted :class:`LLMResponse` objects in order and records each
received :class:`LLMRequest` so tests can assert on the call pattern.

Per the Phase 1 PRD layering, ``FakeLLMProvider`` lives in
``noeta.testing`` so production layers cannot accidentally depend on it
(import-linter enforces). Tests and the future
``examples/phase1_react_demo.py`` import it freely.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from noeta.protocols.messages import LLMRequest, LLMResponse, StreamDelta


@dataclass
class FakeLLMProvider:
    """Returns a pre-scripted sequence of :class:`LLMResponse`.

    ``responses`` is iterated in order; ``received_requests`` records
    every call. When the script is exhausted, ``complete`` raises
    :class:`IndexError` so a runaway test surfaces loudly rather than
    silently looping on the last response.

    fan-out v2: ``complete`` is
    **thread-safe** (a single lock guards the cursor + request log), and an
    optional ``responder(request) -> LLMResponse`` routes by request *content*
    instead of the global cursor. A concurrent group's members each call
    ``complete`` on their own thread; the positional cursor is order-dependent
    and so unusable for them, but a content ``responder`` hands each member a
    deterministic response keyed off its own request. The responder is invoked
    **outside** the lock so a deliberately-blocking responder (e.g. a barrier
    that proves wall-clock overlap) cannot serialise its callers.
    """

    responses: list[LLMResponse] = field(default_factory=list)
    received_requests: list[LLMRequest] = field(default_factory=list)
    responder: Optional[Callable[[LLMRequest], LLMResponse]] = None
    _cursor: int = 0
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Hand back a scripted response (positional) or route by content.

        Records ``request`` first so assertions on ``received_requests``
        still see the call that triggered the eventual exhaustion error.
        """
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
        # Content-routed mode: call outside the lock so a blocking responder
        # (barrier / event) does not serialise concurrent callers.
        return responder(request)


@dataclass
class FakeStreamingLLMProvider:
    """Scripted reference implementation of ``StreamingProvider``.

    Like :class:`FakeLLMProvider`, but each scripted response may carry a
    parallel script of :class:`StreamDelta` fragments: ``complete_streaming``
    fires them through ``on_delta`` in order, then returns the full response —
    the push-shaped contract real streaming adapters implement. ``complete``
    serves the same script *without* deltas, so a single instance can prove
    both the streamed and the fallback path of the runtime probe.

    ``streamed_headers`` records the ``request_headers`` each streaming call
    received (``None`` when the runtime attached none); ``streamed_calls`` /
    ``batch_calls`` count which path the probe actually took.
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
