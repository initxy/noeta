"""LLM client wrapper — the one layer every provider round-trip goes through.

:class:`RuntimeLLMClient` records each round-trip as three events (Started /
Recorded / Finished) into the EventLog + ContentStore, and holds that
contract on the failure path too: a provider exception is translated into
``LLMResponse(stop_reason="error", ...)``, never re-raised, so Policy decides
what to do from the response. Serialisation must route through
:mod:`noeta.protocols.canonical` — reaching for ``dataclasses.asdict`` +
``json.dumps`` silently drops the ``__canonical_tag__`` keys that let a
reader rebuild typed Blocks.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Mapping, Optional

from noeta.protocols.canonical import (
    from_canonical_bytes,
    to_canonical_bytes,
)
from noeta.protocols.content_store import ContentStore
from noeta.protocols.errors import (
    CATEGORY_FATAL,
    ContextOverflowError,
    FatalError,
    TransientError,
    retry_policy,
)
from noeta.protocols.event_log import EventLog
from noeta.protocols.events import (
    LLMRequestFinishedPayload,
    LLMRequestStartedPayload,
    LLMResponseRecordedPayload,
    LLMRetryScheduledPayload,
    MessageSelection,
)
from noeta.protocols.messages import (
    Block,
    HeaderAwareProvider,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    StreamDelta,
    StreamingProvider,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.step_context import StepContext
from noeta.protocols.values import ContentRef


__all__ = [
    "RuntimeLLMClient",
]


_LLM_MEDIA_TYPE = "application/json"


def _serialize_request(req: LLMRequest) -> bytes:
    return to_canonical_bytes(req)


def _serialize_response(resp: LLMResponse) -> bytes:
    return to_canonical_bytes(resp)


def _deserialize_response(body: bytes) -> LLMResponse:
    """Rebuild an :class:`LLMResponse` from canonical bytes.

    The canonical layer restores tagged Block / Message sub-types on its own;
    the surrounding ``LLMResponse`` is rebuilt here by hand because untagged
    dataclasses do not round-trip through ``from_canonical``.
    """
    raw = from_canonical_bytes(body)
    if not isinstance(raw, dict):
        raise ValueError(
            f"_deserialize_response: expected dict, got {type(raw).__name__}"
        )
    return LLMResponse(
        stop_reason=raw["stop_reason"],
        content=_rebuild_block_list(raw.get("content") or []),
        usage=_rebuild_usage(raw.get("usage")),
        raw=raw.get("raw"),
    )


def _rebuild_usage(value: Any) -> Usage:
    """Rebuild a typed :class:`Usage` from canonical form.

    ``Usage`` carries no ``__canonical_tag__`` (it rides inside the untagged
    ``LLMResponse``), so ``from_canonical`` leaves it a plain dict. A dict
    carrying vendor-shaped keys instead of the known fields restores to an
    empty ``Usage()`` rather than raising.
    """
    if isinstance(value, Usage):
        return value
    if isinstance(value, dict):
        known = {"uncached", "cache_read", "cache_write", "output", "reasoning_tokens"}
        return Usage(**{k: v for k, v in value.items() if k in known})
    return Usage()


def _rebuild_block_list(items: list[Any]) -> list[Block]:
    """Pass restored Block instances through; reject anything still a dict.

    An untagged block means the bytes did not go through
    ``to_canonical_bytes``, and silently handing a dict to a caller expecting
    a Block would surface far from the real fault.
    """
    out: list[Block] = []
    for it in items:
        if isinstance(it, (TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock)):
            out.append(it)
            continue
        raise ValueError(
            "_rebuild_block_list: encountered untagged block "
            f"{it!r} — canonical tag missing, did the serializer go "
            "through to_canonical_bytes?"
        )
    return out


def _put_request(cs: ContentStore, req: LLMRequest) -> ContentRef:
    return cs.put(_serialize_request(req), media_type=_LLM_MEDIA_TYPE)


def _put_response(cs: ContentStore, resp: LLMResponse) -> ContentRef:
    return cs.put(_serialize_response(resp), media_type=_LLM_MEDIA_TYPE)


def _default_id_factory() -> str:
    return f"llm-{uuid.uuid4().hex}"


def _default_clock() -> float:
    return time.monotonic()


def _default_sleep(seconds: float) -> None:
    time.sleep(seconds)


#: Default transient-retry budget. Sized so a persistent rate-limit gets a
#: real recovery window (~1+2+4+8+16+30+30+30s ⇒ ~2 min of waiting) while
#: staying comfortably inside the 600 s lease, so the lease can never expire
#: mid-backoff. A provider-supplied ``Retry-After`` overrides the backoff for
#: that attempt.
_DEFAULT_MAX_RETRIES = 8


def _error_response(exc: Exception) -> LLMResponse:
    """Translate a provider exception into a typed error ``LLMResponse``.

    The error *category* (transient / overflow / fatal) rides inside ``raw``
    so Policy can branch on it without re-deriving the exception class. An
    untranslated exception is bucketed ``fatal``: the conservative default,
    which maps to a non-retryable decision rather than a loop.
    """
    category = getattr(exc, "category", CATEGORY_FATAL)
    retry_after = getattr(exc, "retry_after", None)
    return LLMResponse(
        stop_reason="error",
        content=[],
        usage=Usage(),
        raw={
            "error": str(exc),
            "category": category,
            "retry_after": retry_after,
        },
    )


def _call_provider(
    provider: LLMProvider,
    req: LLMRequest,
    ctx: StepContext,
    provider_headers: Optional[Callable[[StepContext], Mapping[str, str]]] = None,
    on_delta: Optional[Callable[[StreamDelta], None]] = None,
) -> LLMResponse:
    # Streaming subsumes the header capability (its signature already carries
    # request_headers), so probing streaming first keeps the two optional
    # Protocols from forming a matrix of combinations.
    if on_delta is not None and isinstance(provider, StreamingProvider):
        headers = (
            dict(provider_headers(ctx)) if provider_headers is not None else None
        )
        return provider.complete_streaming(req, on_delta, headers)
    if provider_headers is not None and isinstance(provider, HeaderAwareProvider):
        return provider.complete_with_headers(req, dict(provider_headers(ctx)))
    return provider.complete(req)


class RuntimeLLMClient:
    """Records every LLM round-trip into EventLog + ContentStore.

    The three-event contract holds on the failure path too: a provider
    exception becomes ``LLMResponse(stop_reason="error", ...)`` and the
    Recorded / Finished events are written before that response reaches the
    caller, so Policy — not this layer — decides what a failure means.
    """

    def __init__(
        self,
        provider: LLMProvider,
        event_log: EventLog,
        content_store: ContentStore,
        *,
        id_factory: Optional[Callable[[], str]] = None,
        clock: Optional[Callable[[], float]] = None,
        pricing: Optional[Callable[[str, Usage], float]] = None,
        provider_headers: Optional[Callable[[StepContext], Mapping[str, str]]] = None,
        delta_sink: Optional[
            Callable[[StepContext, str, StreamDelta], None]
        ] = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._provider = provider
        self._event_log = event_log
        self._content_store = content_store
        self._id_factory = id_factory or _default_id_factory
        self._clock = clock or _default_clock
        # ``pricing(model, usage) -> USD`` is injected because the kernel
        # never imports the providers built-in; ``None`` prices at 0.0.
        self._pricing = pricing
        self._provider_headers = provider_headers
        # ``delta_sink(ctx, call_id, delta)`` receives ephemeral StreamDeltas
        # while a streaming-capable provider's call is in flight. Deltas are
        # never recorded — the trio plus MessagesAppended stay the only
        # durable record of a round-trip.
        self._delta_sink = delta_sink
        # Injectable sleep so tests never wall-clock-sleep through a backoff.
        self._max_retries = max_retries
        self._sleep = sleep or _default_sleep

    def complete(
        self,
        req: LLMRequest,
        ctx: StepContext,  # noqa: ARG002
        *,
        selection: Optional[MessageSelection] = None,
        allow_stream: bool = True,
    ) -> LLMResponse:
        call_id = self._id_factory()
        request_ref = _put_request(self._content_store, req)

        # ``allow_stream=False`` is the per-call opt-out for round-trips that
        # are not user-facing output (the compaction summarize call). Sink
        # exceptions are swallowed: deltas are observational and must never
        # fail or retry an LLM call.
        on_delta: Optional[Callable[[StreamDelta], None]] = None
        if allow_stream and self._delta_sink is not None:
            sink = self._delta_sink

            def on_delta(delta: StreamDelta) -> None:
                try:
                    sink(ctx, call_id, delta)
                except Exception:  # noqa: BLE001 — observational channel
                    pass

        # ``selection`` (the policy's message-selection provenance) is
        # event-only metadata and deliberately NOT part of ``req`` /
        # ``request_ref``, so recording it leaves the request hash alone.
        # This is its single writer.
        self._emit(
            ctx,
            type="LLMRequestStarted",
            payload=LLMRequestStartedPayload(
                call_id=call_id,
                model=req.model,
                request_ref=request_ref,
                input_tokens=0,
                selection=selection,
            ),
        )

        # One logical request emits exactly ONE trio however many times the
        # retry loop below re-calls the provider, so a resume that folds the
        # EventLog rebuilds the same state.
        t0 = self._clock()
        resp = self._invoke_with_retry(
            req, ctx, call_id=call_id, on_delta=on_delta
        )
        t1 = self._clock()
        latency_ms = max(0, int((t1 - t0) * 1000))

        response_ref = _put_response(self._content_store, resp)
        self._emit(
            ctx,
            type="LLMResponseRecorded",
            payload=LLMResponseRecordedPayload(
                call_id=call_id,
                response_ref=response_ref,
                stop_reason=resp.stop_reason,
                output_tokens=resp.usage.output,
            ),
        )

        # cost_usd is recorded INTO the event, so a fold reads the price as it
        # stood at the call and a later price-table change never rewrites past
        # recordings. The error path carries an empty Usage and therefore
        # prices at 0.0 without polluting any accumulator.
        cost_usd = (
            self._pricing(req.model, resp.usage)
            if self._pricing is not None
            else 0.0
        )
        self._emit(
            ctx,
            type="LLMRequestFinished",
            payload=LLMRequestFinishedPayload(
                call_id=call_id,
                success=resp.stop_reason != "error",
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                usage=resp.usage,
            ),
        )

        return resp

    def _emit(self, ctx: StepContext, *, type: str, payload: Any) -> None:
        """Append one LLM event, then fold it onto the Engine's in-memory task.

        The apply half is what keeps ``fold(events) → state`` equal to the
        runtime state INSIDE a tool loop. Without it the Engine never sees this
        client's emits, ``RuntimeState.last_input_tokens`` stays pinned at the
        entry fold's value for the whole turn, and every consumer of the
        real-usage baseline silently degrades to a chars/4 estimate. The Engine
        supplies the applier bound to the task it is stepping, so it remains
        the sole physical writer.

        Only ``LLMRequestFinished`` carries state; the others fold as no-ops,
        so routing all four through here costs nothing and leaves no second
        emit site to forget.
        """
        env = self._event_log.emit(
            task_id=ctx.task_id,
            type=type,
            payload=payload,
            trace_id=ctx.trace_id,
            actor="llm",
            origin="llm",
        )
        if ctx.apply_event is not None:
            ctx.apply_event(env)

    def _invoke_with_retry(
        self,
        req: LLMRequest,
        ctx: StepContext,
        *,
        call_id: str,
        on_delta: Optional[Callable[[StreamDelta], None]] = None,
    ) -> LLMResponse:
        """Call the provider, retrying transient failures with backoff.

        Returns a normal :class:`LLMResponse`, or a typed error response
        (``stop_reason="error"`` + ``raw['category']``) when the failure is
        non-transient or the budget is exhausted. Retries are live-only: an
        intermediate attempt writes no request/response trio, so a resume
        folds the same state either way. Each scheduled backoff does record an
        ``LLMRetryScheduled`` — a fold no-op, present purely so a live
        consumer sees "rate-limited, retrying" instead of a silent stall.
        """
        attempt = 0
        while True:
            try:
                return _call_provider(
                    self._provider,
                    req,
                    ctx,
                    self._provider_headers,
                    on_delta,
                )
            except (
                TransientError,
                ContextOverflowError,
                FatalError,
            ) as exc:
                # ``None`` delay ⇒ non-transient (overflow / fatal): surface
                # it immediately rather than burning the budget.
                delay = retry_policy(exc, attempt=attempt)
                if delay is None or attempt >= self._max_retries:
                    return _error_response(exc)
                attempt += 1
                self._emit(
                    ctx,
                    type="LLMRetryScheduled",
                    payload=LLMRetryScheduledPayload(
                        call_id=call_id,
                        attempt=attempt,
                        max_retries=self._max_retries,
                        delay_seconds=delay,
                        category=getattr(exc, "category", CATEGORY_FATAL),
                        error=str(exc)[:500],
                    ),
                )
                self._sleep(delay)
            except Exception as exc:  # noqa: BLE001 — protocol contract
                # A provider that did not translate its failure cleanly:
                # bucket it fatal, so an unrecognised error cannot loop.
                return _error_response(exc)
