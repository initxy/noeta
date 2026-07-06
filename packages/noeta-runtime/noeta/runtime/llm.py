"""LLM client wrapper — Phase 1 issue 12.

The client class exposes a ``complete(req, ctx) -> LLMResponse`` shape, so
Policy injection routes every LLM call through this one wrapper layer:

* :class:`RuntimeLLMClient`  — calls a real :class:`LLMProvider`, records
                              the three LLM events (Started / Recorded /
                              Finished) into EventLog + ContentStore. On
                              provider exception the three-event contract
                              is preserved: the exception is
                              translated into ``LLMResponse(stop_reason=
                              "error", ...)``, all three events still fire,
                              and the caller receives the error response
                              without an upstream raise.

The *single point of canonical bytes* in Noeta is
:mod:`noeta.protocols.canonical`. The helpers below route every
serialisation through ``to_canonical_bytes`` (and the inverse through
``from_canonical_bytes``); going around them with ``dataclasses.asdict``
+ ``json.dumps`` would silently drop the ``__canonical_tag__`` keys that
let callers rebuild typed Blocks, which is the most common Phase-1
foot-gun.
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


# ---------------------------------------------------------------------------
# Shared serialisation helpers (single canonical layer).
# ---------------------------------------------------------------------------


_LLM_MEDIA_TYPE = "application/json"


def _serialize_request(req: LLMRequest) -> bytes:
    """Canonicalise an :class:`LLMRequest` to stable bytes."""
    return to_canonical_bytes(req)


def _serialize_response(resp: LLMResponse) -> bytes:
    """Canonicalise an :class:`LLMResponse` to stable bytes."""
    return to_canonical_bytes(resp)


def _deserialize_response(body: bytes) -> LLMResponse:
    """Rebuild an :class:`LLMResponse` from canonical bytes.

    The canonical layer (issue 10 ``register`` calls in
    :mod:`noeta.protocols.messages`) restores tagged Block / Message
    sub-types; the surrounding ``LLMResponse`` dataclass is rebuilt by
    this helper directly because untagged dataclasses do not round-trip
    through ``from_canonical`` automatically.
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

    ``Usage`` carries no ``__canonical_tag__`` (it rides inside the
    untagged ``LLMResponse``), so ``from_canonical`` leaves it a
    stored-field dict; this helper rebuilds the typed object. Old
    recordings whose ``usage`` was a legacy bare dict with vendor keys
    (e.g. ``input_tokens`` / ``total_tokens``) restore to an empty
    ``Usage()``. ``None`` / missing → empty.
    """
    if isinstance(value, Usage):
        return value
    if isinstance(value, dict):
        known = {"uncached", "cache_read", "cache_write", "output", "reasoning_tokens"}
        return Usage(**{k: v for k, v in value.items() if k in known})
    return Usage()


def _rebuild_block_list(items: list[Any]) -> list[Block]:
    """Some Block variants come back as typed instances via the canonical
    restorers; legacy paths that dropped the tag remain dicts. Pass typed
    instances through and reject the latter so callers see the regression
    loudly.
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


#: Default transient-retry budget for ``RuntimeLLMClient`` (README D-2d).
#: A provider-neutral budget: the retry loop is LIVE-only — intermediate
#: attempts record no request/response trio, only an observational
#: ``LLMRetryScheduled`` marker per backoff (so the frontend can show
#: "retrying" instead of a silent stall). Sized so a persistent 429
#: rate-limit gets a real recovery window (~1+2+4+8+16+30+30+30s ⇒ ~2min of
#: waiting across 8 retries; a gateway that stays saturated past the 16s
#: rung usually needs tens of seconds, not another doubling) — still
#: comfortably inside the driver's 600s lease, so the lease never expires
#: mid-backoff. A provider-supplied ``Retry-After`` overrides the backoff per
#: attempt (see ``retry_policy``).
_DEFAULT_MAX_RETRIES = 8


def _error_response(exc: Exception) -> LLMResponse:
    """Translate a provider exception into a typed error ``LLMResponse``.

    ② error recovery: the error *category* (transient / overflow / fatal)
    rides inside ``raw`` (a dict), letting Policy branch on
    ``raw['category']`` without re-deriving the class. A bare (untranslated)
    exception is bucketed ``fatal`` — the conservative default that maps to
    a non-retryable ``FailDecision`` and preserves the historical contract
    that an unrecognised provider failure does not loop.
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
    # Probe order: streaming → header-aware → plain. Streaming subsumes the
    # header capability (its signature carries request_headers) so the two
    # optional Protocols never form a probe matrix.
    if on_delta is not None and isinstance(provider, StreamingProvider):
        headers = (
            dict(provider_headers(ctx)) if provider_headers is not None else None
        )
        return provider.complete_streaming(req, on_delta, headers)
    if provider_headers is not None and isinstance(provider, HeaderAwareProvider):
        return provider.complete_with_headers(req, dict(provider_headers(ctx)))
    return provider.complete(req)


# ---------------------------------------------------------------------------
# RuntimeLLMClient
# ---------------------------------------------------------------------------


class RuntimeLLMClient:
    """Records every LLM round-trip into EventLog + ContentStore.

    The three-event contract is preserved on both success
    and provider exception paths: an exception is translated into
    ``LLMResponse(stop_reason="error", content=[], usage=Usage(),
    raw={"error": str(e)})`` and the LLMResponseRecorded / LLMRequestFinished events
    are written before the response is returned to the caller. Policy
    sees ``stop_reason="error"`` and decides what to do (Phase 1
    first-slice ReActPolicy produces ``FailDecision(retryable=False)``).
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
        # ① billing: provider-neutral pricing is INJECTED (runtime must not
        # import noeta.providers). ``pricing(model, usage) -> USD``
        # is supplied by the code-layer wiring (catalog.price); ``None`` keeps
        # the stub / no-pricing path at cost_usd=0.0.
        self._pricing = pricing
        self._provider_headers = provider_headers
        # Token-streaming seam: ``delta_sink(ctx, call_id, delta)`` receives
        # ephemeral StreamDeltas while a streaming-capable provider's call is
        # in flight. Host wiring only (the product's delta hub); ``None`` (the
        # default, and every headless SDK caller) keeps the non-streaming
        # provider paths byte-identical to today. Deltas are never recorded —
        # the trio + MessagesAppended stay the only durable record.
        self._delta_sink = delta_sink
        # ② error recovery: provider-neutral transient-retry budget + an
        # injectable sleep (so tests never wall-clock-sleep). The
        # retry loop is LIVE-only and writes no events (README D-2d).
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

        # Token streaming: bind the injected sink to this trio's identity.
        # ``allow_stream=False`` is the per-call opt-out for round-trips that
        # are not user-facing output (the compaction summarize call). Sink
        # exceptions are swallowed — deltas are observational and must never
        # fail or retry an LLM call.
        on_delta: Optional[Callable[[StreamDelta], None]] = None
        if allow_stream and self._delta_sink is not None:
            sink = self._delta_sink

            def on_delta(delta: StreamDelta) -> None:
                try:
                    sink(ctx, call_id, delta)
                except Exception:  # noqa: BLE001 — observational channel
                    pass

        # 1. LLMRequestStarted — MS1: persist the policy's message-selection
        # provenance (counts + strategy). It is event-only metadata: it is
        # NOT part of ``req`` / ``request_ref`` (request bytes/hash unchanged).
        # This is the single writer of ``selection``.
        self._event_log.emit(
            task_id=ctx.task_id,
            type="LLMRequestStarted",
            payload=LLMRequestStartedPayload(
                call_id=call_id,
                model=req.model,
                request_ref=request_ref,
                input_tokens=0,
                selection=selection,
            ),
            trace_id=ctx.trace_id,
            actor="llm",
            origin="llm",
        )

        # 2. Invoke provider, wrapped in the LIVE-only transient-retry loop
        # (README D-2d). Intermediate failed attempts record no
        # request/response trio — one logical request emits exactly one trio,
        # so a resume that folds the EventLog rebuilds the same state — but
        # each scheduled backoff emits an observational ``LLMRetryScheduled``
        # (a fold no-op) so a live consumer sees the stall. Non-transient
        # categories (overflow / fatal) and a budget-exhausted transient are
        # translated into a single error response carrying ``raw['category']``.
        t0 = self._clock()
        resp = self._invoke_with_retry(
            req, ctx, call_id=call_id, on_delta=on_delta
        )
        t1 = self._clock()
        latency_ms = max(0, int((t1 - t0) * 1000))

        # 3. LLMResponseRecorded
        response_ref = _put_response(self._content_store, resp)
        self._event_log.emit(
            task_id=ctx.task_id,
            type="LLMResponseRecorded",
            payload=LLMResponseRecordedPayload(
                call_id=call_id,
                response_ref=response_ref,
                stop_reason=resp.stop_reason,
                output_tokens=resp.usage.output,
            ),
            trace_id=ctx.trace_id,
            actor="llm",
            origin="llm",
        )

        # 4. LLMRequestFinished — price the round-trip if a pricing callback
        # was injected (① billing). cost_usd is recorded INTO the event so a
        # fold reads the captured value; a later price-table
        # change never rewrites old recordings. The error path carries an
        # empty Usage, so pricing it yields 0.0 naturally — no accumulator
        # pollution.
        cost_usd = (
            self._pricing(req.model, resp.usage)
            if self._pricing is not None
            else 0.0
        )
        self._event_log.emit(
            task_id=ctx.task_id,
            type="LLMRequestFinished",
            payload=LLMRequestFinishedPayload(
                call_id=call_id,
                success=resp.stop_reason != "error",
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                usage=resp.usage,
            ),
            trace_id=ctx.trace_id,
            actor="llm",
            origin="llm",
        )

        return resp

    def _invoke_with_retry(
        self,
        req: LLMRequest,
        ctx: StepContext,
        *,
        call_id: str,
        on_delta: Optional[Callable[[StreamDelta], None]] = None,
    ) -> LLMResponse:
        """Call the provider with LIVE-only transient backoff (README D-2d).

        Returns a normal :class:`LLMResponse` on success, or a typed error
        response (``stop_reason="error"`` + ``raw['category']``) when the
        failure is non-transient or the transient budget is exhausted.
        Intermediate transient retries write **no** request/response trio and
        emit **no** ``StepTransitionMarked`` — fold rebuilds the same state on
        resume. Each scheduled backoff DOES record an observational
        ``LLMRetryScheduled`` (a fold no-op keyed to this trio's ``call_id``)
        so the SSE stream carries "rate-limited, retrying" to the frontend
        instead of a silent multi-second stall.
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
                # Non-transient (overflow / fatal): surface immediately.
                delay = retry_policy(exc, attempt=attempt)
                if delay is None or attempt >= self._max_retries:
                    return _error_response(exc)
                attempt += 1
                self._event_log.emit(
                    task_id=ctx.task_id,
                    type="LLMRetryScheduled",
                    payload=LLMRetryScheduledPayload(
                        call_id=call_id,
                        attempt=attempt,
                        max_retries=self._max_retries,
                        delay_seconds=delay,
                        category=getattr(exc, "category", CATEGORY_FATAL),
                        error=str(exc)[:500],
                    ),
                    trace_id=ctx.trace_id,
                    actor="llm",
                    origin="llm",
                )
                self._sleep(delay)
            except Exception as exc:  # noqa: BLE001 — protocol contract
                # A provider that did not translate cleanly: bucket fatal
                # (conservative, non-retryable) to preserve the historical
                # contract that an unrecognised failure does not loop.
                return _error_response(exc)
