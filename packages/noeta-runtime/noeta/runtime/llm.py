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

import inspect
import threading
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
    AbortedError,
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

#: How often the abandonable wait re-polls the cancel predicate while a
#: provider call is in flight (and how finely the retry backoff is sliced).
#: This bounds interrupt latency on the LLM path, so it is deliberately small;
#: the poll is one lock-guarded set lookup.
_DEFAULT_ABANDON_POLL_SECONDS = 0.05

#: Message carried by the aborted-response ``raw['error']`` on every abort
#: path, so a transcript reader sees one spelling whether the round was
#: abandoned mid-wait, refused pre-attempt, or cut mid-backoff.
_ABORT_MESSAGE = "aborted: human stop while the LLM round was in flight"


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


def _streaming_accepts_abort(provider: LLMProvider) -> bool:
    """Does this provider's ``complete_streaming`` take ``should_abort``?

    The parameter is folded into the :class:`StreamingProvider` signature
    (same rationale as ``request_headers`` — no probe matrix), but a
    ``runtime_checkable`` Protocol only proves the method exists, not its
    arity. A third-party adapter on the pre-abort signature must keep working,
    so the runtime probes once and simply withholds the predicate from an
    adapter that cannot accept it — such an adapter merely forgoes fast orphan
    shutdown; the abandonable wait above it is unaffected.
    """
    if not isinstance(provider, StreamingProvider):
        return False
    try:
        sig = inspect.signature(provider.complete_streaming)
    except (TypeError, ValueError):  # builtins / C-accelerated callables
        return False
    return "should_abort" in sig.parameters


def _call_provider(
    provider: LLMProvider,
    req: LLMRequest,
    ctx: StepContext,
    provider_headers: Optional[Callable[[StepContext], Mapping[str, str]]] = None,
    on_delta: Optional[Callable[[StreamDelta], None]] = None,
    should_abort: Optional[Callable[[], bool]] = None,
) -> LLMResponse:
    # Streaming subsumes the header capability (its signature already carries
    # request_headers), so probing streaming first keeps the two optional
    # Protocols from forming a matrix of combinations.
    if on_delta is not None and isinstance(provider, StreamingProvider):
        headers = (
            dict(provider_headers(ctx)) if provider_headers is not None else None
        )
        if should_abort is not None and _streaming_accepts_abort(provider):
            return provider.complete_streaming(
                req, on_delta, headers, should_abort=should_abort
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
        abandon_poll_seconds: float = _DEFAULT_ABANDON_POLL_SECONDS,
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
        self._abandon_poll_s = abandon_poll_seconds

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
        #
        # ``muted`` closes the abandonment gap: once the step thread stops
        # waiting for an abandoned provider call, the orphan I/O thread may
        # keep streaming for a while — without the gate the user keeps seeing
        # tokens arrive for a round that is already thrown away.
        muted = threading.Event()
        on_delta: Optional[Callable[[StreamDelta], None]] = None
        if allow_stream and self._delta_sink is not None:
            sink = self._delta_sink

            def on_delta(delta: StreamDelta) -> None:
                if muted.is_set():
                    return
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
            req, ctx, call_id=call_id, on_delta=on_delta, muted=muted
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
        muted: Optional[threading.Event] = None,
    ) -> LLMResponse:
        """Call the provider, retrying transient failures with backoff.

        Returns a normal :class:`LLMResponse`, or a typed error response
        (``stop_reason="error"`` + ``raw['category']``) when the failure is
        non-transient or the budget is exhausted. Retries are live-only: an
        intermediate attempt writes no request/response trio, so a resume
        folds the same state either way. Each scheduled backoff does record an
        ``LLMRetryScheduled`` — a fold no-op, present purely so a live
        consumer sees "rate-limited, retrying" instead of a silent stall.

        When ``ctx.cancelled`` is wired (a live drive under a host cancel
        seam), every wait in here is abortable: the provider call itself runs
        on a daemon I/O thread the step thread can stop waiting for
        (:meth:`_call_abandonable` — safe because providers are contractually
        pure), the backoff sleep is sliced around the same poll, and each new
        attempt re-checks the predicate first. All abort paths resolve to the
        same ``category="aborted"`` error response; the Engine's own poll
        right after ``decide`` then abandons the whole decision, so the
        response only exists to keep the recorded trio well-formed.
        ``ctx.cancelled is None`` (resume / replay) keeps the historical
        inline, single-sleep behavior byte-for-byte.
        """
        should_abort = ctx.cancelled
        attempt = 0
        while True:
            if should_abort is not None and should_abort():
                return _error_response(AbortedError(_ABORT_MESSAGE))
            try:
                resp = self._dispatch_provider_call(
                    req, ctx,
                    on_delta=on_delta,
                    should_abort=should_abort,
                    muted=muted,
                )
                if resp is None:
                    # Abandoned mid-wait: the orphan I/O thread owns the
                    # in-flight HTTP call now; its eventual result is dropped.
                    return _error_response(AbortedError(_ABORT_MESSAGE))
                return resp
            except AbortedError as exc:
                # The adapter noticed ``should_abort`` mid-stream and closed
                # the connection itself. Never retried, never bucketed fatal.
                return _error_response(exc)
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
                if should_abort is not None and should_abort():
                    # A stop that landed during the failed attempt: don't
                    # schedule work for a round the user already discarded.
                    return _error_response(AbortedError(_ABORT_MESSAGE))
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
                if self._backoff_wait(delay, should_abort):
                    return _error_response(AbortedError(_ABORT_MESSAGE))
            except Exception as exc:  # noqa: BLE001 — protocol contract
                # A provider that did not translate its failure cleanly:
                # bucket it fatal, so an unrecognised error cannot loop.
                return _error_response(exc)

    def _dispatch_provider_call(
        self,
        req: LLMRequest,
        ctx: StepContext,
        *,
        on_delta: Optional[Callable[[StreamDelta], None]],
        should_abort: Optional[Callable[[], bool]],
        muted: Optional[threading.Event],
    ) -> Optional[LLMResponse]:
        """One provider attempt — inline, or abandonable when cancel is wired.

        ``None`` means "abandoned": the wait was given up mid-flight and no
        response will ever be observed for this attempt.
        """
        if should_abort is None:
            return _call_provider(
                self._provider, req, ctx, self._provider_headers, on_delta
            )
        return self._call_abandonable(
            req, ctx,
            on_delta=on_delta,
            should_abort=should_abort,
            muted=muted,
        )

    def _call_abandonable(
        self,
        req: LLMRequest,
        ctx: StepContext,
        *,
        on_delta: Optional[Callable[[StreamDelta], None]],
        should_abort: Callable[[], bool],
        muted: Optional[threading.Event],
    ) -> Optional[LLMResponse]:
        """Run the provider call on a daemon I/O thread; wait abandonably.

        The step thread polls ``should_abort`` between short waits and walks
        away the moment it turns truthy — this is what turns "interrupt waits
        out the whole generation" into "interrupt lands in milliseconds", and
        it works in every phase, including the pre-first-byte silence no
        chunk-loop check can cover. Abandonment is safe because
        :class:`~noeta.protocols.messages.LLMProvider` is contractually pure:
        the orphan call writes no events and holds no Task, so its eventual
        return value simply has no consumer. ``muted`` is set on abandonment
        so the orphan's remaining deltas stop reaching the sink; the adapter's
        own ``should_abort`` check (when its signature accepts one) then
        closes the connection within a chunk interval, bounding the orphan's
        token burn. A truthy poll racing a just-completed call is benign in
        both directions: the discarded response mirrors what the Engine's own
        post-``decide`` poll would have done with it.
        """
        outcome: list[tuple[str, Any]] = []
        done = threading.Event()

        def _run() -> None:
            try:
                outcome.append(
                    (
                        "ok",
                        _call_provider(
                            self._provider,
                            req,
                            ctx,
                            self._provider_headers,
                            on_delta,
                            should_abort,
                        ),
                    )
                )
            except BaseException as exc:  # noqa: BLE001 — re-raised on the step thread
                outcome.append(("err", exc))
            finally:
                done.set()

        worker = threading.Thread(
            target=_run, name="noeta-llm-io", daemon=True
        )
        worker.start()
        while not done.wait(self._abandon_poll_s):
            if should_abort():
                if muted is not None:
                    muted.set()
                return None
        kind, value = outcome[0]
        if kind == "err":
            raise value  # translated by _invoke_with_retry's handlers
        response: LLMResponse = value
        return response

    def _backoff_wait(
        self, delay: float, should_abort: Optional[Callable[[], bool]]
    ) -> bool:
        """Wait out one retry backoff; ``True`` ⇒ aborted before it elapsed.

        With no cancel seam this is the historical single ``sleep(delay)``
        (and the injected test sleep sees exactly one call, as before). With
        one, the delay is sliced so a stop pressed mid-backoff is honored
        within one poll interval instead of after the full ~30 s cap.
        """
        if should_abort is None:
            self._sleep(delay)
            return False
        remaining = delay
        while remaining > 0:
            if should_abort():
                return True
            step = min(self._abandon_poll_s, remaining)
            self._sleep(step)
            remaining -= step
        return should_abort()
