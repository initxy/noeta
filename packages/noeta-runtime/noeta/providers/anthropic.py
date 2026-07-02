"""Anthropic ``/v1/messages`` adapter for the Noeta-shape LLM protocol.

Issue 22. Implements :class:`noeta.protocols.messages.LLMProvider`
against the official Anthropic Messages API. The translation contract
mirrors :mod:`noeta.providers.openai_compat`: every loss
of fidelity caused by speaking Anthropic's wire shape is contained to
this single file; Engine / Policy only ever see Noeta-shape
types.

Key contracts (issue 22 design doc §"Anthropic Messages API translation rules"):

* Provider does not pin a model. ``LLMRequest.model`` is forwarded
  per-call so one instance can talk to multiple models.
* ``max_tokens`` is fail-fast (rev2 B4): `LLMRequest.max_tokens` wins;
  otherwise ``default_max_tokens`` constructor parameter; otherwise
  raise ``ValueError``. Adapter does not pick a silent default.
* ``LLMRequest.system`` is flattened to a single string at top-level
  ``system``; ``role=='system'`` inside ``LLMRequest.messages`` is
  rejected (same as ``OpenAICompatProvider``).
* Assistant content is regrouped deterministically (rev2 B2):
  ``ThinkingBlock*`` → ``TextBlock*`` → ``ToolUseBlock*``. Stable
  sort within each group preserves caller order.
* ``Message(role='tool')`` becomes one Anthropic ``user`` message
  containing only ``tool_result`` content blocks (rev2 B3). Mixed
  ``ToolResultBlock`` inside ``role='user'`` and non-ToolResultBlock
  inside ``role='tool'`` raise ``ValueError`` — wire-shape placement
  is adapter responsibility, ID alignment stays with Engine.
* Extended thinking (rev2 B1): adapter-unit round-trip only.
  End-to-end signature continuation needs upstream ReActPolicy /
  Composer / RuntimeLLMClient changes and is a follow-up.
* Tools schema is OpenAI-shape on the way in (Composer emits OpenAI
  shape); adapter unpacks to Anthropic shape. Missing function /
  parameters / name raise.
* ``stop_reason`` mapping: ``end_turn`` / ``tool_use`` /
  ``max_tokens`` pass through; ``stop_sequence`` collapses to
  ``end_turn``; unknown / missing maps to ``error`` without raising
  (matches OpenAICompatProvider). Inconsistent state (e.g.
  ``stop_reason='tool_use'`` with no ``tool_use`` block in
  ``content``) raises ``ValueError``.

This module does not implement async, streaming, retry, or Bedrock /
Vertex auth. Image input IS supported on vision-capable models: an
``ImageBlock`` (user/assistant content, or a ``ToolResultBlock``'s
``images``) is deref'd via the injected ``image_resolver`` and base64-inlined
onto the wire (an ``image`` content block / a ``tool_result.content`` array);
a top-level image bound for a non-vision model raises ``FatalError`` up front,
and a tool-result image on a non-vision model degrades to string content.
Prompt caching is applied as ephemeral cache_control breakpoints on the
outbound wire body only (#4); it never enters ``LLMRequest`` / the recorded
``request_ref``.
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
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.values import ContentRef
from noeta.providers import catalog
from noeta.providers.codecs import parse_retry_after


#: A narrowly injected ``ContentRef → bytes`` deref callback (backed by
#: ``content_store.get``). It is the **only** image dependency this adapter
#: holds — it does NOT carry a ContentStore / StepContext, and deref→base64
#: happens only at wire-assembly time, never written back to the ledger.
ImageResolver = Callable[[ContentRef], bytes]


__all__ = ["AnthropicProvider"]


_API_VERSION_DEFAULT = "2023-06-01"
_MESSAGES_ENDPOINT = "/v1/messages"

_STOP_REASON_MAP: dict[str, Literal["tool_use", "end_turn", "max_tokens", "error"]] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "end_turn",
    # A safety-classifier ``refusal`` is a *completed* HTTP-200 turn (the
    # assistant declined; its content carries the refusal text). The Noeta
    # neutral vocabulary has no ``refusal`` value, so map it to ``end_turn`` —
    # the refusal surfaces as the assistant's finished answer, NOT a fatal
    # ``error`` task failure (which would discard the refusal and terminate the
    # task non-retryably). ``pause_turn`` (Anthropic server-side tools mid-turn)
    # is deliberately absent: Noeta wires no server-side tools, so it is
    # unreachable, and mapping it to ``end_turn`` would silently truncate a turn
    # the API expects to be resumed — an absent key falls through to ``error``.
    "refusal": "end_turn",
}


class AnthropicProvider:
    """Adapter for the Anthropic Messages API.

    Construct once with the API key and reuse across calls — the
    underlying :class:`httpx.Client` is shared, and ``LLMRequest.model``
    selects the model per call. ``extra_headers`` is the escape hatch
    for ``anthropic-beta`` flags / org IDs / proxy auth. Prompt caching is
    GA and needs NO ``anthropic-beta`` header — the ephemeral
    ``cache_control`` breakpoints ``_apply_cache_control`` stamps on the wire
    body are honoured on their own (adding a ``prompt-caching-*`` beta flag is
    unnecessary).

    Implements the optional
    :class:`~noeta.protocols.messages.HeaderAwareProvider` capability
    (:meth:`complete_with_headers`): the runtime can attach request-scoped
    headers per call over the shared client. Those headers are transport-only
    and never affect prompt-cache hits (the cache key is the rendered wire
    body, not the HTTP headers).

    ``image_resolver`` is a narrowly injected ``ContentRef → bytes`` deref
    callback (same nature as the httpx client it already holds): when a request
    carries an ``ImageBlock`` (user/assistant content or a ``ToolResultBlock``'s
    ``images``) and the target model is vision-capable, the bytes are deref'd and
    base64-inlined onto the outbound wire body. **Red line**: it does not hold a
    ContentStore / StepContext, and the base64 never re-enters the ledger.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.anthropic.com",
        anthropic_version: str = _API_VERSION_DEFAULT,
        default_max_tokens: Optional[int] = None,
        timeout_seconds: float = 60.0,
        extra_headers: Optional[dict[str, str]] = None,
        image_resolver: Optional[ImageResolver] = None,
    ) -> None:
        self._default_max_tokens = default_max_tokens
        self._image_resolver = image_resolver
        headers: dict[str, str] = {
            "x-api-key": api_key,
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
    # LLMProvider / HeaderAwareProvider Protocol
    # ------------------------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        return self.complete_with_headers(request, None)

    def complete_with_headers(
        self,
        request: LLMRequest,
        request_headers: Optional[dict[str, str]],
    ) -> LLMResponse:
        # HeaderAwareProvider capability: the runtime attaches request-scoped
        # headers (e.g. a per-task tracing/log id from a gateway) per call
        # WITHOUT rebuilding the shared client — the httpx client is a
        # server-level singleton constructed before any ``task_id`` exists.
        # ``request_headers`` merges over the client's constructor headers
        # (``x-api-key`` / ``anthropic-version`` stay unless overridden). These
        # are transport-only: they never enter ``LLMRequest`` / ``request_ref``,
        # and — because the Anthropic cache key is the rendered wire body
        # (tools → system → messages), NOT the HTTP headers — they do not affect
        # prompt-cache hits.
        #
        # Vision guard: a top-level ``ImageBlock`` (user/assistant content)
        # bound for a non-vision model is a loud misroute — refuse before wire
        # assembly (same stance as the Responses adapter). Tool-result images
        # ride ``ToolResultBlock.images`` (not a top-level block), so the guard
        # does not see them — their non-vision degrade happens in the tool
        # renderer instead (fall back to string content).
        _guard_vision_capability(request)
        body = self._build_request_body(request)
        # ② error recovery: translate every wire-shape failure
        # to the neutral Noeta error taxonomy here so the runtime never sees
        # an httpx type. Connection / timeout → transient; HTTP status →
        # bucketed by ``_translate_http_error``.
        post_kwargs: dict[str, Any] = {"json": body}
        if request_headers is not None:
            post_kwargs["headers"] = request_headers
        try:
            http_response = self._client.post(_MESSAGES_ENDPOINT, **post_kwargs)
            http_response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _translate_http_error(exc) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(str(exc)) from exc
        try:
            payload = http_response.json()
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Anthropic response was not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(
                f"Anthropic response root was not a JSON object: "
                f"type={type(payload).__name__}"
            )
        return self._parse_response(payload)

    # ------------------------------------------------------------------
    # Outbound translation (Noeta → Anthropic)
    # ------------------------------------------------------------------

    def _build_request_body(self, request: LLMRequest) -> dict[str, Any]:
        max_tokens = self._resolve_max_tokens(request)
        # Vision flag computed once per request (catalog lookup): user/assistant
        # images are already gated by ``_guard_vision_capability``; this flag is
        # what the tool renderer consults to either inline tool-result images
        # (vision) or degrade them to string content (non-vision).
        vision = _model_supports_vision(request.model)

        # D4: host-injected turns (``origin`` system / memory)
        # render as <system-reminder>-wrapped text and MERGE into the
        # adjacent user wire turn (Anthropic has no mid-history system
        # role). Only pairs touching an injected turn merge — plain
        # consecutive user turns keep their legacy 1:1 rendering.
        outbound_messages: list[dict[str, Any]] = []
        prev_injected = False
        for message in request.messages:
            if message.role == "system":
                raise ValueError(
                    "system must use LLMRequest.system field, not messages array"
                )
            wire = _message_to_anthropic(message, self._image_resolver, vision)
            injected = _is_host_injected(message)
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
        # Prompt caching (#4): stamp ephemeral cache_control breakpoints on the
        # outbound wire body ONLY. cache_control is an Anthropic wire concern —
        # it must never reach LLMRequest / request_ref (the recorded bytes stay
        # provider-neutral and unchanged). Breakpoints mark the END of a cached
        # prefix; placing one on the last tool caches the (large) system+tools
        # prefix, and one on the last message caches the growing conversation.
        # Anthropic allows up to 4 breakpoints; we use 2-3.
        _apply_cache_control(body)
        if request.temperature is not None:
            body["temperature"] = request.temperature
        # Structured output (GA): JSON Schema pinned via output_config.
        if request.output_schema is not None:
            body["output_config"] = {
                **body.get("output_config", {}),
                "format": {
                    "type": "json_schema",
                    "schema": dict(request.output_schema),
                },
            }
        # Reasoning effort: carried inside output_config (overwrites any
        # existing output_config entries set above.
        if request.effort is not None:
            body.setdefault("output_config", {})["effort"] = request.effort
        # Top-level thinking mode ("adaptive" / "disabled") — a sibling of
        # output_config, not nested.
        if request.thinking is not None:
            body["thinking"] = {"type": request.thinking}
        return body

    def _resolve_max_tokens(self, request: LLMRequest) -> int:
        """rev2 B4: explicit request > explicit default > fail-fast."""
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

        return LLMResponse(
            stop_reason=stop_reason,
            content=content,
            usage=usage,
            raw=payload,
        )


# ---------------------------------------------------------------------------
# Error translation (② error recovery, provider-neutral)
# ---------------------------------------------------------------------------

#: Substrings in an Anthropic ``invalid_request_error`` message that signal
#: the prompt exceeded the context window (→ ContextOverflowError).
#: Each marker must be a phrase that *only* an input-context overflow emits.
#: The over-broad ``"max tokens"`` / ``"too many tokens"`` are deliberately
#: excluded: they can appear in non-overflow 400s (e.g. an output-cap
#: validation message) that compaction can't fix, and they are redundant —
#: the real overflow body is ``"prompt is too long: N tokens > M maximum"``,
#: already caught by the tight phrasings above.
_OVERFLOW_MESSAGE_MARKERS: tuple[str, ...] = (
    "prompt is too long",
    "prompt too long",
    "context window",
    "maximum context",
)


def _translate_http_error(exc: httpx.HTTPStatusError) -> Exception:
    """Map an Anthropic-shape HTTP status error into the neutral taxonomy.

    * 429 → :class:`TransientError` (reads ``retry-after``).
    * 529 (overloaded) / 5xx → :class:`TransientError`.
    * 400 ``invalid_request_error`` whose message mentions the context
      window / prompt-too-long → :class:`ContextOverflowError`.
    * other 4xx (400 / 401 / 403 / ...) → :class:`FatalError`.

    Anthropic error body: ``{"type": "error", "error": {"type", "message"}}``.
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


# ---------------------------------------------------------------------------
# Usage translation (foundation A, provider-neutral)
# ---------------------------------------------------------------------------


def _translate_usage(usage_raw: Any) -> Usage:
    """Map Anthropic's usage wire shape into Noeta-shape :class:`Usage`.

    Anthropic reports cache detail and — critically — its
    ``input_tokens`` is the **uncached** portion only (cache reads /
    writes are billed separately and counted in their own fields). So:

      * ``input_tokens``               → ``uncached``
      * ``cache_read_input_tokens``    → ``cache_read``
      * ``cache_creation_input_tokens``→ ``cache_write``
      * ``output_tokens``              → ``output``

    The derived ``Usage.input`` then sums to the *total* input
    (uncached + cache read + cache write), satisfying D-A5. Anthropic
    has no reasoning-token field today, so ``reasoning_tokens`` stays 0.
    A missing / non-dict ``usage`` yields an empty ``Usage()`` rather
    than raising.
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


def _model_supports_vision(model: str) -> bool:
    """Whether ``model`` is catalogued as vision-capable.

    Resolve a friendly alias first, then look the real id up in
    ``catalog.CATALOG``; an unregistered model (or one with
    ``supports_vision`` False) is treated as **not** vision-capable — the
    conservative default keeps images away from a model that may not read them.
    """
    spec = catalog.CATALOG.get(catalog.resolve_alias(model))
    return bool(spec is not None and spec.supports_vision)


def _request_has_image(request: LLMRequest) -> bool:
    """True if any ``Message`` carries a **top-level** ``ImageBlock`` (scans
    every position, not just the last turn).

    Tool-result images ride ``ToolResultBlock.images`` (nested, not a top-level
    block), so they are deliberately invisible here — their non-vision degrade
    is the tool renderer's job, not the guard's.
    """
    return any(
        isinstance(block, ImageBlock)
        for message in request.messages
        for block in message.content
    )


def _guard_vision_capability(request: LLMRequest) -> None:
    """A top-level ``ImageBlock`` bound for a non-vision model → :class:`FatalError`
    before going on the wire.

    Mirrors the Responses adapter: don't send an image to a model that cannot
    read it. The text-only / tool-only path hits the catalog only when an image
    is actually present, so it carries zero overhead and zero behavior change.
    """
    if not _request_has_image(request):
        return
    if _model_supports_vision(request.model):
        return
    raise FatalError(
        f"request carries an ImageBlock but model {request.model!r} is not "
        "vision-capable (catalog supports_vision is False or model is "
        "unregistered); refusing to send the image to a model that cannot "
        "read it."
    )


def _image_block_to_anthropic(
    block: ImageBlock, image_resolver: Optional[ImageResolver]
) -> dict[str, Any]:
    """``ImageBlock(ContentRef)`` → an Anthropic base64 ``image`` content block.

    deref+base64 happens only at wire-assembly time, is transient, and is never
    written back to the ledger / ContentStore (red line). A missing
    ``image_resolver`` (None) means incomplete config → error explicitly rather
    than silently dropping the image (mirrors the Responses adapter).
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


def _is_host_injected(message: Message) -> bool:
    """D4: a user-channel turn authored by the host (system-side
    injection or memory recall) rather than the human. ``human`` / ``None``
    mean the role's natural author — rendered as a plain user turn."""
    return message.role == "user" and message.origin in ("system", "memory")


def _wrap_system_reminder(text: str) -> str:
    """Anthropic-only tag syntax — exists ONLY in this adapter (the tag
    never enters the ledger; provider-neutral per D4)."""
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
    """rev2 B3: ``ToolResultBlock`` is forbidden inside ``role='user'``;
    use ``role='tool'`` instead. ``ImageBlock`` is translated to an Anthropic
    base64 ``image`` block (the vision guard already rejected the non-vision
    misroute upstream). Other non-text blocks (Thinking / ToolUse) are skipped
    silently — they don't normally appear in user history, and silent tolerance
    matches OpenAICompatProvider's approach."""
    wrap = _is_host_injected(message)
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
        # ThinkingBlock / ToolUseBlock in user message: silently skipped
    return {"role": "user", "content": blocks}


def _assistant_message_to_anthropic(
    message: Message, image_resolver: Optional[ImageResolver]
) -> dict[str, Any]:
    """rev2 B2: regroup ``Message.content`` blocks into Anthropic's
    required-ish order ``thinking* / text* / image* / tool_use*``. Stable sort
    within each group preserves caller's relative ordering — useful
    for multi-thinking or multi-tool-use cases. ``ImageBlock`` translates to an
    Anthropic base64 ``image`` block (placed after text, before tool_use; the
    vision guard already rejected the non-vision misroute upstream).
    ``ToolResultBlock`` in an assistant message is silently skipped (caller bug,
    matches OpenAICompatProvider tolerance)."""
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
        # ToolResultBlock silently skipped
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
    """rev2 B3: ``role='tool'`` becomes one Anthropic user message whose
    content is **only** ``tool_result`` blocks (in input order).
    Non-ToolResultBlock content raises — the strict placement keeps
    Anthropic's "tool_use must be followed by tool_result user turn"
    wire-shape invariant intact."""
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
    array. With no images (or a non-vision model) this returns the historical
    **string** (JSON-encode non-string outputs; ``error`` prefixed per rev1 Q10),
    keeping the text-only path byte-identical. When the block carries ``images``
    AND the model is vision-capable, it returns an **array**: one ``text`` block
    holding that same string, followed by one base64 ``image`` block per image
    (deref'd via ``image_resolver``)."""
    text = _tool_result_text(block)
    if vision and block.images:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for image in block.images:
            content.append(_image_block_to_anthropic(image, image_resolver))
        return content
    return text


def _tool_result_text(block: ToolResultBlock) -> str:
    """The string rendering of ``ToolResultBlock.output``: JSON-encode non-string
    outputs; prefix an ``error`` string (rev1 Q10) so Noeta's two-field
    success/error split survives Anthropic's one-field tool_result body."""
    output = block.output
    if isinstance(output, str):
        body = output
    else:
        body = json.dumps(output)
    if block.error:
        return f"[error] {block.error}\n{body}"
    return body


#: The single ephemeral cache breakpoint marker reused on every stamp site.
#: Default TTL (5 min); no extended-TTL flag (out of scope, spec Non-goals).
_CACHE_CONTROL_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}


def _apply_cache_control(body: dict[str, Any]) -> None:
    """Stamp ephemeral prompt-cache breakpoints onto the outbound wire body.

    Mutates ``body`` in place; the caller passes the just-built wire dict, so
    no LLMRequest / request_ref bytes are touched (#4). Breakpoints (≤4):

    * **system**: if present, lift the flat string into block form
      ``[{"type":"text","text":...,"cache_control":...}]`` — Anthropic requires
      block (not bare-string) shape to carry cache_control. Caches the system
      preamble.
    * **last tool**: stamp the final tool dict — caches the whole system+tools
      prefix (the bulk of the stable bytes).
    * **last message's last content block**: stamp the final content block —
      caches the growing conversation up to that point. Every wire content
      block is already a dict here, so it can carry the field directly.
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
    """Unpack OpenAI-shape tool dicts (Composer's current emit shape)
    into Anthropic-shape. rev2 NB3 widens defensive validation:
    ``function`` / ``name`` / ``parameters`` must all be present with
    the right types."""
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
    """Translate Anthropic response content blocks 1-to-1 into Noeta
    Block instances. Unknown block types are silently skipped to
    keep the adapter forward-compatible with future Anthropic schema
    additions; the missing block reaches the caller as a content-shape
    gap (one fewer block than the wire carried)."""
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
            # blob is missing / non-str there is nothing to round-trip: keeping a
            # ``ThinkingBlock(text="", data=None)`` would be re-emitted outbound
            # as an empty ``thinking`` block (the API rejects it), so drop it —
            # the same skip the pre-``data`` parser did for redacted blocks.
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
        # Unknown block types: silently skipped for forward compatibility
    return blocks
