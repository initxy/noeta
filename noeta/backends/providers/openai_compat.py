"""OpenAI ``/chat/completions`` adapter.

Speaks to any endpoint using the Chat Completions wire format — official
OpenAI, vLLM, OpenRouter, LiteLLM, most middle-proxy gateways. The provider
pins no model: ``ModelRequest.model`` is forwarded per call. Wire
inconsistencies (``finish_reason="tool_calls"`` with no ``tool_calls``) raise
rather than being papered over. ``"stop"`` alongside tool calls is not one:
several compatible servers send it, and it reads as a tool-use stop.

Every request streams; the chunks are folded back into the batch wire shape
and parsed once, so streamed and non-streamed semantics cannot drift.
"""

from __future__ import annotations

import base64
import json
import os
import re
from contextlib import aclosing
from typing import Any, Literal

import httpx

from noeta.backends.providers._common import (
    HOST_INJECTED_PREAMBLE,
    decode_tool_arguments,
    encode_tool_arguments,
    flatten_text,
    guard_vision,
    is_host_injected,
    tool_result_images,
    tool_result_text,
    translate_http_error,
    transport_error,
)
from noeta.backends.providers._sse import iter_sse_events
from noeta.core import models
from noeta.core.errors import Fatal, MalformedToolArguments, Transient
from noeta.core.provider import OnDelta
from noeta.core.types import (
    Block,
    Delta,
    Image,
    Message,
    ModelRequest,
    ModelResponse,
    StopReason,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
    Usage,
)

__all__ = ["FAMILY", "MaxTokensParam", "OpenAICompatProvider", "ReasoningContinuation"]

#: ``Provider.family`` and ``Thinking.family`` for this adapter.
FAMILY = "openai_compat"

_API_KEY_ENV = "OPENAI_API_KEY"

_FINISH_REASON_MAP: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}

#: Whether an assistant ``Thinking`` block is written back onto the wire.
#: ``"off"`` (the default) drops it, because native OpenAI hides reasoning and
#: DeepSeek-style gateways reject an echoed ``reasoning_content`` with HTTP
#: 400. ``"chat"`` echoes it for gateways that do round-trip reasoning;
#: ``"responses"`` exists for symmetry with the Responses adapter and behaves
#: like ``"chat"`` here.
ReasoningContinuation = Literal["off", "chat", "responses"]

#: Which body key carries ``ModelRequest.max_tokens``. OpenAI's own API refuses
#: ``max_tokens`` for its reasoning models and wants ``max_completion_tokens``;
#: many OpenAI-compatible gateways still only read ``max_tokens``. ``"auto"``
#: picks per request (see :func:`_output_cap_key`); the literals force one.
MaxTokensParam = Literal["auto", "max_tokens", "max_completion_tokens"]

#: OpenAI reasoning-model ids, for a model the catalog does not know.
_OPENAI_REASONING_ID = re.compile(r"^(o1|o3|o4|gpt-5)")

#: OpenAI has no effort bucket above "high", so xhigh/max collapse onto it.
_EFFORT_MAP = {"xhigh": "high", "max": "high"}


class OpenAICompatProvider:
    """Adapter for OpenAI-style ``/chat/completions`` endpoints.

    Construct once with the endpoint base URL and credentials and reuse across
    calls. ``extra_headers`` is the escape hatch for proxy-specific auth;
    ``ModelRequest.headers`` merge over it per call.
    """

    family = FAMILY

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout_seconds: float = 300.0,
        extra_headers: dict[str, str] | None = None,
        reasoning_continuation: ReasoningContinuation = "off",
        max_tokens_param: MaxTokensParam = "auto",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """``api_key`` defaults to the ``OPENAI_API_KEY`` environment variable.
        An **empty** key counts as absent — the usual way to arrive at one is
        ``os.environ.get("KEY", "")``, the very misconfiguration this check
        exists to name; an endpoint that wants no authentication takes any
        placeholder. ``base_url`` has no default because an OpenAI-*compatible*
        endpoint has no conventional one.

        ``timeout_seconds`` measures silence between SSE events: a reasoning
        model can think for minutes before its first token, and a gateway that
        has gone quiet for five minutes is dead, not slow.
        """
        if max_tokens_param not in ("auto", "max_tokens", "max_completion_tokens"):
            raise ValueError(
                "max_tokens_param must be 'auto', 'max_tokens' or "
                f"'max_completion_tokens', got {max_tokens_param!r}"
            )
        resolved_key = api_key if api_key is not None else os.environ.get(_API_KEY_ENV)
        if not resolved_key:
            raise ValueError(
                "OpenAICompatProvider needs an API key: pass api_key=... or "
                f"set the {_API_KEY_ENV} environment variable"
            )
        self._reasoning_continuation = reasoning_continuation
        self._max_tokens_param: MaxTokensParam = max_tokens_param
        headers: dict[str, str] = {
            "Authorization": f"Bearer {resolved_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self, request: ModelRequest, on_delta: OnDelta | None = None
    ) -> ModelResponse:
        body = self.build_body(request)
        body["stream"] = True
        # Requests the terminal usage-only chunk. Some gateways never send it;
        # a missing usage object degrades to an empty ``Usage``.
        body["stream_options"] = {"include_usage": True}
        accumulator = _ChatStreamAccumulator(on_delta)
        try:
            async with self._client.stream(
                "POST",
                "/chat/completions",
                json=body,
                headers=request.headers or None,
            ) as response:
                if response.is_error:
                    await response.aread()
                    raise translate_http_error(response)
                async with aclosing(iter_sse_events(response.aiter_lines())) as events:
                    async for _event, data in events:
                        if data.strip() == "[DONE]":
                            accumulator.saw_done = True
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            # Skip a malformed chunk, mirroring the parser's
                            # tolerance of unknown shapes.
                            continue
                        if isinstance(chunk, dict):
                            accumulator.feed(chunk)
        except httpx.RequestError as exc:
            raise transport_error(exc) from exc
        # No ``[DONE]`` and no ``finish_reason``: the stream was cut
        # mid-generation, which is a dropped connection (retryable), not a
        # finished turn — whatever partial content arrived. A gateway that
        # omits ``[DONE]`` but sent a ``finish_reason`` finished normally.
        if not accumulator.saw_done and not accumulator.finished:
            raise Transient("OpenAI stream ended without [DONE] or a finish_reason")
        return parse_chat_payload(accumulator.terminal_payload(), request.model)

    # ------------------------------------------------------------------
    # Outbound translation
    # ------------------------------------------------------------------

    def build_body(self, request: ModelRequest) -> dict[str, Any]:
        """The wire body for ``request`` (without the stream flags)."""
        guard_vision(request)
        outbound: list[dict[str, Any]] = []
        system = "\n".join(part.text for part in request.system)
        if request.system:
            outbound.append({"role": "system", "content": system})
        for message in request.messages:
            outbound.extend(_message_to_wire(message, self._reasoning_continuation))

        body: dict[str, Any] = {"model": request.model, "messages": outbound}
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]
        # The neutral bare-string spelling is already the OpenAI wire shape.
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice
        if request.stop_sequences:
            body["stop"] = list(request.stop_sequences)
        if request.max_tokens is not None:
            body[_output_cap_key(self._max_tokens_param, request.model)] = (
                request.max_tokens
            )
        if request.output_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "noeta_output",
                    "schema": dict(request.output_schema),
                },
            }
        if request.effort is not None:
            body["reasoning_effort"] = _EFFORT_MAP.get(request.effort, request.effort)
        # ``thinking`` has no Chat Completions equivalent; it is ignored.
        return body


# ---------------------------------------------------------------------------
# Inbound translation
# ---------------------------------------------------------------------------


def parse_chat_payload(payload: dict[str, Any], model: str = "") -> ModelResponse:
    """A batch-shaped Chat Completions payload → :class:`ModelResponse`."""
    choices = payload.get("choices")
    if not choices:
        raise Fatal("OpenAI response missing 'choices' field or it was empty")
    choice = choices[0]
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason")
    tool_calls = message.get("tool_calls") or []

    if finish_reason == "stop" and tool_calls:
        # Some compatible servers (Gemini's OpenAI endpoint, older Ollama)
        # report "stop" on a response that does call tools; the calls are what
        # the model asked for, so it is a tool-use stop.
        finish_reason = "tool_calls"
    if finish_reason == "tool_calls" and not tool_calls:
        raise Fatal(
            "inconsistent OpenAI response: finish_reason='tool_calls' but "
            "tool_calls was empty/null"
        )
    stop_reason = _FINISH_REASON_MAP.get(finish_reason or "")
    if stop_reason is None:
        raise Fatal(f"OpenAI response has unsupported finish_reason {finish_reason!r}")

    content: list[Block] = []
    thinking = _extract_thinking(message)
    if thinking is not None:
        content.append(thinking)
    text_value = message.get("content")
    if isinstance(text_value, str) and text_value:
        content.append(Text(text=text_value))
    for call in tool_calls:
        function = call.get("function") or {}
        try:
            arguments = decode_tool_arguments(
                function.get("arguments"), error_label="tool_call arguments"
            )
        except MalformedToolArguments:
            if finish_reason != "length":
                raise
            # The output cap cut this call's arguments mid-JSON. That is a
            # ``max_tokens`` stop, not a garbled stream: keep the complete
            # calls only.
            continue
        content.append(
            ToolUse(
                id=call.get("id", "") or "",
                name=function.get("name", "") or "",
                input=arguments,
            )
        )
    if stop_reason == "refusal":
        # A filtered turn's calls never completed and must not run.
        content = [b for b in content if not isinstance(b, ToolUse)]
    returned_model = payload.get("model")
    return ModelResponse(
        message=Message(role="assistant", content=content),
        stop_reason=stop_reason,
        usage=translate_usage(payload.get("usage")),
        model=returned_model if isinstance(returned_model, str) else model,
    )


def _extract_thinking(message: dict[str, Any]) -> Thinking | None:
    """``reasoning_content`` (DeepSeek, vLLM) or ``reasoning`` (OpenRouter)
    text, plus an ``encrypted_reasoning`` signature when a gateway sends one."""
    text_field = next((k for k in ("reasoning_content", "reasoning") if k in message), None)
    text_value = message.get(text_field) if text_field else None
    signature_value = message.get("encrypted_reasoning")
    signature = signature_value if isinstance(signature_value, str) and signature_value else None
    if not isinstance(text_value, str) or not text_value:
        if signature is not None:
            return Thinking(text="", signature=signature, family=FAMILY)
        return None
    return Thinking(text=text_value, signature=signature, family=FAMILY)


def translate_usage(usage: Any) -> Usage:
    """OpenAI usage → :class:`Usage`. ``prompt_tokens`` is a *total*, so the
    cached part is subtracted out to get ``uncached`` — the opposite of
    Anthropic. ``cache_write`` stays 0 because Chat Completions never reports
    one; a missing or non-dict ``usage`` degrades to an empty ``Usage()``."""
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
        output=int(usage.get("completion_tokens", 0) or 0),
        reasoning=reasoning,
    )


class _ChatStreamAccumulator:
    """Folds Chat Completions stream chunks back into the batch wire shape.

    Delta indexes are assigned in arrival order. Reasoning models emit every
    reasoning fragment before ``content``, so the indexes line up with the
    final response's block positions; either way the two kinds always get
    distinct, ordered indexes.
    """

    def __init__(self, on_delta: OnDelta | None) -> None:
        self._on_delta = on_delta
        self._text_parts: list[str] = []
        self._reasoning_parts: dict[str, list[str]] = {}
        self._signature_parts: list[str] = []
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._finish_reason: str | None = None
        self._usage: dict[str, Any] | None = None
        self._meta: dict[str, Any] = {}
        self._text_index: int | None = None
        self._thinking_index: int | None = None
        self._next_index = 0
        self.saw_done = False

    @property
    def finished(self) -> bool:
        """Did the stream carry a ``finish_reason`` for the choice?"""
        return self._finish_reason is not None

    def _emit(self, kind: Literal["text", "thinking"], text: str) -> None:
        index = self._block_index(kind)
        if self._on_delta is not None:
            self._on_delta(Delta(kind=kind, text=text, index=index))

    def feed(self, chunk: dict[str, Any]) -> None:
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._usage = usage
        # First-seen wins, so the rebuilt payload stays vendor-shaped.
        for key in ("id", "model"):
            value = chunk.get(key)
            if isinstance(value, str) and value and key not in self._meta:
                self._meta[key] = value
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return  # usage-only terminal chunk (or unknown shape)
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
            self._emit("text", text)
        for field_name in ("reasoning_content", "reasoning"):
            fragment = delta.get(field_name)
            if isinstance(fragment, str) and fragment:
                self._reasoning_parts.setdefault(field_name, []).append(fragment)
                self._emit("thinking", fragment)
        # A signature, not visible reasoning text, so never a delta.
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
        call_id = fragment.get("id")
        index = fragment.get("index")
        if not isinstance(index, int):
            # Some gateways omit ``index`` on parallel calls. A fragment then
            # continues the latest call, unless it carries an ``id`` other than
            # that call's — which starts the next call instead of splicing two
            # argument strings into one undecodable blob.
            index = max(self._tool_calls, default=0)
            latest = self._tool_calls.get(index)
            if (
                latest is not None
                and isinstance(call_id, str)
                and call_id
                and latest["id"]
                and call_id != latest["id"]
            ):
                index += 1
        entry = self._tool_calls.setdefault(index, {"id": "", "name": "", "arguments": []})
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
            # Batch tool-call responses carry ``content: null``; mirror that.
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
            {"index": 0, "message": message, "finish_reason": self._finish_reason}
        ]
        if self._usage is not None:
            payload["usage"] = self._usage
        return payload


# ---------------------------------------------------------------------------
# Outbound helpers
# ---------------------------------------------------------------------------


def _output_cap_key(setting: MaxTokensParam, model: str) -> str:
    """The body key for the output cap under ``setting`` for ``model``.

    ``"auto"``: a catalogued row decides by its own columns — an
    ``is_reasoning`` row of the OpenAI family (declared, or inferred from a
    ``gpt`` real id, or an ``o1`` / ``o3`` / ``o4`` / ``gpt-5`` real id) gets
    ``max_completion_tokens``. An uncatalogued id gets it only when it is an
    ``o1`` / ``o3`` / ``o4`` / ``gpt-5`` id. Everything else keeps
    ``max_tokens``, the key every compatible gateway reads.
    """
    if setting != "auto":
        return setting
    spec = models.find_spec(model)
    if spec is None:
        reasoning = bool(_OPENAI_REASONING_ID.match(models.resolve_alias(model)))
    else:
        family = models.model_family(model)
        reasoning = spec.is_reasoning and (
            family == "openai"
            or (family is None and bool(_OPENAI_REASONING_ID.match(spec.real_model_id)))
        )
    return "max_completion_tokens" if reasoning else "max_tokens"


#: Chat Completions carries images only as content parts of a plain user turn.
#: An image anywhere else (an assistant turn, a host-injected turn that renders
#: as ``system``) has no wire shape here, and dropping it silently is never an
#: option — so it is loud.
_NO_IMAGE_SUPPORT = (
    "this provider does not support image input outside a plain user turn; "
    "refusing to silently drop the image."
)


def _image_part(block: Image) -> dict[str, Any]:
    data = base64.b64encode(block.data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{block.media_type};base64,{data}"},
    }


def _message_to_wire(
    message: Message, reasoning_continuation: ReasoningContinuation
) -> list[dict[str, Any]]:
    if message.role == "assistant":
        return [_assistant_to_wire(message, reasoning_continuation)]
    return _user_to_wire(message)


def _user_to_wire(message: Message) -> list[dict[str, Any]]:
    """One user message → its tool results as ``role:"tool"`` messages (first:
    they answer the preceding assistant's calls), then the rest as one user —
    or, when host-injected, ``system`` — message, omitted when empty."""
    out: list[dict[str, Any]] = []
    rest: list[Block] = []
    for block in message.content:
        if isinstance(block, ToolResult):
            content = tool_result_text(block)
            if tool_result_images(block):
                # Chat tool messages carry text only. Say the image existed so
                # the model does not act as if the tool returned only text.
                content += "\n[image omitted: this provider sends tool results as text only]"
            out.append({"role": "tool", "tool_call_id": block.call_id, "content": content})
        elif isinstance(block, (Text, Image)):
            rest.append(block)
    if not rest:
        return out
    if is_host_injected(message):
        # Host-injected turns render as a mid-history ``system`` message,
        # which the Chat shape supports natively. The role alone does not
        # reliably tell an arbitrary model "this is not the user speaking", so
        # the preamble states it in-band. ``system`` carries text only; an
        # image here would vanish without a trace, so it is refused.
        if any(isinstance(b, Image) for b in rest):
            raise Fatal(_NO_IMAGE_SUPPORT)
        out.append(
            {"role": "system", "content": f"{HOST_INJECTED_PREAMBLE}\n{flatten_text(rest)}"}
        )
    elif any(isinstance(b, Image) for b in rest):
        parts = [
            {"type": "text", "text": b.text} if isinstance(b, Text) else _image_part(b)
            for b in rest
            if isinstance(b, (Text, Image))
        ]
        out.append({"role": "user", "content": parts})
    else:
        out.append({"role": "user", "content": flatten_text(rest)})
    return out


def _assistant_to_wire(
    message: Message, reasoning_continuation: ReasoningContinuation
) -> dict[str, Any]:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    signature: str | None = None
    tool_calls: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, Image):
            raise Fatal(_NO_IMAGE_SUPPORT)  # a misroute must be loud
        if isinstance(block, Text):
            text_parts.append(block.text)
        elif isinstance(block, Thinking):
            # Reasoning another family produced is never echoed back here;
            # unknown provenance is.
            if block.family not in (None, FAMILY):
                continue
            thinking_parts.append(block.text)
            if block.signature is not None:
                signature = block.signature
        elif isinstance(block, ToolUse):
            tool_calls.append(
                {
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": encode_tool_arguments(block.input),
                    },
                }
            )
    out: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(text_parts) if text_parts else None,
    }
    # Only a gateway that accepts an echoed ``reasoning_content`` sees one
    # (DeepSeek answers HTTP 400).
    if reasoning_continuation != "off":
        if thinking_parts:
            out["reasoning_content"] = "\n".join(thinking_parts)
        if signature is not None:
            out["encrypted_reasoning"] = signature
    if tool_calls:
        out["tool_calls"] = tool_calls
    return out
