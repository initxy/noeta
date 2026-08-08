"""Anthropic ``/v1/messages`` adapter for the Noeta-shape LLM protocol.

Implements :class:`noeta.protocols.messages.LLMProvider` against the Anthropic
Messages API; every loss of fidelity caused by that wire shape is contained to
this file, so Engine and Policy only ever see Noeta-shape types. The provider
pins no model — ``LLMRequest.model`` is forwarded per call — and ``max_tokens``
is fail-fast rather than silently defaulted, because Anthropic requires it and
a guessed cap truncates answers invisibly. Three wire concerns are stamped on
the outbound body only and never enter ``LLMRequest`` or the recorded
``request_ref``: the ephemeral prompt-cache breakpoints, the
``<system-reminder>`` tagging that Anthropic needs because it has no
mid-history system role, and the ``stream`` flag.

**Every request is transported over SSE.** ``complete`` /
``complete_with_headers`` delegate to ``complete_streaming`` with a discarding
sink, so the only difference between them is whether fragments reach a caller
— the wire body, the parse, the error taxonomy and the recorded bytes are one
code path. See :meth:`AnthropicProvider.complete_with_headers` for why the
non-streaming POST had to go.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Callable, Literal, Optional

import httpx

from noeta.protocols.errors import (
    CATEGORY_OVERFLOW,
    AbortedError,
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
    is_host_injected,
)
from noeta.protocols.values import ContentRef
from noeta.builtins.providers.impl import catalog
from noeta.builtins.providers.impl._sse import iter_sse_events
from noeta.builtins.providers.impl.codecs import (
    parse_retry_after,
    render_tool_result_body,
)


#: The adapter's only image dependency: a narrow ``ContentRef → bytes`` deref
#: callback, deliberately not a ContentStore or a StepContext. Deref and base64
#: happen at wire-assembly time and the result is never written back.
ImageResolver = Callable[[ContentRef], bytes]


__all__ = ["AnthropicProvider"]


_API_VERSION_DEFAULT = "2023-06-01"

_API_KEY_ENV = "ANTHROPIC_API_KEY"
_MESSAGES_ENDPOINT = "/v1/messages"

#: The stop_reason a current model returns when the *input* overflowed the
#: context window on an otherwise-successful HTTP 200 turn. It is the same
#: failure the 400 body sniffer catches, arriving over a different transport,
#: so it must reach Policy in the same neutral shape — ``stop_reason="error"``
#: plus ``raw["category"] == "overflow"`` — which is what the compaction
#: policy's passive path keys on. Left unrecognised it falls through to a bare
#: ``"error"`` → ``FailDecision(llm_error, retryable=False)``, killing the task
#: exactly where compaction would have rescued it.
_OVERFLOW_STOP_REASON = "model_context_window_exceeded"

_STOP_REASON_MAP: dict[str, Literal["tool_use", "end_turn", "max_tokens", "error"]] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "end_turn",
    # Neutral ``error``, but a *recoverable* one: ``_parse_response`` stamps the
    # overflow category onto ``raw`` so the Policy compacts and retries instead
    # of failing. Listed explicitly (rather than left to the fall-through) so
    # the mapping is a decision on the record, not an accident.
    _OVERFLOW_STOP_REASON: "error",
    # A safety-classifier ``refusal`` is a *completed* HTTP-200 turn whose
    # content carries the refusal text. The neutral vocabulary has no
    # ``refusal``, so it maps to ``end_turn``: the refusal reaches the caller as
    # the assistant's finished answer rather than an ``error`` that would
    # discard it and fail the task non-retryably. ``pause_turn`` is absent on
    # purpose — Noeta wires no server-side tools, so it is unreachable, and
    # calling it ``end_turn`` would silently truncate a turn the API expects to
    # be resumed; an absent key falls through to ``error``.
    "refusal": "end_turn",
}


def _discard_delta(delta: StreamDelta) -> None:
    """The ``on_delta`` :meth:`AnthropicProvider.complete_with_headers` passes.

    A named no-op rather than a lambda so the intent is on the record: the
    non-streaming entry point uses the streaming *transport* and drops every
    fragment, which is what keeps ``HostConfig.delta_sink`` the only surface
    through which a delta can reach a host.
    """
    return None


class AnthropicProvider:
    """Adapter for the Anthropic Messages API.

    Construct once with the API key and reuse across calls — the underlying
    :class:`httpx.Client` is shared, and ``LLMRequest.model`` selects the model
    per call. ``extra_headers`` is the escape hatch for ``anthropic-beta``
    flags, org IDs and proxy auth; prompt caching needs no beta flag, as the
    ``cache_control`` breakpoints on the wire body are honoured on their own.

    :meth:`complete_with_headers` lets the runtime attach request-scoped
    headers per call over that shared client, which matters because the client
    is built long before any ``task_id`` exists. Such headers are
    transport-only and cannot disturb prompt-cache hits: the cache key is the
    rendered wire body, not the HTTP headers.

    ``image_resolver`` is a narrow ``ContentRef → bytes`` deref callback, of
    the same nature as the httpx client the adapter already holds — never a
    ContentStore or a StepContext, and the base64 it produces never re-enters
    the ledger.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: str = "https://api.anthropic.com",
        anthropic_version: str = _API_VERSION_DEFAULT,
        default_max_tokens: Optional[int] = None,
        timeout_seconds: float = 60.0,
        extra_headers: Optional[dict[str, str]] = None,
        image_resolver: Optional[ImageResolver] = None,
    ) -> None:
        """``api_key`` defaults to the ``ANTHROPIC_API_KEY`` environment variable.

        The conventional env var is read because that is what every other
        Anthropic client does; an explicit argument still wins. Missing
        credentials raise here rather than yielding a client that fails its
        first call with an opaque 401.
        """
        resolved_key = api_key if api_key is not None else os.environ.get(_API_KEY_ENV)
        if not resolved_key:
            raise ValueError(
                "AnthropicProvider needs an API key: pass api_key=... or set "
                f"the {_API_KEY_ENV} environment variable"
            )
        self._default_max_tokens = default_max_tokens
        self._image_resolver = image_resolver
        headers: dict[str, str] = {
            "x-api-key": resolved_key,
            "anthropic-version": anthropic_version,
            "content-type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
        )

    # ------------------------------------------------------------------
    # LLMProvider / HeaderAwareProvider / StreamingProvider Protocol
    # ------------------------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        return self.complete_with_headers(request, None)

    def complete_with_headers(
        self,
        request: LLMRequest,
        request_headers: Optional[dict[str, str]],
    ) -> LLMResponse:
        """Blocking one-shot completion — transported over SSE, not a batch POST.

        The externally visible contract is unchanged: one ``LLMResponse``,
        the same parse, the same neutral error taxonomy, and **no deltas
        anywhere** (the sink below discards). Only the transport moved, because
        the non-streaming POST holds one socket idle for the whole generation
        and dies on the client timeout: ReAct forwards the catalog's
        ``max_output_tokens`` as ``max_tokens`` on every turn, so a long answer
        on a 128 K cap reliably outran the default 60 s and then burned the
        runtime's retry budget re-running the same doomed call. A streamed
        connection keeps producing bytes, so the timeout measures silence
        rather than total generation time.

        This changes no recorded bytes. ``request_ref`` is the canonical
        serialization of the NEUTRAL ``LLMRequest``
        (``noeta.runtime.llm._put_request`` → ``to_canonical_bytes(req)``); the
        wire body — including ``stream: true`` and the ``cache_control``
        breakpoints — is assembled here and never travels back. Prompt-cache
        keys are unaffected for the same reason the header-merge path is: the
        cached prefix is ``tools``/``system``/``messages``, which are
        byte-identical between the two transports.
        """
        # ``request_headers`` merges over the constructor headers, so
        # ``x-api-key`` / ``anthropic-version`` survive unless deliberately
        # overridden. The vision guard and the wire assembly both live in
        # ``complete_streaming``; delegating whole (rather than duplicating the
        # pre-flight) is what keeps the two entry points from drifting.
        return self.complete_streaming(request, _discard_delta, request_headers)

    def complete_streaming(
        self,
        request: LLMRequest,
        on_delta: Callable[[StreamDelta], None],
        request_headers: Optional[dict[str, str]] = None,
        should_abort: Optional[Callable[[], bool]] = None,
    ) -> LLMResponse:
        # Still the blocking one-shot contract of ``complete``: the full
        # ``LLMResponse`` is the return value and ``on_delta`` only fires as a
        # side effect. This IS the transport for both entry points —
        # ``complete_with_headers`` calls straight into here with a discarding
        # sink (and no ``should_abort``: batch semantics are unchanged) — so
        # the wire body, the parse and the error taxonomy cannot drift between
        # a previewed turn and a silent one.
        _guard_vision_capability(request)
        body = self._build_request_body(request)
        body["stream"] = True
        stream_kwargs: dict[str, Any] = {"json": body}
        if request_headers is not None:
            stream_kwargs["headers"] = request_headers
        accumulator = _StreamAccumulator(on_delta)
        # Identical taxonomy to the batch path. The error body must be
        # ``read()`` first: an unread streaming response raises when the
        # overflow check reaches for ``.json()``. Timeout and transport
        # failures — including a disconnect mid-iteration — stay transient so
        # the runtime retry loop applies unchanged.
        try:
            with self._client.stream(
                "POST", _MESSAGES_ENDPOINT, **stream_kwargs
            ) as http_response:
                if http_response.is_error:
                    http_response.read()
                try:
                    http_response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise _translate_http_error(exc) from exc
                for event_name, data in iter_sse_events(
                    http_response.iter_lines()
                ):
                    # Client-side stop: polled per event and raised from
                    # INSIDE the stream context, so the exit closes the
                    # connection and the model stops burning tokens.
                    # ``AbortedError`` is neither an httpx type nor transient,
                    # so it passes the handler below untranslated.
                    if should_abort is not None and should_abort():
                        raise AbortedError("client abort mid-stream")
                    accumulator.feed(event_name, data)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(str(exc)) from exc
        # Reusing the batch parse path is what pins a streamed response
        # shape-identical to a batch one. ``raw`` then carries the
        # reconstructed dict: diagnostics only, never part of the recording.
        return self._parse_response(accumulator.message_payload())

    # ------------------------------------------------------------------
    # Outbound translation (Noeta → Anthropic)
    # ------------------------------------------------------------------

    def _build_request_body(self, request: LLMRequest) -> dict[str, Any]:
        max_tokens = self._resolve_max_tokens(request)
        # One catalog lookup for the whole request: the tool renderer consults
        # this to inline tool-result images or degrade them to string content.
        # Top-level images were already settled by the guard.
        vision = _model_admits_images(request.model)

        # Anthropic has no mid-history system role, so a host-injected turn
        # renders as <system-reminder>-wrapped text and merges into the
        # adjacent user wire turn. Only pairs touching an injected turn merge;
        # plain consecutive user turns keep their 1:1 rendering.
        outbound_messages: list[dict[str, Any]] = []
        prev_injected = False
        for message in request.messages:
            if message.role == "system":
                raise ValueError(
                    "system must use LLMRequest.system field, not messages array"
                )
            wire = _message_to_anthropic(message, self._image_resolver, vision)
            injected = is_host_injected(message)
            if (
                (injected or prev_injected)
                and wire["role"] == "user"
                and outbound_messages
                and outbound_messages[-1]["role"] == "user"
            ):
                outbound_messages[-1]["content"] = [
                    *outbound_messages[-1]["content"],
                    *wire["content"],
                ]
            else:
                outbound_messages.append(wire)
            prev_injected = injected

        body: dict[str, Any] = {
            "model": request.model,
            "max_tokens": max_tokens,
            "messages": outbound_messages,
        }
        if request.system is not None:
            body["system"] = _flatten_text_blocks(request.system)
        if request.tools:
            body["tools"] = _translate_tools(request.tools)
        # ``LLMRequest`` has no tool_choice field — it rides ``metadata`` (the
        # summarize round-trip sets ``"none"`` so the summarizer cannot answer
        # with a tool call; see react.py ``_summary_prompt_request``). The
        # neutral spelling is a bare string, which Anthropic wants wrapped as
        # ``{"type": ...}``; a dict rides through verbatim for callers that
        # already speak the vendor shape.
        tool_choice = request.metadata.get("tool_choice")
        if tool_choice is not None:
            body["tool_choice"] = (
                {"type": tool_choice}
                if isinstance(tool_choice, str)
                else tool_choice
            )
        # ``cache_control`` is an Anthropic wire concern and must never reach
        # LLMRequest / request_ref, so it is stamped on the just-built body.
        _apply_cache_control(body)
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.output_schema is not None:
            body["output_config"] = {
                **body.get("output_config", {}),
                "format": {
                    "type": "json_schema",
                    "schema": dict(request.output_schema),
                },
            }
        # Effort rides inside output_config, alongside any format set above.
        if request.effort is not None:
            body.setdefault("output_config", {})["effort"] = request.effort
        # Thinking mode is a sibling of output_config, not nested inside it.
        if request.thinking is not None:
            body["thinking"] = {"type": request.thinking}
        return body

    def _resolve_max_tokens(self, request: LLMRequest) -> int:
        """Explicit request beats constructor default; neither raises.

        Anthropic requires ``max_tokens``, and a cap this adapter invented
        would truncate answers with no signal, so it never picks one.
        """
        if request.max_tokens is not None:
            return int(request.max_tokens)
        if self._default_max_tokens is not None:
            return int(self._default_max_tokens)
        raise ValueError(
            "Anthropic requires max_tokens; pass LLMRequest.max_tokens or "
            "AnthropicProvider(default_max_tokens=...)"
        )

    # ------------------------------------------------------------------
    # Inbound translation (Anthropic → Noeta)
    # ------------------------------------------------------------------

    def _parse_response(self, payload: dict[str, Any]) -> LLMResponse:
        if payload.get("type") != "message":
            raise ValueError(
                f"Anthropic response 'type' was not 'message': "
                f"got {payload.get('type')!r}"
            )
        if payload.get("role") != "assistant":
            raise ValueError(
                f"Anthropic response 'role' was not 'assistant': "
                f"got {payload.get('role')!r}"
            )
        content_raw = payload.get("content")
        if not isinstance(content_raw, list):
            raise ValueError(
                f"Anthropic response 'content' must be a list: "
                f"got type={type(content_raw).__name__}"
            )

        content = _parse_response_content(content_raw)

        raw_stop = payload.get("stop_reason")
        stop_reason = _STOP_REASON_MAP.get(raw_stop or "", "error")

        has_tool_use = any(isinstance(b, ToolUseBlock) for b in content)
        if stop_reason == "tool_use" and not has_tool_use:
            raise ValueError(
                "inconsistent Anthropic response: stop_reason='tool_use' "
                "but content has no tool_use block"
            )
        if stop_reason == "end_turn" and has_tool_use:
            raise ValueError(
                "inconsistent Anthropic response: stop_reason='end_turn' "
                "but content has tool_use block(s)"
            )

        usage_raw = payload.get("usage") or {}
        usage = _translate_usage(usage_raw)

        raw: dict[str, Any] = payload
        if raw_stop == _OVERFLOW_STOP_REASON:
            # Same neutral shape the 400 path produces (a ``ContextOverflowError``
            # the runtime renders as ``raw['category'] == 'overflow'``), so both
            # transports converge on the Policy's one passive-compaction branch.
            # Copied, never mutated in place: ``payload`` is the recorded body.
            raw = {**payload, "category": CATEGORY_OVERFLOW}

        return LLMResponse(
            stop_reason=stop_reason,
            content=content,
            usage=usage,
            raw=raw,
        )


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------

#: Substrings in an ``invalid_request_error`` message that mean the prompt
#: exceeded the context window. Every marker must be a phrase that *only* an
#: input-context overflow emits: the over-broad ``"max tokens"`` /
#: ``"too many tokens"`` are excluded because they also appear in 400s
#: compaction cannot fix (an output-cap validation message), and they add
#: nothing — the real overflow body reads ``"prompt is too long: N tokens > M
#: maximum"``, which the tight phrasings already catch.
_OVERFLOW_MESSAGE_MARKERS: tuple[str, ...] = (
    "prompt is too long",
    "prompt too long",
    "context window",
    "maximum context",
)


def _translate_http_error(exc: httpx.HTTPStatusError) -> Exception:
    """Map an Anthropic-shape HTTP status error into the neutral taxonomy.

    429, 529 (overloaded) and 5xx are retryable; every other 4xx is fatal. A
    400 gets its body sniffed first, because context overflow arrives as an
    ordinary ``invalid_request_error`` yet needs its own bucket — it is not
    retryable and the recovery a Policy drives is compaction.
    """
    response = exc.response
    status = response.status_code
    if status == 429:
        return TransientError(
            str(exc),
            retry_after=parse_retry_after(response.headers.get("retry-after")),
        )
    if status == 529 or status >= 500:
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
    if str(error.get("type") or "") != "invalid_request_error":
        return False
    message = str(error.get("message") or "").lower()
    return any(marker in message for marker in _OVERFLOW_MESSAGE_MARKERS)


#: In-band SSE ``error`` event types that bucket as :class:`TransientError` —
#: the same failure classes their HTTP-status counterparts land in (429 / 500 /
#: 529).
_TRANSIENT_STREAM_ERROR_TYPES: frozenset = frozenset(
    {"rate_limit_error", "api_error", "overloaded_error"}
)


def _translate_stream_error_event(payload: dict[str, Any]) -> Exception:
    """Map an in-band SSE ``error`` event into the neutral taxonomy.

    The 200 header is already on the wire by then, so mid-stream failures
    arrive as ``event: error`` frames and this classifies on the error *type*
    where :func:`_translate_http_error` classifies on a status code. A frame
    carries no headers, hence no ``retry_after`` — the runtime falls back to
    exponential backoff.
    """
    error = payload.get("error")
    if not isinstance(error, dict):
        error = {}
    error_type = str(error.get("type") or "")
    message = str(error.get("message") or "")
    described = f"Anthropic stream error event: {error_type}: {message}"
    if error_type in _TRANSIENT_STREAM_ERROR_TYPES:
        return TransientError(described)
    if error_type == "invalid_request_error" and any(
        marker in message.lower() for marker in _OVERFLOW_MESSAGE_MARKERS
    ):
        return ContextOverflowError(described)
    return FatalError(described)


# ---------------------------------------------------------------------------
# Usage translation
# ---------------------------------------------------------------------------


def _translate_usage(usage_raw: Any) -> Usage:
    """Map Anthropic's usage wire shape into Noeta-shape :class:`Usage`.

    ``input_tokens`` maps straight to ``uncached`` because Anthropic already
    excludes the cache buckets from it and bills them separately — the
    opposite of OpenAI, whose prompt count is a total the cached part must be
    subtracted from. The derived ``Usage.input`` therefore sums back to the
    real total. Anthropic reports no reasoning-token field, so that stays 0,
    and a missing or non-dict ``usage`` degrades to an empty ``Usage()``.
    """
    if not isinstance(usage_raw, dict):
        return Usage()
    return Usage(
        uncached=int(usage_raw.get("input_tokens", 0) or 0),
        cache_read=int(usage_raw.get("cache_read_input_tokens", 0) or 0),
        cache_write=int(usage_raw.get("cache_creation_input_tokens", 0) or 0),
        output=int(usage_raw.get("output_tokens", 0) or 0),
    )


# ---------------------------------------------------------------------------
# Outbound helpers
# ---------------------------------------------------------------------------


def _model_admits_images(model: str) -> bool:
    """Whether an image may be sent to ``model``.

    Three states collapse into two answers, and the middle one is the whole
    point of this helper:

    * catalogued with ``supports_vision=True`` — yes.
    * catalogued with ``supports_vision=False`` — no. Somebody decided this
      model is text-only, so the adapter refuses locally with a clear message
      instead of paying a round-trip for a vendor 4xx.
    * **absent from the catalog — yes.** Nobody decided anything; the model is
      unknown, not text-only. Treating unknown as non-vision made an
      uncatalogued gateway model refuse every image locally, with no signal
      that the catalog (rather than the model) was the thing saying no. The
      provider is the authority on its own capabilities and answers with a
      clear error if the model truly cannot read images.
    """
    spec = catalog.find_spec(model)
    if spec is None:
        return True
    return bool(spec.supports_vision)


def _request_has_image(request: LLMRequest) -> bool:
    """True if any ``Message`` carries a **top-level** ``ImageBlock``.

    Tool-result images ride ``ToolResultBlock.images`` and are deliberately
    invisible here: their non-vision degrade is the tool renderer's job, not
    the guard's.
    """
    return any(
        isinstance(block, ImageBlock)
        for message in request.messages
        for block in message.content
    )


def _guard_vision_capability(request: LLMRequest) -> None:
    """A top-level ``ImageBlock`` bound for a **catalogued** non-vision model →
    :class:`FatalError`.

    Refusing before wire assembly beats letting the gateway answer with a
    cryptic 4xx or, worse, silently ignore the image. An *uncatalogued* model
    is not refused — see :func:`_model_admits_images` for why the two cases
    part company. The catalog is consulted only when an image is actually
    present, so the text-only path pays nothing.
    """
    if not _request_has_image(request):
        return
    if _model_admits_images(request.model):
        return
    raise FatalError(
        f"request carries an ImageBlock but model {request.model!r} is "
        "catalogued with supports_vision=False; refusing to send the image to "
        "a model that cannot read it."
    )


def _image_block_to_anthropic(
    block: ImageBlock, image_resolver: Optional[ImageResolver]
) -> dict[str, Any]:
    """``ImageBlock(ContentRef)`` → an Anthropic base64 ``image`` content block.

    Deref and base64 happen only at wire-assembly time; the result is
    transient and never written back to the ledger or ContentStore. A missing
    ``image_resolver`` is incomplete configuration and errors out, because a
    silently dropped image is far harder to notice.
    """
    if image_resolver is None:
        raise ValueError(
            "request carries an ImageBlock but provider has no image_resolver "
            "configured; cannot deref image bytes (set image_resolver to "
            "content_store.get). Refusing to silently drop the image."
        )
    raw = image_resolver(block.source)
    b64 = base64.b64encode(raw).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": block.source.media_type,
            "data": b64,
        },
    }


def _flatten_text_blocks(message: Message) -> str:
    return "\n".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    )


def _wrap_system_reminder(text: str) -> str:
    """Anthropic-only tag syntax; it lives here so it never enters the ledger."""
    return f"<system-reminder>\n{text}\n</system-reminder>"


def _message_to_anthropic(
    message: Message,
    image_resolver: Optional[ImageResolver],
    vision: bool,
) -> dict[str, Any]:
    if message.role == "user":
        return _user_message_to_anthropic(message, image_resolver)
    if message.role == "assistant":
        return _assistant_message_to_anthropic(message, image_resolver)
    if message.role == "tool":
        return _tool_message_to_anthropic(message, image_resolver, vision)
    raise ValueError(f"unsupported message role: {message.role!r}")


def _user_message_to_anthropic(
    message: Message, image_resolver: Optional[ImageResolver]
) -> dict[str, Any]:
    """``ToolResultBlock`` raises here rather than being translated: tool
    results must ride ``role='tool'`` so Anthropic's tool_use → tool_result
    wire ordering stays intact. Thinking / ToolUse blocks are skipped silently
    — they don't appear in user history, matching OpenAICompatProvider's
    tolerance. Any non-vision image misroute was already rejected by the vision
    guard upstream."""
    wrap = is_host_injected(message)
    blocks: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, ToolResultBlock):
            raise ValueError(
                "ToolResultBlock not allowed in role='user' message; "
                "use role='tool' instead"
            )
        if isinstance(block, TextBlock):
            text = _wrap_system_reminder(block.text) if wrap else block.text
            blocks.append({"type": "text", "text": text})
        elif isinstance(block, ImageBlock):
            blocks.append(_image_block_to_anthropic(block, image_resolver))
    return {"role": "user", "content": blocks}


def _assistant_message_to_anthropic(
    message: Message, image_resolver: Optional[ImageResolver]
) -> dict[str, Any]:
    """Regroup ``Message.content`` into Anthropic's required order
    ``thinking* / text* / image* / tool_use*``, stable-sorted within each group
    to preserve caller ordering across multi-thinking or multi-tool-use turns.
    ``ToolResultBlock`` is skipped silently (caller bug, matches
    OpenAICompatProvider tolerance); the non-vision image misroute was already
    rejected upstream."""
    thinking_blocks: list[ThinkingBlock] = []
    text_blocks: list[TextBlock] = []
    image_blocks: list[ImageBlock] = []
    tool_use_blocks: list[ToolUseBlock] = []
    for block in message.content:
        if isinstance(block, ThinkingBlock):
            thinking_blocks.append(block)
        elif isinstance(block, TextBlock):
            text_blocks.append(block)
        elif isinstance(block, ImageBlock):
            image_blocks.append(block)
        elif isinstance(block, ToolUseBlock):
            tool_use_blocks.append(block)
    content: list[dict[str, Any]] = []
    for thinking in thinking_blocks:
        if thinking.data is not None:
            # A redacted (encrypted) reasoning block: re-emit the opaque blob
            # verbatim under its own wire type, never as a ``thinking`` block
            # (an empty-text thinking block would be rejected).
            content.append(
                {"type": "redacted_thinking", "data": thinking.data}
            )
            continue
        entry: dict[str, Any] = {
            "type": "thinking",
            "thinking": thinking.text,
        }
        if thinking.signature is not None:
            entry["signature"] = thinking.signature
        content.append(entry)
    for text in text_blocks:
        content.append({"type": "text", "text": text.text})
    for image in image_blocks:
        content.append(_image_block_to_anthropic(image, image_resolver))
    for tool_use in tool_use_blocks:
        content.append(
            {
                "type": "tool_use",
                "id": tool_use.call_id,
                "name": tool_use.tool_name,
                "input": tool_use.arguments,
            }
        )
    return {"role": "assistant", "content": content}


def _tool_message_to_anthropic(
    message: Message,
    image_resolver: Optional[ImageResolver],
    vision: bool,
) -> dict[str, Any]:
    """``role='tool'`` becomes one Anthropic user message whose content is
    **only** ``tool_result`` blocks (in input order). Non-ToolResultBlock
    content raises: the strict placement keeps Anthropic's "tool_use must be
    followed by a tool_result user turn" wire-shape invariant intact."""
    blocks: list[dict[str, Any]] = []
    for block in message.content:
        if not isinstance(block, ToolResultBlock):
            raise ValueError(
                "role='tool' message may only contain ToolResultBlock; "
                f"got {type(block).__name__}"
            )
        blocks.append(
            {
                "type": "tool_result",
                "tool_use_id": block.call_id,
                "content": _tool_result_content(block, image_resolver, vision),
                "is_error": not block.success,
            }
        )
    return {"role": "user", "content": blocks}


def _tool_result_content(
    block: ToolResultBlock,
    image_resolver: Optional[ImageResolver],
    vision: bool,
) -> Any:
    """Render ``ToolResultBlock`` for Anthropic ``tool_result.content``.

    Anthropic's ``tool_result.content`` accepts either a bare string or a block
    array. With no images (or a non-vision model) this returns the **string**
    (JSON-encode non-string outputs; ``error`` prefixed), keeping the text-only
    path byte-identical. When the block carries ``images`` AND the model is
    vision-capable, it returns an **array**: one ``text`` block holding that same
    string, followed by one base64 ``image`` block per image (deref'd via
    ``image_resolver``)."""
    text = _tool_result_text(block)
    if vision and block.images:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for image in block.images:
            content.append(_image_block_to_anthropic(image, image_resolver))
        return content
    return text


def _tool_result_text(block: ToolResultBlock) -> str:
    """The string rendering of ``ToolResultBlock.output`` — the shared
    :func:`render_tool_result_body` convention (string outputs verbatim,
    ``ensure_ascii=False`` for structured ones, error text leading), on top of
    which Anthropic additionally carries ``is_error`` on the wire."""
    return render_tool_result_body(block.output, block.error)


#: The single ephemeral cache breakpoint marker reused on every stamp site.
#: Default TTL (5 min); no extended-TTL flag.
_CACHE_CONTROL_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}


def _apply_cache_control(body: dict[str, Any]) -> None:
    """Stamp ephemeral prompt-cache breakpoints onto the outbound wire body.

    Mutates ``body`` in place; the caller passes the just-built wire dict, so
    no LLMRequest / request_ref bytes are touched.

    Anthropic renders the prefix in the order ``tools`` → ``system`` →
    ``messages``, and a breakpoint caches everything *before* it, so each of
    the three stamps below (≤4 is the vendor cap) covers strictly more than the
    one above it:

    * **last tool**: stamp the final tool dict — caches the tool schemas
      (which render first, so this breakpoint covers tools ONLY, not system).
    * **system**: if present, lift the flat string into block form
      ``[{"type":"text","text":...,"cache_control":...}]`` — Anthropic requires
      block (not bare-string) shape to carry cache_control. Caches
      tools + system, i.e. the bulk of the stable bytes.
    * **last message's last content block**: stamp the final content block —
      caches tools + system + the growing conversation up to that point. Every
      wire content block is already a dict here, so it can carry the field
      directly.

    (The stamps are applied system-first below purely because the system block
    needs re-shaping first; order of application is irrelevant — only wire
    position is.)
    """
    system = body.get("system")
    if isinstance(system, str):
        body["system"] = [
            {
                "type": "text",
                "text": system,
                "cache_control": dict(_CACHE_CONTROL_EPHEMERAL),
            }
        ]

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        tools[-1]["cache_control"] = dict(_CACHE_CONTROL_EPHEMERAL)

    messages = body.get("messages")
    if isinstance(messages, list) and messages:
        last_content = messages[-1].get("content")
        if isinstance(last_content, list) and last_content:
            last_content[-1]["cache_control"] = dict(_CACHE_CONTROL_EPHEMERAL)


def _translate_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unpack OpenAI-shape tool dicts into Anthropic-shape. ``function`` /
    ``name`` / ``parameters`` must all be present with the right types."""
    out: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict):
            raise ValueError(
                f"tool entry missing 'function' dict: {tool!r}"
            )
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(
                f"tool function missing/invalid 'name' (must be non-empty str): "
                f"{function!r}"
            )
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError(
                f"tool function missing/invalid 'parameters' (must be dict): "
                f"{function!r}"
            )
        description = function.get("description", "")
        if not isinstance(description, str):
            description = ""
        out.append(
            {
                "name": name,
                "description": description,
                "input_schema": parameters,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Inbound helpers
# ---------------------------------------------------------------------------


def _parse_response_content(content_raw: list[Any]) -> list[Block]:
    """Translate Anthropic response content blocks into Noeta Block instances.
    Unknown block types are skipped to stay forward-compatible with new
    Anthropic block types; the missing block reaches the caller as a
    content-shape gap (one fewer block than the wire carried)."""
    blocks: list[Block] = []
    for entry in content_raw:
        if not isinstance(entry, dict):
            raise ValueError(f"Anthropic content entry not a dict: {entry!r}")
        entry_type = entry.get("type")
        if entry_type == "text":
            text = entry.get("text", "")
            if not isinstance(text, str):
                raise ValueError(
                    f"Anthropic 'text' block 'text' not a str: {entry!r}"
                )
            blocks.append(TextBlock(text=text))
        elif entry_type == "thinking":
            text = entry.get("thinking", "")
            signature = entry.get("signature")
            blocks.append(
                ThinkingBlock(
                    text=text if isinstance(text, str) else "",
                    signature=signature if isinstance(signature, str) else None,
                )
            )
        elif entry_type == "redacted_thinking":
            # Encrypted reasoning the safety system redacted. There is nothing
            # human-readable to keep, but the opaque ``data`` blob MUST round-
            # trip verbatim on the next request (a tool-use turn that carried
            # thinking is rejected if its reasoning blocks are missing). Carry
            # it on ``ThinkingBlock.data`` rather than dropping the block. If the
            # blob is missing / non-str there is nothing to round-trip, and a
            # ``ThinkingBlock(text="", data=None)`` would be re-emitted outbound
            # as an empty ``thinking`` block (the API rejects it), so drop it.
            data = entry.get("data")
            if isinstance(data, str):
                blocks.append(ThinkingBlock(text="", signature=None, data=data))
        elif entry_type == "tool_use":
            call_id = entry.get("id", "")
            tool_name = entry.get("name", "")
            arguments = entry.get("input", {})
            if not isinstance(arguments, dict):
                raise ValueError(
                    f"Anthropic 'tool_use.input' not a JSON object: {entry!r}"
                )
            blocks.append(
                ToolUseBlock(
                    call_id=call_id if isinstance(call_id, str) else "",
                    tool_name=tool_name if isinstance(tool_name, str) else "",
                    arguments=arguments,
                )
            )
    return blocks


# ---------------------------------------------------------------------------
# Streaming accumulation (StreamingProvider capability)
# ---------------------------------------------------------------------------


class _StreamAccumulator:
    """Rebuilds the vendor-shaped ``message`` payload from Messages-API stream
    events, firing :class:`StreamDelta` side effects along the way.

    Only ``text_delta`` / ``thinking_delta`` fragments surface as deltas.
    ``input_json_delta`` (tool arguments) and ``signature_delta`` accumulate
    **silently** — argument JSON is undecodable while partial and the
    signature is an opaque continuation token, so neither is previewable.
    ``redacted_thinking`` blocks arrive whole on ``content_block_start`` (the
    opaque ``data`` blob rides the block; no deltas follow). Unknown event /
    delta types are skipped silently, mirroring the batch parser's
    forward-compatibility stance. The finished payload feeds
    ``AnthropicProvider._parse_response`` unchanged — that reuse is what pins
    streamed and batch responses shape-identical.
    """

    def __init__(self, on_delta: Callable[[StreamDelta], None]) -> None:
        self._on_delta = on_delta
        self._message: Optional[dict[str, Any]] = None
        self._blocks: dict[int, dict[str, Any]] = {}
        self._json_fragments: dict[int, list[str]] = {}

    def feed(self, event_name: Optional[str], data: str) -> None:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Anthropic stream event was not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(
                f"Anthropic stream event root was not a JSON object: "
                f"type={type(payload).__name__}"
            )
        if event_name == "message_start":
            message = payload.get("message")
            self._message = dict(message) if isinstance(message, dict) else {}
        elif event_name == "content_block_start":
            self._start_block(payload)
        elif event_name == "content_block_delta":
            self._apply_delta(payload)
        elif event_name == "content_block_stop":
            index = payload.get("index")
            if isinstance(index, int):
                self._finalize_block(index)
        elif event_name == "message_delta":
            self._apply_message_delta(payload)
        elif event_name == "error":
            raise _translate_stream_error_event(payload)
        # message_stop / ping / unknown event types: nothing to accumulate.

    def _start_block(self, payload: dict[str, Any]) -> None:
        index = payload.get("index")
        block = payload.get("content_block")
        if not isinstance(index, int) or not isinstance(block, dict):
            return
        # Copied because the dict is mutated as deltas accumulate. A
        # ``tool_use`` start carries id / name (its ``input`` grows from
        # ``input_json_delta`` fragments); a ``redacted_thinking`` start is
        # already complete.
        self._blocks[index] = dict(block)
        if block.get("type") == "tool_use":
            self._json_fragments[index] = []

    def _apply_delta(self, payload: dict[str, Any]) -> None:
        index = payload.get("index")
        delta = payload.get("delta")
        if not isinstance(index, int) or not isinstance(delta, dict):
            return
        block = self._blocks.get(index)
        if block is None:
            # A delta for a block that never started: skip the accumulation
            # AND the preview together, so the emitted deltas never disagree
            # with the final content.
            return
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            fragment = delta.get("text")
            if isinstance(fragment, str):
                block["text"] = str(block.get("text") or "") + fragment
                self._on_delta(
                    StreamDelta(kind="text", text=fragment, index=index)
                )
        elif delta_type == "thinking_delta":
            fragment = delta.get("thinking")
            if isinstance(fragment, str):
                block["thinking"] = str(block.get("thinking") or "") + fragment
                self._on_delta(
                    StreamDelta(kind="thinking", text=fragment, index=index)
                )
        elif delta_type == "input_json_delta":
            fragment = delta.get("partial_json")
            if isinstance(fragment, str):
                self._json_fragments.setdefault(index, []).append(fragment)
        elif delta_type == "signature_delta":
            fragment = delta.get("signature")
            if isinstance(fragment, str):
                block["signature"] = (
                    str(block.get("signature") or "") + fragment
                )
        # Unknown delta types: silently skipped for forward compatibility.

    def _finalize_block(self, index: int) -> None:
        """Decode a ``tool_use`` block's accumulated argument fragments.

        The joined string is the same wire shape the batch path receives
        inside the response body JSON, so the same decode applies
        (``json.loads``, then ``_parse_response_content`` enforces the
        JSON-object check). An empty accumulation keeps the ``input`` the
        block started with (``{}`` on the wire — a no-argument tool call).
        Idempotent: fragments are popped, so the defensive re-finalize at
        assembly time is a no-op for already-stopped blocks.
        """
        fragments = self._json_fragments.pop(index, None)
        if not fragments:
            return
        joined = "".join(fragments)
        if not joined.strip():
            return
        block = self._blocks.get(index)
        if block is None:
            return
        try:
            block["input"] = json.loads(joined)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Anthropic streamed tool_use input was not valid JSON: {exc}"
            ) from exc

    def _apply_message_delta(self, payload: dict[str, Any]) -> None:
        """``message_delta``: the terminal stop signal + output-side usage.

        Usage keys merge over the ``message_start`` snapshot — Anthropic
        reports the input side up front and the (cumulative) output side
        here, so the merged dict is exactly the batch response's ``usage``.
        """
        if self._message is None:
            return
        delta = payload.get("delta")
        if isinstance(delta, dict):
            if "stop_reason" in delta:
                self._message["stop_reason"] = delta.get("stop_reason")
            if "stop_sequence" in delta:
                self._message["stop_sequence"] = delta.get("stop_sequence")
            if "stop_details" in delta:
                # Capture only — nothing branches on it. The batch path carries
                # it for free (``raw`` is the whole payload); without this line
                # the streamed reconstruction would silently drop the vendor's
                # explanation of WHY the turn stopped.
                self._message["stop_details"] = delta.get("stop_details")
        usage = payload.get("usage")
        if isinstance(usage, dict):
            merged = self._message.get("usage")
            merged = dict(merged) if isinstance(merged, dict) else {}
            merged.update(usage)
            self._message["usage"] = merged

    def message_payload(self) -> dict[str, Any]:
        """The reconstructed vendor-shaped ``message`` dict, ready for
        ``AnthropicProvider._parse_response``."""
        if self._message is None:
            raise ValueError(
                "Anthropic stream ended without a message_start event"
            )
        # Defensive: decode any tool_use block whose content_block_stop never
        # arrived (clean-but-unterminated close). Already-stopped blocks were
        # finalized in feed(); the pop makes this a no-op for them.
        for index in list(self._json_fragments):
            self._finalize_block(index)
        self._message["content"] = [
            self._blocks[index] for index in sorted(self._blocks)
        ]
        return self._message
