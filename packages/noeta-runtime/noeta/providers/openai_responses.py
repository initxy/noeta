"""OpenAI Responses-API adapter for the Noeta-shape LLM protocol.

Implements :class:`noeta.protocols.messages.LLMProvider` against endpoints
speaking the **OpenAI Responses API** (not Chat Completions) — typically an
Azure-flavored gateway transport (``api-key`` header,
``?api-version`` query). Named by protocol, not vendor: this is the
*Responses-compatible* adapter, parallel to the Chat-compatible one
(``openai_compat.py``), purely additive
(D1).

The translation contract is pinned: every
fidelity loss from the Responses wire shape is sealed inside this one file;
Engine / Policy only ever see Noeta-shape types. The two protocol
shapes differ too much (``messages`` vs ``input``, ``choices`` vs
``output[]``, ``tool_calls`` vs ``function_call`` items, all usage field names
differ), so this is **written from scratch** — it does not reuse the Chat
translation, nor import any private helper from ``openai_compat`` (this file
stays self-contained, D2).

Transport is all constructor parameters (azure is not hard-coded — azure is
not a protocol, just a gateway hosting this one): ``base_url`` / ``api_key`` /
``api_version`` / ``timeout_seconds`` (**default 300s**; high-effort reasoning
routinely takes 1-2 minutes+, probed at ~80s) / ``extra_headers`` (carries
``X-TT-LOGID``) / ``image_resolver`` (the ledger stores a small
``ImageBlock(ContentRef)`` handle; this narrow Callable deref→base64-inlines
only at wire-assembly time, **never** writing back to the ledger) /
``reasoning_continuation`` (used by 04, symmetric placeholder to
``openai_compat``). Wire details (re-probed against a real gateway
2026-06-12): ``base_url`` is the **complete responses endpoint** (e.g.
``https://<gateway-host>/responses``); the provider
**POSTs directly to that URL**, adding only a ``?api-version=<ver>`` query, and
**no longer appends an ``/openai/responses`` path** (appending the path failed
in testing; the verbatim POST returned 200). Auth via the ``api-key: <key>``
header, **model in the body** (confirmed by probing, not in the URL path),
always with ``store:false`` (red line).

The tracer bullet did text-in, text-out. Tool round-trips followed: outbound
``ToolUseBlock``→top-level ``function_call`` item, ``ToolResultBlock``→
``function_call_output`` item, ``tools`` array un-nested from the Chat nested
shape into the Responses flat shape; inbound ``output[]`` ``function_call``
items→``ToolUseBlock`` (**paired by ``call_id``**, not the internal ``id``).
Reasoning and images came in later. ``stop_reason`` was written fully from the
start (``tool_use`` priority per implementation).

Token streaming came last: :meth:`OpenAIResponsesProvider.complete_streaming`
POSTs the same body plus ``stream:true`` and feeds the terminal
``response.completed`` payload through the same ``_parse_response``, so the
streamed and batch results are shape-identical (token-streaming ADR).
"""

from __future__ import annotations

import base64
import json
from typing import Any, Callable, Literal, Optional

import httpx

from noeta.protocols.errors import (
    ContextOverflowError,
    FatalError,
    TransientError,
)
from noeta.protocols.messages import (
    Block,
    ImageBlock,
    LLMRequest,
    LLMResponse,
    Message,
    StreamDelta,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.values import ContentRef
from noeta.providers import catalog
from noeta.providers._sse import iter_sse_events
from noeta.providers.codecs import (
    decode_tool_arguments,
    encode_tool_arguments,
    parse_retry_after,
)


#: Outbound reasoning-chain echo policy (symmetric to ``openai_compat``; only
#: wired up by 04). In Responses, echoing ``encrypted_content`` is **required**
#: for continuation, so this defaults to ``"responses"`` (unlike Chat, which
#: defaults to ``"off"`` — native OpenAI rejects the echo).
ReasoningContinuation = Literal["off", "chat", "responses"]


class OpenAIResponsesProvider:
    """Adapter for an OpenAI Responses-API endpoint.

    Construct once (endpoint base URL + credentials) and reuse across calls —
    the underlying :class:`httpx.Client` is shared, and ``LLMRequest.model``
    picks the model per call. ``extra_headers`` is the escape hatch for
    gateway-specific headers (e.g. ``X-TT-LOGID``).

    The provider stays clean (red line):
    ``image_resolver`` is a narrowly injected constructor parameter (same
    nature as the httpx client it already holds); it does **not** hold a
    ContentStore / StepContext, and its only protocol methods are
    :meth:`complete` (plus the optional-capability variants
    :meth:`complete_with_headers` / :meth:`complete_streaming`).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        api_version: Optional[str] = None,
        default_max_tokens: Optional[int] = None,
        timeout_seconds: float = 300.0,
        extra_headers: Optional[dict[str, str]] = None,
        image_resolver: Optional[Callable[[ContentRef], bytes]] = None,
        reasoning_continuation: ReasoningContinuation = "responses",
    ) -> None:
        # base_url is the **complete responses endpoint**
        # (D1, re-probed
        # 2026-06-12): POST directly to that URL, adding only the ?api-version
        # query, and **do not append** the /openai/responses path. So the
        # httpx.Client sets no base_url (which would re-join the endpoint as a
        # relative path); the whole endpoint string is stored and POSTed
        # verbatim in complete().
        self._endpoint = base_url.rstrip("/")
        self._api_version = api_version
        self._default_max_tokens = default_max_tokens
        self._image_resolver = image_resolver
        self._reasoning_continuation = reasoning_continuation
        headers: dict[str, str] = {
            "api-key": api_key,
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        self._client = httpx.Client(
            headers=headers,
            timeout=timeout_seconds,
        )

    # ------------------------------------------------------------------
    # LLMProvider Protocol
    # ------------------------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        return self.complete_with_headers(request, None)

    def complete_with_headers(
        self,
        request: LLMRequest,
        request_headers: Optional[dict[str, str]],
    ) -> LLMResponse:
        # Vision guard (D6):
        # request carries an ImageBlock but the target model is not vision-
        # capable → FatalError before going on the wire. **Don't blindly send
        # an image to a model that can't read it** (the gateway rejects with a
        # cryptic 4xx, or worse, silently ignores the image). Placed before
        # wire assembly: the guard runs ahead of all outbound assembly.
        _guard_vision_capability(request)
        body = self._build_request_body(request)
        # Error recovery: every wire-shape
        # failure is translated here into the neutral Noeta error taxonomy; the
        # runtime never sees httpx types. Connection/timeout is transient
        # (worth retrying); HTTP status errors are bucketed by
        # ``_translate_http_error``.
        params = (
            {"api-version": self._api_version}
            if self._api_version is not None
            else None
        )

        def _post(json_body: dict[str, Any]) -> httpx.Response:
            # POST verbatim to the complete endpoint (base_url is the endpoint
            # itself; **do not** append a path).
            kwargs: dict[str, Any] = {"params": params, "json": json_body}
            if request_headers is not None:
                kwargs["headers"] = request_headers
            return self._client.post(self._endpoint, **kwargs)

        try:
            http_response = _post(body)
            # Stale cross-turn reasoning recovery: the echoed ``reasoning`` input
            # items carry ``encrypted_content`` (a prior turn's ThinkingBlock
            # signature). That ciphertext is only verifiable inside the
            # continuation window the gateway minted it for — across a long
            # human-suspend gap (key rotation / TTL) the gateway rejects it with
            # 400 ``invalid_encrypted_content``. The ciphertext is only needed
            # for in-flight continuation, never to make a fresh turn valid, so
            # drop every echoed reasoning item and retry ONCE (request-level
            # ``reasoning``/``include`` stay, so this turn still reasons fresh).
            if (
                http_response.status_code == 400
                and _is_invalid_encrypted_content(http_response)
                and _has_reasoning_input(body)
            ):
                http_response = _post(_strip_reasoning_input(body))
            http_response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _translate_http_error(exc) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(str(exc)) from exc
        try:
            payload = http_response.json()
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Responses payload was not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(
                "Responses payload root was not a JSON object: "
                f"type={type(payload).__name__}"
            )
        return self._parse_response(payload)

    # ------------------------------------------------------------------
    # StreamingProvider Protocol
    # ------------------------------------------------------------------

    def complete_streaming(
        self,
        request: LLMRequest,
        on_delta: Callable[[StreamDelta], None],
        request_headers: Optional[dict[str, str]] = None,
    ) -> LLMResponse:
        """Streamed variant of :meth:`complete_with_headers`.

        Same wire body as the batch path plus ``stream:true``, same endpoint /
        ``api-version`` query / merged ``request_headers``. Text and
        reasoning-summary fragments fire ``on_delta`` while the response is in
        flight; the terminal ``response.completed`` event carries the complete
        response object, which is fed through the **same** ``_parse_response``
        as the batch path — so the returned :class:`LLMResponse` is
        shape-identical whether or not anyone streamed (token-streaming ADR).

        Errors keep the batch taxonomy: an HTTP error status on stream open is
        read and translated by ``_translate_http_error``; a transport/timeout
        failure mid-stream is a :class:`TransientError` (the runtime retry
        loop reissues the whole call); the stale-ciphertext 400
        (``invalid_encrypted_content``) self-heals with the same one-shot
        strip-and-retry as the batch path — that 400 always surfaces before
        any stream starts, and the retried request streams as well.
        """
        _guard_vision_capability(request)
        body = self._build_request_body(request)
        body["stream"] = True
        params = (
            {"api-version": self._api_version}
            if self._api_version is not None
            else None
        )

        def _stream(json_body: dict[str, Any]) -> LLMResponse:
            kwargs: dict[str, Any] = {"params": params, "json": json_body}
            if request_headers is not None:
                kwargs["headers"] = request_headers
            with self._client.stream(
                "POST", self._endpoint, **kwargs
            ) as http_response:
                if http_response.status_code >= 400:
                    # Load the error body while the stream is still open so
                    # the taxonomy helpers (_is_context_overflow /
                    # _is_invalid_encrypted_content) can read the JSON after
                    # the context closes.
                    http_response.read()
                http_response.raise_for_status()
                return self._consume_stream(http_response, on_delta)

        try:
            try:
                return _stream(body)
            except httpx.HTTPStatusError as exc:
                # Stale cross-turn reasoning ciphertext self-heals exactly as
                # in complete_with_headers: drop the echoed reasoning input
                # items and retry ONCE (see the batch path for the full
                # rationale). Any error the retried stream raises falls
                # through to the outer translation below.
                if (
                    exc.response.status_code == 400
                    and _is_invalid_encrypted_content(exc.response)
                    and _has_reasoning_input(body)
                ):
                    return _stream(_strip_reasoning_input(body))
                raise
        except httpx.HTTPStatusError as exc:
            raise _translate_http_error(exc) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # Covers both a failed stream open and a mid-stream disconnect:
            # transient, so the runtime retry loop reissues the whole call.
            raise TransientError(str(exc)) from exc

    def _consume_stream(
        self,
        http_response: httpx.Response,
        on_delta: Callable[[StreamDelta], None],
    ) -> LLMResponse:
        """Drain one Responses SSE stream: emit deltas, parse the terminal
        response object with the batch parser.

        Event handling (the SSE ``event:`` name is authoritative; the JSON
        ``type`` field is the fallback for nameless frames):

        * ``response.output_text.delta`` → ``StreamDelta(kind="text")``;
          ``response.reasoning_summary_text.delta`` →
          ``StreamDelta(kind="thinking")``. ``index`` is the event's
          ``output_index`` (the item's position in ``output[]``, monotonic
          across the response).
        * ``response.function_call_arguments.delta`` → swallowed: tool-call
          arguments are never surfaced as deltas (partial JSON is
          undecodable), and no client-side accumulator is needed — the
          terminal response object carries every ``function_call`` item with
          its complete ``arguments`` string for ``_parse_response``.
        * ``response.completed`` / ``response.incomplete`` / a
          ``response.failed`` without an error payload → the carried
          ``response`` object goes through :meth:`_parse_response`, keeping
          the batch inferences (``incomplete`` + ``max_output_tokens`` →
          ``max_tokens``; ``failed`` → ``error``).
        * A ``response.failed`` **with** an error payload, and top-level
          ``error`` events → raised through :func:`_translate_stream_error`
          (same buckets as ``_translate_http_error``).
        * Everything else (``response.created`` / ``*.added`` / ``*.done`` /
          unknown types / non-JSON data frames) is skipped silently —
          mirroring the batch parser's unknown-item stance; vendor stream
          vocabularies drift.

        A stream that ends without a terminal response event is a truncated
        stream → :class:`TransientError` (retryable, like any mid-stream
        disconnect).
        """
        final_payload: Optional[dict[str, Any]] = None
        for event_name, data in iter_sse_events(http_response.iter_lines()):
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            event_type = event_name or payload.get("type")
            if event_type == "response.output_text.delta":
                fragment = payload.get("delta")
                if isinstance(fragment, str) and fragment:
                    on_delta(
                        StreamDelta(
                            kind="text",
                            text=fragment,
                            index=_output_index(payload),
                        )
                    )
            elif event_type == "response.reasoning_summary_text.delta":
                fragment = payload.get("delta")
                if isinstance(fragment, str) and fragment:
                    on_delta(
                        StreamDelta(
                            kind="thinking",
                            text=fragment,
                            index=_output_index(payload),
                        )
                    )
            elif event_type in (
                "response.completed",
                "response.failed",
                "response.incomplete",
            ):
                response_obj = payload.get("response")
                if event_type == "response.failed":
                    error = (
                        response_obj.get("error")
                        if isinstance(response_obj, dict)
                        else None
                    )
                    if isinstance(error, dict):
                        raise _translate_stream_error(error)
                if isinstance(response_obj, dict):
                    final_payload = response_obj
            elif event_type == "error":
                raise _translate_stream_error(payload)
            # Every other event type: skipped silently.
        if final_payload is None:
            raise TransientError(
                "Responses stream ended without a terminal response event "
                "(response.completed / response.failed / response.incomplete)"
            )
        return self._parse_response(final_payload)

    # ------------------------------------------------------------------
    # Outbound translation (Noeta → Responses)
    # ------------------------------------------------------------------

    def _build_request_body(self, request: LLMRequest) -> dict[str, Any]:
        input_items: list[dict[str, Any]] = []
        # Resolve the bound model's vision capability **once** (the whole
        # request targets one model). Only the tool-result image branch reads
        # it: a vision model inlines tool-surfaced images into
        # ``function_call_output``, a non-vision model degrades to text — and a
        # tool-result image is invisible to ``_guard_vision_capability`` (which
        # only scans top-level ImageBlocks), so this flag is its dedicated gate.
        model_supports_vision = _model_supports_vision(request.model)
        for message in request.messages:
            if message.role == "system":
                raise ValueError(
                    "system must use LLMRequest.system field, not messages array"
                )
            # One Message may expand into several input items: assistant text
            # is one ``message`` item, but each ToolUseBlock it carries is a
            # **separate top-level ``function_call`` item** in Responses (not
            # nested inside message); likewise each ToolResultBlock on a tool-
            # role message is a separate ``function_call_output``.
            input_items.extend(
                _message_to_responses(
                    message,
                    self._reasoning_continuation,
                    self._image_resolver,
                    model_supports_vision,
                )
            )

        body: dict[str, Any] = {
            "model": request.model,
            "input": input_items,
            # red line:
            # Responses requests always carry store:false (no state left on the
            # gateway, so each request is self-contained and a resumed run does
            # not depend on gateway-side conversation state).
            "store": False,
        }
        # system → top-level instructions (flatten its text).
        if request.system is not None:
            body["instructions"] = _flatten_text_blocks(request.system)
        if request.tools:
            # Un-nest the tools shape: Chat hides the args in a ``function:{…}``
            # sub-object; Responses lays it flat
            # (``{type:function,name,description,parameters}``).
            body["tools"] = [_tool_to_responses(tool) for tool in request.tools]
        # LLMRequest has no tool_choice field — pass it through only when
        # explicitly given in metadata; **don't** invent one (absent means
        # omitted, so the gateway uses its default auto).
        tool_choice = request.metadata.get("tool_choice")
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if request.temperature is not None:
            body["temperature"] = request.temperature
        # Responses uses max_output_tokens (not Chat's max_tokens). Prefer the
        # request's own value; else fall back to the constructor-time
        # ``default_max_tokens`` (host config's ``max_tokens``). Omitted only
        # when both are absent — letting the gateway use its own default (which
        # may be small and easily truncated).
        effective_max_tokens = (
            request.max_tokens
            if request.max_tokens is not None
            else self._default_max_tokens
        )
        if effective_max_tokens is not None:
            body["max_output_tokens"] = effective_max_tokens
        # output_schema → text.format (Responses' structured-output knob; shape
        # differs from Chat's response_format.json_schema,
        # D2).
        if request.output_schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "noeta_output",
                    "schema": dict(request.output_schema),
                }
            }
        # Reasoning chain (D3): derive a mapped effort from effort/thinking.
        # Non-None means "an explicit effort was requested" — attach
        # reasoning{effort,summary:auto}. summary:"auto" makes the gateway emit
        # readable summary segments; store:false is already always set above.
        effort = _map_effort(request)
        if effort is not None:
            body["reasoning"] = {"effort": effort, "summary": "auto"}
        # include:[reasoning.encrypted_content] is DECOUPLED from the effort
        # gate: a reasoning model reasons at its server-side default effort even
        # when the request carries no reasoning{} block, and without the
        # ciphertext the next turn can only echo an empty reasoning item — the
        # gateway then cannot restore the prior turn's reasoning tokens, which
        # breaks both continuation and the prompt-cache prefix at the first
        # assistant turn (observed: subagents spawned without an effort override
        # never got a cache hit past the static head). ``reasoning_continuation
        # = "off"`` remains the escape hatch for gateways that reject the
        # include param — with the echo off the ciphertext is never sent back,
        # so requesting it would be pointless.
        if self._reasoning_continuation != "off":
            body["include"] = ["reasoning.encrypted_content"]
        return body

    # ------------------------------------------------------------------
    # Inbound translation (Responses → Noeta)
    # ------------------------------------------------------------------

    def _parse_response(self, payload: dict[str, Any]) -> LLMResponse:
        output = payload.get("output")
        if not isinstance(output, list):
            raise ValueError(
                "Responses payload missing 'output' array or it was not a list"
            )

        content: list[Block] = []
        has_function_call = False
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "message":
                content.extend(_message_item_to_blocks(item))
            elif item_type == "function_call":
                has_function_call = True
                content.append(_function_call_item_to_block(item))
            elif item_type == "reasoning":
                content.append(_reasoning_item_to_block(item))
            # Other item types are skipped.

        stop_reason = _infer_stop_reason(payload, has_function_call)
        usage = _translate_usage(payload.get("usage") or {})
        return LLMResponse(
            stop_reason=stop_reason,
            content=content,
            usage=usage,
            raw=payload,
        )


# ---------------------------------------------------------------------------
# stop_reason inference (no finish_reason; by priority, D2)
# ---------------------------------------------------------------------------


def _infer_stop_reason(
    payload: dict[str, Any], has_function_call: bool
) -> Literal["tool_use", "end_turn", "max_tokens", "error"]:
    """Responses has no usable ``finish_reason``; infer the stop signal by
    priority:

    1. ``incomplete`` + ``incomplete_details.reason == "max_output_tokens"``
       → ``max_tokens`` (**overrides** a truncated function_call, consistent
       with Chat's ``length`` precedent).
    2. A ``function_call`` item present → ``tool_use``.
    3. ``status == "completed"`` → ``end_turn``.
    4. Otherwise (``failed`` / ``content_filter`` etc.) → ``error``.
    """
    status = payload.get("status")
    if status == "incomplete":
        details = payload.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        if reason == "max_output_tokens":
            return "max_tokens"
    if has_function_call:
        return "tool_use"
    if status == "completed":
        return "end_turn"
    return "error"


# ---------------------------------------------------------------------------
# effort / thinking mapping (D2/D3)
# ---------------------------------------------------------------------------


#: Effort mapping table: ``low/medium/high`` pass through; ``xhigh/max``
#: collapse to ``high`` (the
#: gateway has no finer bucket).
_EFFORT_MAP: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


def _map_effort(request: LLMRequest) -> Optional[str]:
    """Derive Responses' effort from ``request.effort`` and ``request.thinking``.

    * ``effort``: ``low/medium/high`` pass through, ``xhigh/max→high``,
      ``None`` unset.
    * ``thinking``: ``"disabled"`` signals **explicitly suppressed reasoning**
      → force ``effort="minimal"`` (overrides any explicit effort);
      ``"adaptive"`` / ``None`` do **not** derive effort from thinking (let the
      effort field speak for itself).

    A non-None return means "reasoning present" — the caller then attaches the
    ``reasoning`` block and ``include``. If neither yields anything → return
    ``None`` (no reasoning parameter).
    """
    if request.thinking == "disabled":
        return "minimal"
    if request.effort is None:
        return None
    return _EFFORT_MAP.get(request.effort, request.effort)


# ---------------------------------------------------------------------------
# Error translation (② error recovery, provider-neutral)
# ---------------------------------------------------------------------------


def _translate_http_error(exc: httpx.HTTPStatusError) -> Exception:
    """Map a Responses-wire HTTP status error to the neutral taxonomy.

    * 429 → :class:`TransientError` (reads ``Retry-After``).
    * 5xx → :class:`TransientError`.
    * 400 with ``error.code/type == 'context_length_exceeded'`` (or a message
      mentioning maximum context length) → :class:`ContextOverflowError`.
    * Other 4xx (400 / 401 / 403 / ...) → :class:`FatalError`.
    """
    response = exc.response
    status = response.status_code
    if status == 429:
        return TransientError(
            str(exc),
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
        )
    if status >= 500:
        return TransientError(str(exc))
    if status == 400 and _is_context_overflow(response):
        return ContextOverflowError(str(exc))
    return FatalError(str(exc))


def _is_context_overflow(response: httpx.Response) -> bool:
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return False
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return False
    return _error_indicates_context_overflow(error)


def _error_indicates_context_overflow(error: dict[str, Any]) -> bool:
    """The dict-level half of the context-overflow match, shared between the
    HTTP-status path (``_is_context_overflow``) and the in-stream error path
    (``_translate_stream_error``) so both classify identically."""
    code = str(error.get("code") or "")
    err_type = str(error.get("type") or "")
    message = str(error.get("message") or "").lower()
    return (
        code == "context_length_exceeded"
        or err_type == "context_length_exceeded"
        or "maximum context length" in message
    )


#: In-stream error codes bucketed :class:`TransientError` — the same failure
#: classes ``_translate_http_error`` buckets transient by HTTP status
#: (429 rate limiting / 5xx server side), which an in-flight stream reports
#: as an error payload instead of a status code.
_TRANSIENT_STREAM_ERROR_CODES = frozenset(
    {"rate_limit_exceeded", "server_error", "service_unavailable"}
)


def _translate_stream_error(error: dict[str, Any]) -> Exception:
    """Map an in-stream error payload to the neutral taxonomy.

    Covers both a top-level SSE ``error`` event and a ``response.failed``
    object's ``error``. Mirrors ``_translate_http_error``'s classification,
    keyed off the error code/message instead of an HTTP status (mid-stream
    there is none): rate limits and server-side failures → Transient;
    context overflow keeps its dedicated bucket; everything else → Fatal.
    """
    code = str(error.get("code") or "")
    message = str(error.get("message") or "")
    detail = f"Responses stream error: code={code or 'unknown'}; {message}"
    if _error_indicates_context_overflow(error):
        return ContextOverflowError(detail)
    if code in _TRANSIENT_STREAM_ERROR_CODES:
        return TransientError(detail)
    return FatalError(detail)


def _output_index(payload: dict[str, Any]) -> int:
    """A delta event's ``output_index`` → ``StreamDelta.index``.

    ``output_index`` is the emitting item's position in the response's
    ``output[]`` (monotonic across items), which is exactly the block index a
    delta consumer keys interleaved text/thinking on. A missing / malformed
    value degrades to 0 rather than dropping the delta."""
    value = payload.get("output_index")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _is_invalid_encrypted_content(response: httpx.Response) -> bool:
    """A 400 whose error is the gateway rejecting echoed reasoning ciphertext.

    The Responses gateway returns ``code: invalid_encrypted_content`` (seen as
    the gateway code ``-4003`` / message ``"encrypted content ... could not be
    verified"``) when a ``reasoning`` input item carries an ``encrypted_content``
    it can no longer decrypt — a prior turn's ciphertext replayed past its
    continuation window. Matched on the message so a gateway that uses its own
    numeric code still trips it.
    """
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return False
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return False
    code = str(error.get("code") or "")
    message = str(error.get("message") or "").lower()
    return (
        code == "invalid_encrypted_content"
        or "invalid_encrypted_content" in message
        or ("encrypted content" in message and "could not be" in message)
    )


def _has_reasoning_input(body: dict[str, Any]) -> bool:
    """Whether the wire body echoes any prior-turn ``reasoning`` input item."""
    return any(
        isinstance(it, dict) and it.get("type") == "reasoning"
        for it in body.get("input", [])
    )


def _strip_reasoning_input(body: dict[str, Any]) -> dict[str, Any]:
    """A shallow copy of ``body`` with all echoed ``reasoning`` input items
    dropped — the retry shape after the gateway rejects stale ciphertext. The
    request-level ``reasoning``/``include`` knobs are untouched, so the turn
    still reasons (and returns fresh ciphertext); only the un-verifiable
    replayed items are removed."""
    retry = dict(body)
    retry["input"] = [
        it
        for it in body.get("input", [])
        if not (isinstance(it, dict) and it.get("type") == "reasoning")
    ]
    return retry


# ---------------------------------------------------------------------------
# Vision-capability guard (D6)
# ---------------------------------------------------------------------------


def _guard_vision_capability(request: LLMRequest) -> None:
    """Request contains an ``ImageBlock`` but the target model is not vision-
    capable → :class:`FatalError` before going on the wire.

    The safety net once ImageBlock entered the union type: don't send an image
    to a model that can't read it. Look up ``request.model`` in
    ``catalog.CATALOG`` (after ``resolve_alias`` translates a friendly alias to
    the real id); treat these as **not vision-capable** and error out on any
    image:

      * model not in the catalog (unregistered → can't tell if it sees images,
        conservatively no);
      * model in the catalog but ``supports_vision`` is False.

    Requests with no ``ImageBlock`` (pure text/tools) pass straight through —
    the guard hits the catalog only when an image is actually present, so the
    text-only path has zero overhead and zero behavior change (red line: leave
    the old path untouched).
    """
    if not _request_has_image(request):
        return
    real_id = catalog.resolve_alias(request.model)
    spec = catalog.CATALOG.get(real_id)
    if spec is not None and spec.supports_vision:
        return
    raise FatalError(
        f"request carries an ImageBlock but model {request.model!r} is not "
        "vision-capable (catalog supports_vision is False or model is "
        "unregistered); refusing to send the image to a model that cannot "
        "read it."
    )


def _request_has_image(request: LLMRequest) -> bool:
    """True if any ``Message`` in the request contains an ``ImageBlock`` (scans
    every position, not just the last turn)."""
    return any(
        isinstance(block, ImageBlock)
        for message in request.messages
        for block in message.content
    )


def _model_supports_vision(model: str) -> bool:
    """Whether ``model`` is registered as vision-capable in the catalog.

    Same lookup as the vision guard (``resolve_alias`` → ``CATALOG`` →
    ``supports_vision``), so one place can't pass an image while another
    rejects it: an unregistered model, or a registered one with
    ``supports_vision`` False, is **not** vision-capable. This gates the tool-
    result image branch (a tool-surfaced image rides ``ToolResultBlock.images``,
    not a top-level ImageBlock, so ``_guard_vision_capability`` never sees it —
    this flag is its dedicated check).
    """
    real_id = catalog.resolve_alias(model)
    spec = catalog.CATALOG.get(real_id)
    return spec is not None and spec.supports_vision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flatten_text_blocks(message: Message) -> str:
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
    return "\n".join(parts)


ImageResolver = Callable[[ContentRef], bytes]


def _message_to_responses(
    message: Message,
    reasoning_continuation: ReasoningContinuation,
    image_resolver: Optional[ImageResolver],
    model_supports_vision: bool = False,
) -> list[dict[str, Any]]:
    """One Noeta Message → a sequence of Responses ``input`` items.

    Returns a **list**, not a single item: Responses treats tool calls/results
    as top-level items peer to ``message``, so an assistant / tool message
    carrying tools expands into several items.

    * user → one ``message`` item; text goes via ``input_text``, ``ImageBlock``
      via an ``input_image`` data URI (see ``_content_segments``).
      (``origin`` system/memory
      are host-injected turns; Responses natively supports a mid-history
      ``system``-role input item, so it is rendered as a ``role:"system"``
      input item here — equivalent to openai_compat raising a system role and
      anthropic wrapping ``<system-reminder>``; ``human`` / ``None`` are the
      user's own words, rendered as ``role:"user"``.)
    * assistant → reasoning echo (if enabled) as ``reasoning`` items,
      text/images (if any) as one ``message`` item (text via ``output_text``,
      images still via ``input_image``); each ``ToolUseBlock`` becomes a
      separate ``{type:function_call,call_id,name,arguments}`` item (arguments
      is a JSON **string**).
    * tool → each ``ToolResultBlock`` becomes a
      ``{type:function_call_output,call_id,output}`` item.

    ``image_resolver`` is a narrowly injected ``ContentRef→bytes``
    (D4, backed by
    ``content_store.get``): when a message contains an ``ImageBlock`` it
    deref→base64-inlines here; if it is None but an ``ImageBlock`` is
    encountered, **error explicitly** (missing config must be loud, not a
    silently dropped image).
    """
    if message.role == "user":
        # D4: host-injected turns (``origin`` system / memory) ride the
        # user channel in the ledger but render as a mid-history ``system`` role
        # input item — Responses supports that natively, so no tag syntax leaks
        # into the wire. ``human`` / ``None`` mean the human's own words → user.
        role = "system" if message.origin in ("system", "memory") else "user"
        return [
            {
                "type": "message",
                "role": role,
                "content": _content_segments(message, "input_text", image_resolver),
            }
        ]
    if message.role == "assistant":
        return _assistant_message_to_responses(
            message, reasoning_continuation, image_resolver
        )
    if message.role == "tool":
        return _tool_message_to_responses(
            message, image_resolver, model_supports_vision
        )
    raise ValueError(f"unsupported message role: {message.role!r}")


def _content_segments(
    message: Message,
    text_segment_type: Literal["input_text", "output_text"],
    image_resolver: Optional[ImageResolver],
) -> list[dict[str, Any]]:
    """A message's content blocks → a sequence of Responses ``content[]``
    segments (the inline primitive).

    This is the **shared** push/pull inline primitive
    (D4) — it scans
    ``ImageBlock`` at **any** message position (not bound to the last-turn
    user), paving the way at zero cost for future pull (an image-reading tool
    returning an image via the assistant/tool path).

    * ``TextBlock`` → ``{type:<text_segment_type>, text}``.
    * ``ImageBlock`` → ``{type:"input_image", image_url:"data:<media>;base64,<…>"}``:
      bytes are deref'd via ``image_resolver(block.source)`` and base64-encoded;
      ``media_type`` comes from ``block.source.media_type``. **Red line**: the
      base64 appears only in the outgoing wire body, is transient, and is never
      written back to the ledger/ContentStore.

    **The text-only path stays byte-for-byte unchanged**: with no
    ``ImageBlock``, all text blocks flatten into a **single** text segment
    (byte-identical to the earlier serialization, keeping golden re-pins at
    zero); only when an image actually appears does it fall back to per-block
    multi-segment (each text its own segment, images as ``input_image``
    segments), in original block order.

    Contains an ``ImageBlock`` but ``image_resolver`` is None → ``ValueError``
    (missing config must be loud).
    """
    has_image = any(isinstance(b, ImageBlock) for b in message.content)
    if not has_image:
        # No image: flatten into a single text segment, byte-identical to the
        # historical serialization.
        return [{"type": text_segment_type, "text": _flatten_text_blocks(message)}]
    segments: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            segments.append({"type": text_segment_type, "text": block.text})
        elif isinstance(block, ImageBlock):
            segments.append(_image_block_to_input_image(block, image_resolver))
        # Other blocks (ThinkingBlock/ToolUseBlock etc.) do not enter the
        # content array — they become top-level items on the assistant path;
        # this handles only message content segments.
    return segments


def _image_block_to_input_image(
    block: ImageBlock, image_resolver: Optional[ImageResolver]
) -> dict[str, Any]:
    """``ImageBlock(ContentRef)`` → a Responses ``input_image`` data URI segment.

    deref+base64 happens only at wire-assembly time, is transient, and is never
    written back to the ledger/ContentStore
    (red line). A missing
    ``image_resolver`` (None) means incomplete config → error explicitly.
    """
    if image_resolver is None:
        raise ValueError(
            "request carries an ImageBlock but provider has no image_resolver "
            "configured; cannot deref image bytes (set image_resolver to "
            "content_store.get). Refusing to silently drop the image."
        )
    raw = image_resolver(block.source)
    b64 = base64.b64encode(raw).decode("ascii")
    media_type = block.source.media_type
    return {
        "type": "input_image",
        "image_url": f"data:{media_type};base64,{b64}",
    }


def _assistant_message_to_responses(
    message: Message,
    reasoning_continuation: ReasoningContinuation,
    image_resolver: Optional[ImageResolver],
) -> list[dict[str, Any]]:
    """assistant text → one ``message`` item; each ToolUseBlock → a separate
    top-level ``function_call`` item (arguments serialized to a JSON string).

    Reasoning-chain echo
    (D3, **on by default** — unlike Chat where the echo is rejected by native
    OpenAI; in Responses, echoing ``encrypted_content`` is **required** for
    continuation): each ``ThinkingBlock`` → one
    ``{type:reasoning,encrypted_content,summary}`` item, gated by
    ``reasoning_continuation != "off"``. This connects to the reasoning-chain
    re-attach done upstream — the
    composer re-attaches the ThinkingBlock before that turn's tool_use in the
    neutral View, and the provider just serializes in appearance order; so the
    reasoning item naturally sorts **before** that turn's function_call (in the
    implementation, reasoning items are collected and extended first, then
    tool_calls).

    The text-only path (no tools, no echoed reasoning) stays byte-for-byte
    consistent: a single ``output_text`` message item.
    """
    items: list[dict[str, Any]] = []
    reasoning_items: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    echo_reasoning = reasoning_continuation != "off"
    for block in message.content:
        if isinstance(block, ThinkingBlock):
            # The echo is gated: the composer re-attaches the reasoning chain
            # neutrally for every provider, but only a gateway that echoes
            # (Responses relies on it for continuation) actually puts it on the
            # wire. Dropped when off. Also dropped when the block carries no
            # ciphertext (signature None — the prior turn was made without
            # include:[reasoning.encrypted_content]): an item without
            # encrypted_content cannot restore any reasoning tokens, and
            # echoing the empty shell breaks the gateway's prompt-cache prefix
            # at this position.
            if echo_reasoning and block.signature is not None:
                reasoning_items.append(_thinking_block_to_reasoning(block))
        elif isinstance(block, ToolUseBlock):
            tool_calls.append(
                {
                    "type": "function_call",
                    "call_id": block.call_id,
                    "name": block.tool_name,
                    "arguments": encode_tool_arguments(block.arguments),
                }
            )
        # TextBlock / ImageBlock go into message content (see
        # _content_segments); other blocks are skipped here.
    # Order: reasoning items (continuation ciphertext) → message text/image
    # item → function_call items. reasoning before function_call is the
    # required layout for continuation (matching the re-attach order). Message
    # content uses the shared inline primitive: a single output_text when no
    # image (bytes unchanged), or text as output_text segments and images as
    # input_image segments when there is one
    # (D4).
    items.extend(reasoning_items)
    # A pure tool-call turn (no text, no image — the ReAct norm) sends **no**
    # message item: emitting an empty output_text segment is a suspect shape
    # not validated against the real gateway, and contradicts the Chat-
    # compatible adapter (openai_compat sets content to None when there is no
    # text). Send a message item only when text/images are actually present;
    # reasoning / function_call items lay out as usual (still reasoning →
    # message → function_call). The text-only path is unaffected, bytes
    # unchanged.
    if _flatten_text_blocks(message) or any(
        isinstance(b, ImageBlock) for b in message.content
    ):
        items.append(
            {
                "type": "message",
                "role": "assistant",
                "content": _content_segments(
                    message, "output_text", image_resolver
                ),
            }
        )
    items.extend(tool_calls)
    return items


def _thinking_block_to_reasoning(block: ThinkingBlock) -> dict[str, Any]:
    """Outbound ``ThinkingBlock`` → a Responses ``reasoning`` item
    (D3).

    ``encrypted_content`` is ``block.signature`` **stuffed back verbatim** (the
    continuation ciphertext is byte-exact and not one byte may change) — the
    key is omitted when signature is None (no ciphertext). ``summary`` refills
    ``block.text`` as a single ``summary_text`` segment.
    """
    summary_segments = (
        [{"type": "summary_text", "text": block.text}] if block.text else []
    )
    item: dict[str, Any] = {
        "type": "reasoning",
        "summary": summary_segments,
    }
    if block.signature is not None:
        item["encrypted_content"] = block.signature
    return item


def _tool_message_to_responses(
    message: Message,
    image_resolver: Optional[ImageResolver],
    model_supports_vision: bool,
) -> list[dict[str, Any]]:
    """Each ``ToolResultBlock`` on a tool-role message → one
    ``function_call_output`` item.

    ``output`` is kept as-is if already a str; otherwise JSON-serialized (same
    convention as the Chat-compatible adapter).

    Tool-result images (e.g. the ``read`` tool reading a ``.png``): when
    ``block.images`` is non-empty the ``output`` may become a content-part
    array instead of a string — see :func:`_tool_result_output`. The text-only
    path (no images) is unchanged, byte-for-byte.
    """
    items: list[dict[str, Any]] = []
    for block in message.content:
        if not isinstance(block, ToolResultBlock):
            continue
        output = block.output
        rendered = output if isinstance(output, str) else json.dumps(output)
        items.append(
            {
                "type": "function_call_output",
                "call_id": block.call_id,
                "output": _tool_result_output(
                    rendered,
                    block.images,
                    image_resolver,
                    model_supports_vision,
                ),
            }
        )
    return items


def _tool_result_output(
    rendered: str,
    images: Optional[list[ImageBlock]],
    image_resolver: Optional[ImageResolver],
    model_supports_vision: bool,
) -> Any:
    """Build the ``function_call_output.output`` value for one tool result.

    * No images → the plain string ``rendered`` (byte-identical to before; the
      text-only tool path has zero behavior change).
    * Images + vision model + a configured ``image_resolver`` → a content-part
      **array**: the rendered text as one ``input_text`` segment, followed by
      one ``input_image`` data-URI segment per image (the wire shape probed
      against a real gateway — HTTP 200, the model actually sees the image).
    * Images but the model is **not** vision-capable, or no ``image_resolver``
      is configured → degrade to the plain string ``rendered`` with a short
      note appended; **never crash** — the text result still reaches the model.
    """
    if not images:
        return rendered
    if not model_supports_vision or image_resolver is None:
        return rendered + "\n[image omitted: model is not vision-capable]"
    segments: list[dict[str, Any]] = [{"type": "input_text", "text": rendered}]
    for image in images:
        segments.append(_image_block_to_input_image(image, image_resolver))
    return segments


def _tool_to_responses(tool: dict[str, Any]) -> dict[str, Any]:
    """Un-nest the tools shape: Chat's nested ``{type:function,function:{name,
    description,parameters}}`` → Responses' flat ``{type:function,name,
    description,parameters}``.

    Returns as-is if already flat (no ``function`` sub-object) — it accepts
    both the Chat shape and already-flat input, idempotently.
    """
    function = tool.get("function")
    if not isinstance(function, dict):
        return tool
    flat: dict[str, Any] = {"type": "function"}
    if "name" in function:
        flat["name"] = function["name"]
    if "description" in function:
        flat["description"] = function["description"]
    if "parameters" in function:
        flat["parameters"] = function["parameters"]
    return flat


def _function_call_item_to_block(item: dict[str, Any]) -> ToolUseBlock:
    """Inbound ``{type:"function_call"}`` item → ``ToolUseBlock``.

    **Paired by ``call_id``** (not the internal ``id`` — probing shows both
    coexist; the Engine pairs ToolUseBlock to ToolResultBlock by ``call_id``).
    ``arguments`` is a JSON string; a decode failure → a
    ``MalformedToolArgumentsError`` (a ``ValueError`` subclass bucketed
    ``transient``, since a non-decodable arguments string is in practice a
    truncated stream) which RuntimeLLMClient retries on its transient budget.
    """
    # The error prefix ``function_call arguments`` is this provider's own wire
    # vocabulary, passed in verbatim to
    # keep the wording byte-stable; defaulting (None→"{}") and exception
    # convergence (including TypeError) belong to the shared codec.
    arguments = decode_tool_arguments(
        item.get("arguments"), error_label="function_call arguments"
    )
    return ToolUseBlock(
        call_id=str(item.get("call_id") or ""),
        tool_name=str(item.get("name") or ""),
        arguments=arguments,
    )


def _reasoning_item_to_block(item: dict[str, Any]) -> ThinkingBlock:
    """Inbound ``{type:"reasoning"}`` item → ``ThinkingBlock``
    (D3).

    Responses gives two things: ``summary`` (a readable summary array, each
    segment ``{type:summary_text, text}``) + ``encrypted_content`` (opaque
    continuation ciphertext). The mapping is natural:

      * ``ThinkingBlock.text`` ← concatenation of each summary segment's
        ``text`` (joined with ``\\n``).
      * ``ThinkingBlock.signature`` ← ``encrypted_content``.

    ``encrypted_content`` must **round-trip verbatim** (the continuation
    ciphertext is void if one byte changes; probed at ~21.6KB), so it is taken
    as-is, not normalized. Missing ``encrypted_content`` → signature is None;
    empty summary → text is an empty string.
    """
    summary = item.get("summary")
    parts: list[str] = []
    if isinstance(summary, list):
        for segment in summary:
            if isinstance(segment, dict):
                text = segment.get("text")
                if isinstance(text, str):
                    parts.append(text)
    encrypted = item.get("encrypted_content")
    signature = encrypted if isinstance(encrypted, str) else None
    return ThinkingBlock(text="\n".join(parts), signature=signature)


def _message_item_to_blocks(item: dict[str, Any]) -> list[Block]:
    """Inbound ``{type:"message"}`` item → a sequence of ``TextBlock``.

    Assembles all ``output_text`` segments in ``content[]``: each segment
    becomes one ``TextBlock``.
    """
    blocks: list[Block] = []
    segments = item.get("content")
    if not isinstance(segments, list):
        return blocks
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        if segment.get("type") == "output_text":
            text = segment.get("text")
            if isinstance(text, str) and text:
                blocks.append(TextBlock(text=text))
    return blocks


def _translate_usage(usage: Any) -> Usage:
    """Map the Responses usage wire shape to Noeta-shape :class:`Usage`.

    Responses reports more fully than Chat (Chat originally dropped cache,
    D2):

      * ``input_tokens − cached_tokens`` → ``uncached``
      * ``input_tokens_details.cached_tokens`` → ``cache_read``
      * ``cache_write`` is always 0 (Responses does not report cache writes)
      * ``output_tokens`` → ``output``
      * ``output_tokens_details.reasoning_tokens`` → ``reasoning_tokens``

    A missing / non-dict ``usage`` yields an empty :class:`Usage`.
    """
    if not isinstance(usage, dict):
        return Usage()
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    input_details = usage.get("input_tokens_details")
    cached = 0
    if isinstance(input_details, dict):
        cached = int(input_details.get("cached_tokens", 0) or 0)
    output_details = usage.get("output_tokens_details")
    reasoning = 0
    if isinstance(output_details, dict):
        reasoning = int(output_details.get("reasoning_tokens", 0) or 0)
    return Usage(
        uncached=max(0, input_tokens - cached),
        cache_read=cached,
        cache_write=0,
        output=int(usage.get("output_tokens", 0) or 0),
        reasoning_tokens=reasoning,
    )
