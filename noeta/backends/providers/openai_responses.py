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

from typing import Any, Literal

import httpx

from noeta.backends.providers._common import (
    HOST_INJECTED_PREAMBLE,
    ReasoningContinuation,
    encode_tool_arguments,
    flatten_text,
    guard_vision,
    image_data_url,
    is_host_injected,
    model_admits_images,
    restore_tool_names,
    tool_result_images,
    tool_result_text,
    translate_http_error,
    transport_error,
    wire_tool_name,
)
from noeta.backends.providers._openai_responses_stream import (
    FAMILY,
    consume_stream,
    is_invalid_encrypted_content,
    parse_responses_payload,
)
from noeta.backends.providers._openai_responses_stream import (
    translate_usage as translate_usage,
)
from noeta.core.errors import ModelError
from noeta.core.provider import OnDelta
from noeta.core.types import (
    Block,
    Image,
    Message,
    ModelRequest,
    ModelResponse,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
)

__all__ = ["FAMILY", "OpenAIResponsesProvider", "ReasoningContinuation"]

# ``ReasoningContinuation``: the outbound reasoning-chain echo policy. In
# Responses, echoing ``encrypted_content`` is required for continuation, so the
# default is ``"responses"``; ``"off"`` is the escape hatch for a gateway that
# rejects the ``include`` parameter.

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
                    and is_invalid_encrypted_content(response)
                    and any(item.get("type") == "reasoning" for item in body["input"])
                ):
                    raise _StaleReasoning(error)
                raise error
            final = await consume_stream(response, on_delta)
        return restore_tool_names(parse_responses_payload(final, request.model), request)

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
                    "name": wire_tool_name(tool.name),
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
    if request.thinking is False:
        return "minimal"
    if request.effort is None:
        return None
    return _EFFORT_MAP.get(request.effort, request.effort)


# ---------------------------------------------------------------------------
# Outbound helpers
# ---------------------------------------------------------------------------


def _input_image(block: Image) -> dict[str, Any]:
    return {"type": "input_image", "image_url": image_data_url(block)}


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
                    "name": wire_tool_name(block.name),
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
