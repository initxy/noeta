"""OpenAI ``/chat/completions`` adapter for the Noeta-shape LLM protocol.

Implements :class:`noeta.protocols.messages.LLMProvider` against any endpoint
speaking the OpenAI Chat Completions wire format — official OpenAI, vLLM,
OpenRouter, LiteLLM, most middle-proxy gateways. Every loss of fidelity caused
by that wire shape is contained to this file, so Engine and Policy only ever
see Noeta-shape types. The provider pins no model: ``LLMRequest.model`` is
forwarded per call, which is what lets one instance serve many models. Wire
inconsistencies (a ``finish_reason`` that contradicts the ``tool_calls``
array) raise rather than being papered over — ``RuntimeLLMClient`` is what
turns an exception into ``stop_reason="error"``.
"""

from __future__ import annotations

import json
import os
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
    is_host_injected,
)
from noeta.builtins.providers.impl._sse import iter_sse_events
from noeta.builtins.providers.impl.codecs import (
    HOST_INJECTED_PREAMBLE,
    decode_tool_arguments,
    encode_tool_arguments,
    parse_retry_after,
    render_tool_result_body,
)


_API_KEY_ENV = "OPENAI_API_KEY"

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

#: Whether an assistant ``ThinkingBlock`` that the ContextComposer re-attached
#: is written back onto the wire. ``"off"`` (the default) drops it, because
#: native OpenAI hides reasoning and DeepSeek-style gateways reject an echoed
#: ``reasoning_content`` with HTTP 400 — a composer that neutrally carries
#: thinking forward for every provider must not leak it here. ``"chat"`` echoes
#: it for gateways that do round-trip reasoning; ``"responses"`` exists for
#: symmetry with the Responses adapter and behaves like ``"chat"`` here.
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
        api_key: Optional[str] = None,
        *,
        timeout_seconds: float = 60.0,
        extra_headers: Optional[dict[str, str]] = None,
        reasoning_continuation: ReasoningContinuation = "off",
    ) -> None:
        """``api_key`` defaults to the ``OPENAI_API_KEY`` environment variable.

        Missing credentials raise here rather than letting the first call fail
        with an opaque 401. ``base_url`` has no default because an
        OpenAI-*compatible* endpoint has no conventional one.

        An **empty** key counts as absent: the usual way to arrive at one is
        ``os.environ.get("KEY", "")``, the very misconfiguration this check
        exists to name. An endpoint that wants no authentication passes any
        placeholder — the header is sent and the server ignores it.
        """
        resolved_key = api_key if api_key is not None else os.environ.get(_API_KEY_ENV)
        if not resolved_key:
            raise ValueError(
                "OpenAICompatProvider needs an API key: pass api_key=... or "
                f"set the {_API_KEY_ENV} environment variable"
            )
        self._base_url = base_url.rstrip("/")
        self._reasoning_continuation = reasoning_continuation
        headers: dict[str, str] = {
            "Authorization": f"Bearer {resolved_key}",
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
        # Every wire-shape failure is translated into the neutral Noeta error
        # taxonomy here, so the runtime never sees an httpx type.
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
    # StreamingProvider capability
    # ------------------------------------------------------------------

    def complete_streaming(
        self,
        request: LLMRequest,
        on_delta: Callable[[StreamDelta], None],
        request_headers: Optional[dict[str, str]] = None,
    ) -> LLMResponse:
        """Stream a Chat Completions request, firing ``on_delta`` per fragment.

        Still the blocking one-shot contract of :meth:`complete`: the full
        :class:`LLMResponse` is the return value, rebuilt from the fragments
        and fed through the same ``_parse_response``, which is what pins
        streamed and batch results shape-identical. Tool-call fragments
        accumulate without firing deltas — partial argument JSON is
        undecodable. ``request_headers`` are transport-only and never
        recorded.
        """
        body = self._build_request_body(request)
        body["stream"] = True
        # Requests the terminal usage-only chunk. Some OpenAI-compatible
        # gateways never send it; a missing usage object degrades to an empty
        # ``Usage`` exactly like batch, without erroring.
        body["stream_options"] = {"include_usage": True}
        accumulator = _ChatStreamAccumulator(on_delta)
        try:
            with self._client.stream(
                "POST",
                "/chat/completions",
                json=body,
                headers=request_headers,
            ) as http_response:
                if http_response.status_code >= 400:
                    # Read the (small) error body while the stream is open, so
                    # ``_is_context_overflow`` can still see the JSON.
                    http_response.read()
                    try:
                        http_response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise _translate_http_error(exc) from exc
                for _event, data in iter_sse_events(http_response.iter_lines()):
                    if data.strip() == "[DONE]":
                        accumulator.saw_done = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        # Skip a malformed chunk, mirroring the batch parser's
                        # tolerance of unknown shapes.
                        continue
                    if isinstance(chunk, dict):
                        accumulator.feed(chunk)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(str(exc)) from exc
        if not accumulator.saw_done and not accumulator.has_usable_content:
            raise TransientError(
                "OpenAI stream ended without [DONE] and produced no content"
            )
        return self._parse_response(accumulator.terminal_payload())

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
        # ``LLMRequest`` has no tool_choice field — it rides ``metadata``, same
        # pass-through as the Responses adapter (the summarize round-trip sets
        # ``"none"`` so the summarizer cannot answer with a tool call). The
        # neutral bare-string spelling is already the OpenAI wire shape.
        tool_choice = request.metadata.get("tool_choice")
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.output_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "noeta_output",
                    "schema": dict(request.output_schema),
                },
            }
        # OpenAI has no bucket above "high", so the Noeta xhigh/max values
        # collapse onto it.
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
            # Reading an empty arguments string as ``{}`` is this adapter's own
            # convention (the shared codec only defaults ``None``), and
            # ``tool_call arguments`` is this adapter's own error vocabulary.
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
# Streaming accumulation (Chat Completions chunks → batch wire shape)
# ---------------------------------------------------------------------------


class _ChatStreamAccumulator:
    """Folds Chat Completions stream chunks back into the batch wire shape.

    Rebuilding the vendor-shaped payload — rather than assembling an
    :class:`LLMResponse` directly — is what lets ``_parse_response`` run
    unchanged, so a streamed response gets the same consistency checks, the
    same tool-call codec and the same usage degrade as a batch one.

    Delta block indexes are assigned in arrival order. Reasoning models emit
    every reasoning fragment before ``content``, so the indexes line up with
    the final response's block positions; either way the two kinds always get
    distinct, ordered indexes.
    """

    def __init__(self, on_delta: Callable[[StreamDelta], None]) -> None:
        self._on_delta = on_delta
        self._text_parts: list[str] = []
        self._reasoning_parts: dict[str, list[str]] = {}
        self._signature_parts: list[str] = []
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._finish_reason: Optional[str] = None
        self._usage: Optional[dict[str, Any]] = None
        self._meta: dict[str, Any] = {}
        self._text_index: Optional[int] = None
        self._thinking_index: Optional[int] = None
        self._next_index = 0
        self.saw_done = False

    @property
    def has_usable_content(self) -> bool:
        return bool(
            self._finish_reason is not None
            or self._text_parts
            or self._reasoning_parts
            or self._signature_parts
            or self._tool_calls
        )

    def feed(self, chunk: dict[str, Any]) -> None:
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._usage = usage
        # First-seen wins, so the rebuilt ``raw`` payload stays vendor-shaped.
        for key in ("id", "model"):
            value = chunk.get(key)
            if isinstance(value, str) and value and key not in self._meta:
                self._meta[key] = value
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return  # usage-only terminal chunk (or unknown shape): no delta
        choice = choices[0]
        if not isinstance(choice, dict):
            return
        finish_reason = choice.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            self._finish_reason = finish_reason
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return
        text = delta.get("content")
        if isinstance(text, str) and text:
            self._text_parts.append(text)
            self._on_delta(
                StreamDelta(kind="text", text=text, index=self._block_index("text"))
            )
        for field_name in ("reasoning_content", "reasoning"):
            fragment = delta.get(field_name)
            if isinstance(fragment, str) and fragment:
                self._reasoning_parts.setdefault(field_name, []).append(fragment)
                self._on_delta(
                    StreamDelta(
                        kind="thinking",
                        text=fragment,
                        index=self._block_index("thinking"),
                    )
                )
        # A signature, not visible reasoning text, so it never surfaces as a
        # delta.
        signature = delta.get("encrypted_reasoning")
        if isinstance(signature, str) and signature:
            self._signature_parts.append(signature)
        for fragment in delta.get("tool_calls") or []:
            if isinstance(fragment, dict):
                self._feed_tool_call(fragment)

    def _block_index(self, kind: Literal["text", "thinking"]) -> int:
        if kind == "text":
            if self._text_index is None:
                self._text_index = self._next_index
                self._next_index += 1
            return self._text_index
        if self._thinking_index is None:
            self._thinking_index = self._next_index
            self._next_index += 1
        return self._thinking_index

    def _feed_tool_call(self, fragment: dict[str, Any]) -> None:
        index = fragment.get("index")
        if not isinstance(index, int):
            index = 0
        entry = self._tool_calls.setdefault(
            index, {"id": "", "name": "", "arguments": []}
        )
        call_id = fragment.get("id")
        if isinstance(call_id, str) and call_id:
            entry["id"] = call_id
        function = fragment.get("function")
        if not isinstance(function, dict):
            return
        name = function.get("name")
        if isinstance(name, str) and name:
            entry["name"] = name
        arguments = function.get("arguments")
        if isinstance(arguments, str) and arguments:
            entry["arguments"].append(arguments)

    def terminal_payload(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            # Batch tool-call responses carry ``content: null``; mirror that
            # when no text arrived so ``_parse_response`` sees the same shape.
            "content": "".join(self._text_parts) if self._text_parts else None,
        }
        for field_name, parts in self._reasoning_parts.items():
            message[field_name] = "".join(parts)
        if self._signature_parts:
            message["encrypted_reasoning"] = "".join(self._signature_parts)
        tool_calls = [
            {
                "id": entry["id"],
                "type": "function",
                "function": {
                    "name": entry["name"],
                    "arguments": "".join(entry["arguments"]),
                },
            }
            for _, entry in sorted(self._tool_calls.items())
        ]
        if tool_calls:
            message["tool_calls"] = tool_calls
        payload: dict[str, Any] = dict(self._meta)
        payload["choices"] = [
            {
                "index": 0,
                "message": message,
                "finish_reason": self._finish_reason,
            }
        ]
        if self._usage is not None:
            payload["usage"] = self._usage
        return payload


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def _translate_http_error(exc: httpx.HTTPStatusError) -> Exception:
    """Map an OpenAI-shape HTTP status error into the neutral taxonomy.

    429 and 5xx are retryable; every other 4xx is fatal. A 400 gets its body
    sniffed first, because context overflow arrives as an ordinary bad request
    yet needs its own bucket — it is not retryable and the recovery a Policy
    drives is compaction.
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


#: This adapter carries no image input; an ``ImageBlock`` belongs on a
#: vision-capable Responses provider. One task is pinned to one provider, so
#: an image arriving here can only be a misroute — and a misroute must be
#: loud, never a silently dropped image.
_NO_IMAGE_SUPPORT = (
    "this provider does not support image input (ImageBlock); "
    "route image tasks to a vision-capable Responses provider instead of "
    "silently dropping the image."
)


def _reject_image_block(block: Block) -> None:
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
        # ``_flatten_text_blocks`` keeps only TextBlock, so an image would
        # vanish without a trace — scan first to make the misroute loud.
        for block in message.content:
            _reject_image_block(block)
        # Host-injected turns ride the user channel in the ledger but render as
        # a mid-history ``system`` wire message, which OpenAI's chat shape
        # supports natively. The role alone does not reliably tell an arbitrary
        # model "this is not the user speaking" — mid-history system turns get
        # answered as if addressed — so the preamble states it in-band.
        # ``human`` / ``None`` mean the role's natural author, i.e. a plain
        # user turn.
        if is_host_injected(message):
            return [
                {
                    "role": "system",
                    "content": (
                        f"{HOST_INJECTED_PREAMBLE}\n{_flatten_text_blocks(message)}"
                    ),
                }
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
        _reject_image_block(block)  # a misroute must be loud, not silent
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
        # A ToolResultBlock here is an upstream misuse; ignored silently to
        # match OpenAI's own tolerance.
    out: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(text_parts) if text_parts else None,
    }
    # The composer re-attaches thinking neutrally for every provider, so the
    # echo has to be gated here: only a gateway that accepts an echoed
    # ``reasoning_content`` should see one (DeepSeek answers HTTP 400).
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
        _reject_image_block(block)  # a misroute must be loud, not silent
        if not isinstance(block, ToolResultBlock):
            continue
        content = render_tool_result_body(block.output, block.error)
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

    ``prompt_tokens`` is a *total*, so the cached part is subtracted out to get
    ``uncached`` — the opposite of Anthropic, whose input count already
    excludes cache. ``cache_write`` stays 0 because Chat Completions never
    reports one, and ``total_tokens`` is dropped rather than pinned into the
    neutral contract: ``Usage.input`` recomputes it. A missing or non-dict
    ``usage`` degrades to an empty ``Usage()`` instead of raising.
    """
    if not isinstance(usage, dict):
        return Usage()
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    prompt_details = usage.get("prompt_tokens_details")
    cached = 0
    if isinstance(prompt_details, dict):
        cached = int(prompt_details.get("cached_tokens", 0) or 0)
    details = usage.get("completion_tokens_details")
    reasoning = 0
    if isinstance(details, dict):
        reasoning = int(details.get("reasoning_tokens", 0) or 0)
    return Usage(
        uncached=max(0, prompt_tokens - cached),
        cache_read=cached,
        cache_write=0,
        output=int(usage.get("completion_tokens", 0) or 0),
        reasoning_tokens=reasoning,
    )
