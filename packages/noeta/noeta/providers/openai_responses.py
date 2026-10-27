"""OpenAI Responses-API adapter.

Speaks ``input`` / ``output[]`` / ``function_call`` items, not Chat
Completions' ``messages`` / ``choices`` / ``tool_calls``. ``base_url`` is the
complete responses endpoint and is POSTed verbatim with only a
``?api-version`` query added — appending a path breaks the gateway. Every
request carries ``store:false`` so no state is left gateway-side and a resumed
run never depends on it. The terminal ``response.completed`` event carries the
whole response object, which goes through one parser.
"""

from __future__ import annotations

import base64
import json
from contextlib import aclosing
from typing import Any, Literal

import httpx

from noeta.errors import (
    ContextOverflow,
    Fatal,
    MalformedToolArguments,
    ModelError,
    RateLimited,
    Transient,
)
from noeta.providers._common import (
    HOST_INJECTED_PREAMBLE,
    decode_tool_arguments,
    encode_tool_arguments,
    error_dict_is_context_overflow,
    flatten_text,
    guard_vision,
    is_host_injected,
    model_admits_images,
    tool_result_images,
    tool_result_text,
    translate_http_error,
    transport_error,
)
from noeta.providers._sse import iter_sse_events
from noeta.providers.base import OnDelta
from noeta.types import (
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

__all__ = ["FAMILY", "OpenAIResponsesProvider", "ReasoningContinuation"]

#: ``Provider.family`` and ``Thinking.family`` for this adapter.
FAMILY = "openai_responses"

#: Outbound reasoning-chain echo policy. In Responses, echoing
#: ``encrypted_content`` is required for continuation, so the default is
#: ``"responses"``; ``"off"`` is the escape hatch for a gateway that rejects
#: the ``include`` parameter.
ReasoningContinuation = Literal["off", "chat", "responses"]

#: ``low/medium/high`` pass through; ``xhigh/max`` collapse to ``high`` (the
#: gateway has no finer bucket).
_EFFORT_MAP: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


class _StaleReasoning(Exception):
    """Internal: the 400 rejecting echoed reasoning ciphertext, carried out of
    the stream context so the request can be re-sent without it."""

    def __init__(self, error: ModelError) -> None:
        super().__init__(str(error))
        self.error = error


class OpenAIResponsesProvider:
    """Adapter for an OpenAI Responses-API endpoint.

    Construct once (complete endpoint URL + credentials) and reuse across
    calls; ``ModelRequest.model`` picks the model per call. The key rides an
    Azure-style ``api-key`` header; ``extra_headers`` is the escape hatch for
    gateway-specific headers (``Authorization: Bearer …``, ``X-TT-LOGID``).
    """

    family = FAMILY

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        api_version: str | None = None,
        default_max_tokens: int | None = None,
        timeout_seconds: float = 300.0,
        extra_headers: dict[str, str] | None = None,
        reasoning_continuation: ReasoningContinuation = "responses",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # The httpx client gets no base_url (which would re-join the endpoint
        # as a relative path); the whole endpoint string is POSTed verbatim.
        self._endpoint = base_url.rstrip("/")
        self._params = {"api-version": api_version} if api_version is not None else None
        self._default_max_tokens = default_max_tokens
        self._reasoning_continuation = reasoning_continuation
        headers: dict[str, str] = {"api-key": api_key, "Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        self._client = httpx.AsyncClient(
            headers=headers, timeout=timeout_seconds, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self, request: ModelRequest, on_delta: OnDelta | None = None
    ) -> ModelResponse:
        guard_vision(request)
        body = self.build_body(request)
        body["stream"] = True
        try:
            try:
                return await self._stream(body, request, on_delta)
            except _StaleReasoning:
                # Stale cross-turn reasoning recovery: echoed ``reasoning``
                # items carry ``encrypted_content`` that is only verifiable
                # inside the continuation window the gateway minted it for —
                # across a long suspend (key rotation / TTL) the gateway
                # rejects it with 400 ``invalid_encrypted_content``. The
                # ciphertext is only needed for in-flight continuation, never
                # to make a fresh turn valid, so every echoed reasoning item is
                # dropped and the request re-sent ONCE (``reasoning`` /
                # ``include`` stay, so this turn still reasons fresh).
                retry = dict(body)
                retry["input"] = [
                    item for item in body["input"] if item.get("type") != "reasoning"
                ]
                try:
                    return await self._stream(retry, request, on_delta)
                except _StaleReasoning as again:
                    raise again.error from None
        except httpx.RequestError as exc:
            raise transport_error(exc) from exc

    async def _stream(
        self, body: dict[str, Any], request: ModelRequest, on_delta: OnDelta | None
    ) -> ModelResponse:
        async with self._client.stream(
            "POST",
            self._endpoint,
            params=self._params,
            json=body,
            headers=request.headers or None,
        ) as response:
            if response.is_error:
                await response.aread()
                error = translate_http_error(response)
                if (
                    response.status_code == 400
                    and _is_invalid_encrypted_content(response)
                    and any(item.get("type") == "reasoning" for item in body["input"])
                ):
                    raise _StaleReasoning(error)
                raise error
            final = await _consume_stream(response, on_delta)
        return parse_responses_payload(final, request.model)

    # ------------------------------------------------------------------
    # Outbound translation
    # ------------------------------------------------------------------

    def build_body(self, request: ModelRequest) -> dict[str, Any]:
        """The wire body for ``request`` (without ``stream``)."""
        vision = model_admits_images(request.model)
        items: list[dict[str, Any]] = []
        for message in request.messages:
            items.extend(_message_to_items(message, self._reasoning_continuation, vision))
        body: dict[str, Any] = {"model": request.model, "input": items, "store": False}
        if request.system:
            body["instructions"] = "\n".join(part.text for part in request.system)
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                }
                for tool in request.tools
            ]
        # Absent means omitted, so the gateway uses its default.
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice
        # Request value, else the constructor default; omitted only when both
        # are absent (the gateway default may be small and easily truncated).
        max_tokens = (
            request.max_tokens if request.max_tokens is not None else self._default_max_tokens
        )
        if max_tokens is not None:
            body["max_output_tokens"] = max_tokens
        if request.output_schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "noeta_output",
                    "schema": dict(request.output_schema),
                }
            }
        effort = _map_effort(request)
        if effort is not None:
            # ``summary: auto`` makes the gateway emit readable summary text.
            body["reasoning"] = {"effort": effort, "summary": "auto"}
        # ``include`` is DECOUPLED from the effort gate: a reasoning model
        # reasons at its server-side default even when the request carries no
        # ``reasoning`` block, and without the ciphertext the next turn can
        # only echo an empty reasoning item — the gateway then cannot restore
        # the prior turn's reasoning, which breaks both continuation and the
        # prompt-cache prefix at the first assistant turn.
        if self._reasoning_continuation != "off":
            body["include"] = ["reasoning.encrypted_content"]
        return body


def _map_effort(request: ModelRequest) -> str | None:
    """``thinking=False`` is explicitly suppressed reasoning → ``"minimal"``
    (overrides any effort). Otherwise the effort level maps through
    :data:`_EFFORT_MAP`; no effort → no ``reasoning`` parameter."""
    if not request.thinking:
        return "minimal"
    if request.effort is None:
        return None
    return _EFFORT_MAP.get(request.effort, request.effort)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def _output_index(payload: dict[str, Any]) -> int:
    """A delta event's ``output_index`` (the item's position in ``output[]``,
    monotonic across the response) → ``Delta.index``; malformed → 0."""
    value = payload.get("output_index")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


async def _consume_stream(
    response: httpx.Response, on_delta: OnDelta | None
) -> dict[str, Any]:
    """Drain one Responses SSE stream, emitting deltas; return the terminal
    response object. The SSE ``event:`` name is authoritative; the JSON
    ``type`` field is the fallback for nameless frames.

    * ``response.output_text.delta`` → text deltas;
      ``response.reasoning_summary_text.delta`` → thinking deltas.
    * ``response.function_call_arguments.delta`` → swallowed: the terminal
      object carries every call with its complete ``arguments``.
    * ``response.completed`` / ``response.incomplete`` / a ``response.failed``
      without an error payload → the carried response object.
    * A ``response.failed`` **with** an error payload, and top-level ``error``
      events → raised through :func:`_translate_stream_error`.
    * Everything else is skipped; vendor stream vocabularies drift.

    A stream that ends without a terminal event is truncated → Transient.
    """
    final: dict[str, Any] | None = None
    async with aclosing(iter_sse_events(response.aiter_lines())) as events:
        async for event_name, data in events:
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            event_type = event_name or payload.get("type")
            if event_type in (
                "response.output_text.delta",
                "response.reasoning_summary_text.delta",
            ):
                fragment = payload.get("delta")
                if isinstance(fragment, str) and fragment and on_delta is not None:
                    kind: Literal["text", "thinking"] = (
                        "text" if event_type == "response.output_text.delta" else "thinking"
                    )
                    on_delta(Delta(kind=kind, text=fragment, index=_output_index(payload)))
            elif event_type in (
                "response.completed",
                "response.failed",
                "response.incomplete",
            ):
                response_obj = payload.get("response")
                if event_type == "response.failed" and isinstance(response_obj, dict):
                    error = response_obj.get("error")
                    if isinstance(error, dict):
                        raise _translate_stream_error(error)
                if isinstance(response_obj, dict):
                    final = response_obj
            elif event_type == "error":
                raise _translate_stream_error(payload)
    if final is None:
        raise Transient(
            "Responses stream ended without a terminal response event "
            "(response.completed / response.failed / response.incomplete)"
        )
    return final


#: In-stream error codes worth retrying — the classes the HTTP path buckets
#: by status (429 / 5xx), reported as an error payload mid-stream.
_TRANSIENT_STREAM_ERROR_CODES = frozenset({"server_error", "service_unavailable"})


def _translate_stream_error(error: dict[str, Any]) -> ModelError:
    """Map an in-stream error payload (a top-level ``error`` event or a
    ``response.failed`` object's ``error``) to the neutral taxonomy. An error
    frame carrying no code is in practice a truncated / dropped stream — the
    gateway emits an empty ``error`` event under load, alongside 429s — so it
    is Transient; only a genuine coded rejection is Fatal."""
    code = str(error.get("code") or "")
    message = str(error.get("message") or "")
    detail = f"Responses stream error: code={code or 'unknown'}; {message}"
    if error_dict_is_context_overflow(error):
        return ContextOverflow(detail)
    if code == "rate_limit_exceeded":
        return RateLimited(detail)
    if code in _TRANSIENT_STREAM_ERROR_CODES or not code:
        return Transient(detail)
    return Fatal(detail)


def _is_invalid_encrypted_content(response: httpx.Response) -> bool:
    """A 400 whose error is the gateway rejecting echoed reasoning ciphertext
    (``code: invalid_encrypted_content``, or a gateway's own numeric code with
    a message like "encrypted content ... could not be verified")."""
    try:
        body = json.loads(response.text)
    except ValueError:
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


# ---------------------------------------------------------------------------
# Inbound translation
# ---------------------------------------------------------------------------


def _hit_output_cap(payload: dict[str, Any]) -> bool:
    return _incomplete_reason(payload) == "max_output_tokens"


def _incomplete_reason(payload: dict[str, Any]) -> str | None:
    if payload.get("status") != "incomplete":
        return None
    details = payload.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    return reason if isinstance(reason, str) else None


def _infer_stop_reason(payload: dict[str, Any], has_function_call: bool) -> StopReason:
    """Responses has no usable ``finish_reason``; infer by priority:

    1. ``incomplete`` because of ``max_output_tokens`` → ``max_tokens``
       (overrides a truncated function_call, like Chat's ``length``).
    2. ``incomplete`` because of ``content_filter`` → ``refusal``.
    3. A ``function_call`` item present → ``tool_use``.
    4. ``status == "completed"`` → ``end_turn``.
    5. Otherwise (``failed`` etc.) → :class:`Fatal`.
    """
    reason = _incomplete_reason(payload)
    if reason == "max_output_tokens":
        return "max_tokens"
    if reason == "content_filter":
        return "refusal"
    if has_function_call:
        return "tool_use"
    if payload.get("status") == "completed":
        return "end_turn"
    raise Fatal(
        f"Responses payload ended with status {payload.get('status')!r} "
        f"(incomplete reason {reason!r})"
    )


def parse_responses_payload(payload: dict[str, Any], model: str = "") -> ModelResponse:
    """A Responses response object → :class:`ModelResponse`."""
    output = payload.get("output")
    if not isinstance(output, list):
        raise Fatal("Responses payload missing 'output' array or it was not a list")
    content: list[Block] = []
    has_function_call = False
    truncated = _hit_output_cap(payload)
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            content.extend(_message_item_to_blocks(item))
        elif item_type == "function_call":
            has_function_call = True
            try:
                # Paired by ``call_id``, not the internal ``id``.
                arguments = decode_tool_arguments(
                    item.get("arguments"), error_label="function_call arguments"
                )
            except MalformedToolArguments:
                if not truncated:
                    raise
                # The output cap cut this call's arguments mid-JSON: a
                # ``max_tokens`` stop. Keep the complete calls only.
                continue
            content.append(
                ToolUse(
                    id=str(item.get("call_id") or ""),
                    name=str(item.get("name") or ""),
                    input=arguments,
                )
            )
        elif item_type == "reasoning":
            content.append(_reasoning_item_to_block(item))
    stop_reason = _infer_stop_reason(payload, has_function_call)
    if stop_reason == "refusal":
        content = [b for b in content if not isinstance(b, ToolUse)]
    returned_model = payload.get("model")
    return ModelResponse(
        message=Message(role="assistant", content=content),
        stop_reason=stop_reason,
        usage=translate_usage(payload.get("usage")),
        model=returned_model if isinstance(returned_model, str) else model,
    )


def _reasoning_item_to_block(item: dict[str, Any]) -> Thinking:
    """``summary`` segments joined with newlines → ``text``;
    ``encrypted_content`` → ``signature``, taken verbatim (the continuation
    ciphertext is void if one byte changes)."""
    summary = item.get("summary")
    parts: list[str] = []
    if isinstance(summary, list):
        for segment in summary:
            if isinstance(segment, dict) and isinstance(segment.get("text"), str):
                parts.append(segment["text"])
    encrypted = item.get("encrypted_content")
    return Thinking(
        text="\n".join(parts),
        signature=encrypted if isinstance(encrypted, str) else None,
        family=FAMILY,
    )


def _message_item_to_blocks(item: dict[str, Any]) -> list[Block]:
    blocks: list[Block] = []
    segments = item.get("content")
    if not isinstance(segments, list):
        return blocks
    for segment in segments:
        if isinstance(segment, dict) and segment.get("type") == "output_text":
            text = segment.get("text")
            if isinstance(text, str) and text:
                blocks.append(Text(text=text))
    return blocks


def translate_usage(usage: Any) -> Usage:
    """``input_tokens`` is a total, so ``cached_tokens`` is subtracted out for
    ``uncached``; Responses reports no cache writes; reasoning tokens come from
    ``output_tokens_details``. Missing / non-dict → empty ``Usage``."""
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
        output=int(usage.get("output_tokens", 0) or 0),
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# Outbound helpers
# ---------------------------------------------------------------------------


def _input_image(block: Image) -> dict[str, Any]:
    data = base64.b64encode(block.data).decode("ascii")
    return {"type": "input_image", "image_url": f"data:{block.media_type};base64,{data}"}


def _content_segments(
    blocks: list[Block], text_type: Literal["input_text", "output_text"]
) -> list[dict[str, Any]]:
    """Text and image blocks → ``content[]`` segments. With no image, every
    text block flattens into a **single** segment; with one, each block is its
    own segment in order (images as ``input_image``)."""
    if not any(isinstance(b, Image) for b in blocks):
        return [{"type": text_type, "text": flatten_text(blocks)}]
    segments: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, Text):
            segments.append({"type": text_type, "text": block.text})
        elif isinstance(block, Image):
            segments.append(_input_image(block))
    return segments


def _message_to_items(
    message: Message, reasoning_continuation: ReasoningContinuation, vision: bool
) -> list[dict[str, Any]]:
    """One message → a sequence of ``input`` items: tool calls and results are
    top-level items peer to ``message``, so one message may expand to several."""
    if message.role == "assistant":
        return _assistant_to_items(message, reasoning_continuation)
    items: list[dict[str, Any]] = []
    rest: list[Block] = []
    for block in message.content:
        if isinstance(block, ToolResult):
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": block.call_id,
                    "output": _tool_result_output(block, vision),
                }
            )
        elif isinstance(block, (Text, Image)):
            rest.append(block)
    if not rest:
        return items
    if is_host_injected(message):
        # Responses supports a mid-history ``system`` input item natively; the
        # role alone does not reliably tell an arbitrary model "this is not the
        # user speaking", so the preamble rides in front as its own segment.
        items.append(
            {
                "type": "message",
                "role": "system",
                "content": [
                    {"type": "input_text", "text": HOST_INJECTED_PREAMBLE},
                    *_content_segments(rest, "input_text"),
                ],
            }
        )
    else:
        items.append(
            {"type": "message", "role": "user", "content": _content_segments(rest, "input_text")}
        )
    return items


def _tool_result_output(block: ToolResult, vision: bool) -> Any:
    """``function_call_output.output``: the plain string, or — with images and
    an image-admitting model — a content-part array (the text as one
    ``input_text`` segment, then one ``input_image`` per image). A catalogued
    non-vision model degrades to the string plus a note; never a crash."""
    text = tool_result_text(block)
    images = tool_result_images(block)
    if not images:
        return text
    if not vision:
        return text + "\n[image omitted: model is not vision-capable]"
    return [{"type": "input_text", "text": text}, *(_input_image(i) for i in images)]


def _assistant_to_items(
    message: Message, reasoning_continuation: ReasoningContinuation
) -> list[dict[str, Any]]:
    """Layout: ``reasoning`` items → one ``message`` item (only when text or
    images exist; a pure tool-call turn sends none) → ``function_call`` items.
    Reasoning is echoed only when continuation is on, the block carries
    ciphertext (an item without it restores nothing and breaks the gateway's
    prompt-cache prefix), and this family produced it."""
    reasoning: list[dict[str, Any]] = []
    body: list[Block] = []
    calls: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, Thinking):
            if (
                reasoning_continuation != "off"
                and block.signature is not None
                and block.family in (None, FAMILY)
            ):
                reasoning.append(
                    {
                        "type": "reasoning",
                        "summary": (
                            [{"type": "summary_text", "text": block.text}] if block.text else []
                        ),
                        "encrypted_content": block.signature,
                    }
                )
        elif isinstance(block, ToolUse):
            calls.append(
                {
                    "type": "function_call",
                    "call_id": block.id,
                    "name": block.name,
                    "arguments": encode_tool_arguments(block.input),
                }
            )
        elif isinstance(block, (Text, Image)):
            body.append(block)
    items = list(reasoning)
    if flatten_text(body) or any(isinstance(b, Image) for b in body):
        items.append(
            {
                "type": "message",
                "role": "assistant",
                "content": _content_segments(body, "output_text"),
            }
        )
    items.extend(calls)
    return items
