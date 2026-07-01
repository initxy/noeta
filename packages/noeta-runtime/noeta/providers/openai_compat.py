"""OpenAI ``/chat/completions`` adapter for the Noeta-shape LLM protocol.

Implements :class:`noeta.protocols.messages.LLMProvider` against any
endpoint that speaks the OpenAI Chat Completions wire format —
official OpenAI, vLLM, OpenRouter, LiteLLM, and most middle-proxy
gateways. The translation contract is pinned: every loss of
fidelity caused by speaking OpenAI's wire shape is contained to this
single file; Engine / Policy only ever see Noeta-shape types.

Key contracts (cross-referenced to PRD §"OpenAICompatProvider translation rules"):

* The provider does **not** hold a model. ``LLMRequest.model`` is
  forwarded per-call so one instance can talk to multiple models.
* ``LLMRequest.system`` (when present) is flattened to a single
  ``{"role": "system", "content": str}`` message prepended to the
  outbound array; a ``role=="system"`` message inside
  ``LLMRequest.messages`` is rejected with :class:`ValueError` because
  the canonical place for system instructions is the dedicated field.
* :class:`noeta.protocols.messages.ThinkingBlock` round-trips outbound
  via the ``reasoning_content`` (text) and ``encrypted_reasoning``
  (signature) fields — but **only when ``reasoning_continuation`` is not
  ``"off"``** (the default). The ContextComposer re-attaches thinking
  neutrally for every provider; this adapter is the single place that
  decides whether OpenAI's wire actually carries it, because native
  OpenAI hides reasoning and DeepSeek-style gateways *reject* an echoed
  ``reasoning_content`` (HTTP 400). Inbound recognises any of
  ``reasoning_content`` / ``reasoning`` / ``encrypted_reasoning``
  (the first match wins) so different middle-proxy implementations
  stay supported.
* Inconsistent responses (``finish_reason="stop"`` together with
  non-empty ``tool_calls``, or ``finish_reason="tool_calls"`` with
  no calls) raise :class:`ValueError` — the wrapping
  ``RuntimeLLMClient`` (issue 12) is what translates exceptions into
  ``LLMResponse(stop_reason="error", ...)``.

This module does **not** implement async, streaming, retry, prompt
caching, or the Anthropic protocol — those are explicitly Out of
Scope for issue 11.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

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
from noeta.providers.codecs import (
    decode_tool_arguments,
    encode_tool_arguments,
    parse_retry_after,
)


_FINISH_REASON_MAP: dict[str, Literal["tool_use", "end_turn", "max_tokens", "error"]] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
}

_REASONING_FIELDS: tuple[str, ...] = (
    "reasoning_content",
    "reasoning",
    "encrypted_reasoning",
)

#: Outbound reasoning-echo policy (extended-thinking alignment, Slice A).
#: Decides whether an assistant ``ThinkingBlock`` re-attached upstream by the
#: ContextComposer is written back onto the wire. ``"off"`` (default) drops it:
#: native OpenAI hides reasoning and DeepSeek-style gateways reject an echoed
#: ``reasoning_content`` (HTTP 400), so a neutral composer that always carries
#: thinking forward must not leak it here. ``"chat"`` echoes
#: ``reasoning_content`` + ``encrypted_reasoning`` for gateways that round-trip
#: reasoning through Chat Completions; ``"responses"`` is reserved for the
#: OpenAI Responses-API encrypted-continuation shape (treated like ``"chat"``
#: by this Chat-Completions adapter until a dedicated Responses adapter lands).
ReasoningContinuation = Literal["off", "chat", "responses"]


class OpenAICompatProvider:
    """Adapter for OpenAI-style ``/chat/completions`` endpoints.

    Construct once with the endpoint base URL and credentials and reuse
    across calls — the underlying :class:`httpx.Client` is shared, and
    ``LLMRequest.model`` selects the model per call. ``extra_headers``
    is the escape hatch for proxy-specific auth (e.g. a vendor key
    next to the OpenAI ``Authorization`` header).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = 60.0,
        extra_headers: Optional[dict[str, str]] = None,
        reasoning_continuation: ReasoningContinuation = "off",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._reasoning_continuation = reasoning_continuation
        headers: dict[str, str] = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout_seconds,
        )

    # ------------------------------------------------------------------
    # LLMProvider Protocol
    # ------------------------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        body = self._build_request_body(request)
        # ② error recovery: every wire-shape failure is
        # translated to the neutral Noeta error taxonomy *here* so the
        # runtime never sees an httpx type. Connection / timeout errors are
        # transient (worth a retry); HTTP status errors are bucketed by
        # ``_translate_http_error``.
        try:
            http_response = self._client.post("/chat/completions", json=body)
            http_response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _translate_http_error(exc) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(str(exc)) from exc
        try:
            payload = http_response.json()
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"OpenAI response was not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(
                f"OpenAI response root was not a JSON object: type={type(payload).__name__}"
            )
        return self._parse_response(payload)

    # ------------------------------------------------------------------
    # Outbound translation (Noeta → OpenAI)
    # ------------------------------------------------------------------

    def _build_request_body(self, request: LLMRequest) -> dict[str, Any]:
        outbound_messages: list[dict[str, Any]] = []
        if request.system is not None:
            outbound_messages.append(_system_message_to_openai(request.system))
        for message in request.messages:
            if message.role == "system":
                raise ValueError(
                    "system must use LLMRequest.system field, not messages array"
                )
            outbound_messages.extend(
                _message_to_openai(message, self._reasoning_continuation)
            )

        body: dict[str, Any] = {
            "model": request.model,
            "messages": outbound_messages,
        }
        if request.tools:
            body["tools"] = request.tools
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        # Structured output: OpenAI-style response_format with json_schema.
        if request.output_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "noeta_output",
                    "schema": dict(request.output_schema),
                },
            }
        # Reasoning effort. OpenAI only supports low/medium/high; the Noeta
        # xhigh/max values are collapsed to "high" because the vendor has
        # no finer buckets.
        if request.effort is not None:
            body["reasoning_effort"] = {
                "xhigh": "high",
                "max": "high",
            }.get(request.effort, request.effort)
        # ``thinking`` has no OpenAI-compat equivalent; silently ignored.
        return body

    # ------------------------------------------------------------------
    # Inbound translation (OpenAI → Noeta)
    # ------------------------------------------------------------------

    def _parse_response(self, payload: dict[str, Any]) -> LLMResponse:
        choices = payload.get("choices")
        if not choices:
            raise ValueError(
                "OpenAI response missing 'choices' field or it was empty"
            )
        choice = choices[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason")
        tool_calls = message.get("tool_calls") or []

        if finish_reason == "stop" and tool_calls:
            raise ValueError(
                "inconsistent OpenAI response: finish_reason='stop' but "
                f"tool_calls has {len(tool_calls)} entries"
            )
        if finish_reason == "tool_calls" and not tool_calls:
            raise ValueError(
                "inconsistent OpenAI response: finish_reason='tool_calls' but "
                "tool_calls was empty/null"
            )

        stop_reason: Literal[
            "tool_use", "end_turn", "max_tokens", "error"
        ] = _FINISH_REASON_MAP.get(finish_reason or "", "error")

        content: list[Block] = []
        thinking = _extract_thinking(message)
        if thinking is not None:
            content.append(thinking)
        text_value = message.get("content")
        if isinstance(text_value, str) and text_value:
            content.append(TextBlock(text=text_value))
        for call in tool_calls:
            function = call.get("function") or {}
            # Treating an empty string as default (``or "{}"``) is this
            # adapter's local reading convention; after normalizing, the shared
            # codec decodes it. The error prefix ``tool_call arguments`` is this
            # provider's wire vocabulary,
            # passed through verbatim so the wording bytes stay unchanged.
            arguments = decode_tool_arguments(
                function.get("arguments") or "{}",
                error_label="tool_call arguments",
            )
            content.append(
                ToolUseBlock(
                    call_id=call.get("id", ""),
                    tool_name=function.get("name", ""),
                    arguments=arguments,
                )
            )

        usage = _translate_usage(payload.get("usage") or {})
        return LLMResponse(
            stop_reason=stop_reason,
            content=content,
            usage=usage,
            raw=payload,
        )


# ---------------------------------------------------------------------------
# Error translation (② error recovery, provider-neutral)
# ---------------------------------------------------------------------------


def _translate_http_error(exc: httpx.HTTPStatusError) -> Exception:
    """Map an OpenAI-shape HTTP status error into the neutral taxonomy.

    * 429 → :class:`TransientError` (reads ``Retry-After``).
    * 5xx → :class:`TransientError`.
    * 400 with ``error.code/type == 'context_length_exceeded'`` (or a
      message mentioning the maximum context length) →
      :class:`ContextOverflowError`.
    * other 4xx (400 / 401 / 403 / ...) → :class:`FatalError`.

    OpenAI error body shape: ``{"error": {"message", "type", "code"}}``.
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
    code = str(error.get("code") or "")
    err_type = str(error.get("type") or "")
    message = str(error.get("message") or "").lower()
    return (
        code == "context_length_exceeded"
        or err_type == "context_length_exceeded"
        or "maximum context length" in message
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: D6: this Chat Completions
#: adapter does NOT support image input. ``ImageBlock`` should only flow to a
#: vision-capable Responses provider. An image task misrouted here raises
#: explicitly and never silently drops the image (one task is pinned to one
#: provider, so this only fires on a misroute — and a misroute must be loud).
_NO_IMAGE_SUPPORT = (
    "this provider does not support image input (ImageBlock); "
    "route image tasks to a vision-capable Responses provider instead of "
    "silently dropping the image."
)


def _reject_image_block(block: Block) -> None:
    """Raise :class:`ValueError` on any ``ImageBlock`` (D6 defensive branch)."""
    if isinstance(block, ImageBlock):
        raise ValueError(_NO_IMAGE_SUPPORT)


def _flatten_text_blocks(message: Message) -> str:
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
    return "\n".join(parts)


def _system_message_to_openai(system: Message) -> dict[str, Any]:
    return {"role": "system", "content": _flatten_text_blocks(system)}


def _message_to_openai(
    message: Message, reasoning_continuation: ReasoningContinuation
) -> list[dict[str, Any]]:
    if message.role == "user":
        # D6: an ImageBlock in
        # a user turn raises explicitly. ``_flatten_text_blocks`` keeps only
        # TextBlock, so an image would be silently dropped — scan first to make
        # the misroute loud.
        for block in message.content:
            _reject_image_block(block)
        # D4: host-injected turns (``origin`` system / memory)
        # ride the user channel in the ledger but render as a mid-history
        # ``system`` role wire message — OpenAI's chat shape supports
        # that natively, so no tag syntax is needed. ``human`` / ``None``
        # mean the role's natural author → plain user turn.
        if message.origin in ("system", "memory"):
            return [
                {"role": "system", "content": _flatten_text_blocks(message)}
            ]
        return [{"role": "user", "content": _flatten_text_blocks(message)}]
    if message.role == "assistant":
        return [_assistant_message_to_openai(message, reasoning_continuation)]
    if message.role == "tool":
        return _tool_message_to_openai(message)
    raise ValueError(f"unsupported message role: {message.role!r}")


def _assistant_message_to_openai(
    message: Message, reasoning_continuation: ReasoningContinuation
) -> dict[str, Any]:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    signature: Optional[str] = None
    tool_calls: list[dict[str, Any]] = []
    for block in message.content:
        _reject_image_block(block)  # D6: no images; misroute must be loud
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ThinkingBlock):
            thinking_parts.append(block.text)
            if block.signature is not None:
                signature = block.signature
        elif isinstance(block, ToolUseBlock):
            tool_calls.append(
                {
                    "id": block.call_id,
                    "type": "function",
                    "function": {
                        "name": block.tool_name,
                        "arguments": encode_tool_arguments(block.arguments),
                    },
                }
            )
        # ToolResultBlock has no place in an assistant message; ignore silently
        # to match OpenAI's tolerance (it would be a misuse upstream).
    out: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(text_parts) if text_parts else None,
    }
    # Outbound reasoning echo is gated: the ContextComposer re-attaches
    # thinking neutrally for every provider, but only gateways that actually
    # accept it should see it on the wire. ``off`` (default) drops both fields
    # so native OpenAI / DeepSeek never receive an echoed ``reasoning_content``
    # (which DeepSeek rejects with HTTP 400).
    if reasoning_continuation != "off":
        if thinking_parts:
            out["reasoning_content"] = "\n".join(thinking_parts)
        if signature is not None:
            out["encrypted_reasoning"] = signature
    if tool_calls:
        out["tool_calls"] = tool_calls
    return out


def _tool_message_to_openai(message: Message) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for block in message.content:
        _reject_image_block(block)  # D6: no images; misroute must be loud
        if not isinstance(block, ToolResultBlock):
            continue
        output = block.output
        content = output if isinstance(output, str) else json.dumps(output)
        expanded.append(
            {
                "role": "tool",
                "tool_call_id": block.call_id,
                "content": content,
            }
        )
    return expanded


def _extract_thinking(message: dict[str, Any]) -> Optional[ThinkingBlock]:
    text_field = next(
        (k for k in _REASONING_FIELDS if k in message and k != "encrypted_reasoning"),
        None,
    )
    text_value = message.get(text_field) if text_field else None
    signature_value = message.get("encrypted_reasoning")
    if not isinstance(text_value, str) or not text_value:
        if isinstance(signature_value, str) and signature_value:
            return ThinkingBlock(text="", signature=signature_value)
        return None
    return ThinkingBlock(
        text=text_value,
        signature=signature_value if isinstance(signature_value, str) else None,
    )


def _translate_usage(usage: Any) -> Usage:
    """Map OpenAI's usage wire shape into Noeta-shape :class:`Usage`.

    OpenAI reports a flat total with no cache breakdown:

      * ``prompt_tokens``     → ``uncached`` (cache_read / cache_write 0,
        so the derived ``Usage.input`` equals ``prompt_tokens``)
      * ``completion_tokens`` → ``output``
      * ``completion_tokens_details.reasoning_tokens`` →
        ``reasoning_tokens`` (newer reasoning models; absent → 0, D-A5)

    ``total_tokens`` is a redundant provider-side field — the derived
    ``input`` recomputes the total — so it is dropped, not pinned into
    the internal contract. A missing / non-dict ``usage``
    yields an empty ``Usage()``.
    """
    if not isinstance(usage, dict):
        return Usage()
    details = usage.get("completion_tokens_details")
    reasoning = 0
    if isinstance(details, dict):
        reasoning = int(details.get("reasoning_tokens", 0) or 0)
    return Usage(
        uncached=int(usage.get("prompt_tokens", 0) or 0),
        output=int(usage.get("completion_tokens", 0) or 0),
        reasoning_tokens=reasoning,
    )
