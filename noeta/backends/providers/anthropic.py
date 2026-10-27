"""Anthropic ``/v1/messages`` adapter.

Every loss of fidelity caused by the Messages wire shape is contained to this
module (and its stream accumulator in :mod:`._anthropic_stream`), so the loop
only ever sees :mod:`noeta.core.types`. The provider pins no model —
``ModelRequest.model`` is forwarded per call — and ``max_tokens`` is never
invented: Anthropic requires it, and a guessed cap truncates answers
invisibly, so it comes from the request, the constructor default, or the
model's catalogued output ceiling, and failing all three the call is refused.
Three wire concerns are stamped on the outbound body only: the ephemeral
prompt-cache breakpoints, the ``<system-reminder>`` framing Anthropic needs
because it has no mid-history system role, and the ``stream`` flag.

**Every request is transported over SSE.** The non-streaming POST holds one
socket idle for the whole generation and dies on the client timeout: a long
answer on a 128K cap reliably outran 60 s and then burned the retry budget
re-running the same doomed call. A streamed connection keeps producing bytes
(Anthropic also sends ``ping`` events), so the timeout measures silence rather
than total generation time.
"""

from __future__ import annotations

import base64
import os
from contextlib import aclosing
from typing import Any

import httpx

from noeta.backends.providers._anthropic_stream import StreamAccumulator, parse_message
from noeta.backends.providers._common import (
    guard_vision,
    is_host_injected,
    model_admits_images,
    neutralize_reserved_tags,
    tool_result_images,
    tool_result_text,
    translate_http_error,
    transport_error,
)
from noeta.backends.providers._sse import iter_sse_events
from noeta.core import models
from noeta.core.errors import Fatal
from noeta.core.provider import OnDelta
from noeta.core.types import (
    Image,
    Message,
    ModelRequest,
    ModelResponse,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
)

__all__ = ["FAMILY", "AnthropicProvider"]

#: ``Provider.family`` and ``Thinking.family`` for this adapter.
FAMILY = "anthropic"

_API_VERSION_DEFAULT = "2023-06-01"
_API_KEY_ENV = "ANTHROPIC_API_KEY"
_MESSAGES_ENDPOINT = "/v1/messages"

#: Anthropic's per-request cap on ``cache_control`` breakpoints.
_MAX_CACHE_BREAKPOINTS = 4

#: The single ephemeral cache breakpoint marker (default 5-minute TTL).
_CACHE_CONTROL_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}


class AnthropicProvider:
    """Adapter for the Anthropic Messages API.

    Construct once and reuse across calls — the underlying
    :class:`httpx.AsyncClient` is shared, and ``ModelRequest.model`` selects
    the model per call. ``extra_headers`` is the escape hatch for
    ``anthropic-beta`` flags, org ids and proxy auth; prompt caching needs no
    beta flag. ``ModelRequest.headers`` merge over these per call; they are
    transport-only and never part of the prompt-cache key (the cache key is
    the rendered body, not the HTTP headers).
    """

    family = FAMILY

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        anthropic_version: str = _API_VERSION_DEFAULT,
        default_max_tokens: int | None = None,
        timeout_seconds: float = 60.0,
        extra_headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """``api_key`` defaults to the ``ANTHROPIC_API_KEY`` environment
        variable; an explicit argument still wins. Missing credentials raise
        here rather than yielding a client that fails its first call with an
        opaque 401. ``timeout_seconds`` bounds silence on the stream, not the
        whole generation."""
        resolved_key = api_key if api_key is not None else os.environ.get(_API_KEY_ENV)
        if not resolved_key:
            raise ValueError(
                "AnthropicProvider needs an API key: pass api_key=... or set "
                f"the {_API_KEY_ENV} environment variable"
            )
        self._default_max_tokens = default_max_tokens
        headers: dict[str, str] = {
            "x-api-key": resolved_key,
            "anthropic-version": anthropic_version,
            "content-type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        self._client = httpx.AsyncClient(
            base_url=(base_url or "https://api.anthropic.com").rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self, request: ModelRequest, on_delta: OnDelta | None = None
    ) -> ModelResponse:
        guard_vision(request)
        body = self.build_body(request)
        body["stream"] = True
        accumulator = StreamAccumulator(on_delta)
        try:
            async with self._client.stream(
                "POST",
                _MESSAGES_ENDPOINT,
                json=body,
                headers=request.headers or None,
            ) as response:
                if response.is_error:
                    # An unread streaming response cannot be sniffed for the
                    # overflow markers, so the (small) error body is read first.
                    await response.aread()
                    raise translate_http_error(response)
                async with aclosing(iter_sse_events(response.aiter_lines())) as events:
                    async for event_name, data in events:
                        accumulator.feed(event_name, data)
        except httpx.RequestError as exc:
            # Timeouts and transport failures, including a disconnect
            # mid-iteration, stay transient so the retry helper applies.
            raise transport_error(exc) from exc
        return parse_message(accumulator.message_payload(), request.model)

    # ------------------------------------------------------------------
    # Outbound translation
    # ------------------------------------------------------------------

    def build_body(self, request: ModelRequest) -> dict[str, Any]:
        """The wire body for ``request`` (without ``stream``). Public so tests
        and diagnostics can see exactly what would be sent."""
        vision = model_admits_images(request.model)

        # Anthropic has no mid-history system role, so a host-injected turn
        # renders as <system-reminder>-wrapped text and merges into the
        # adjacent user wire turn. Only pairs touching an injected turn merge;
        # plain consecutive user turns keep their 1:1 rendering.
        outbound: list[dict[str, Any]] = []
        #: The last wire block at or before each message index — what a
        #: ``cache_after`` index stamps. The dicts are shared by reference with
        #: ``outbound`` (merging spreads the same dicts), so stamping later
        #: lands on the body.
        last_block: list[dict[str, Any] | None] = []
        prev_injected = False
        for message in request.messages:
            wire = _message_to_wire(message, vision)
            injected = is_host_injected(message)
            previous = last_block[-1] if last_block else None
            last_block.append(wire["content"][-1] if wire["content"] else previous)
            if (
                (injected or prev_injected)
                and wire["role"] == "user"
                and outbound
                and outbound[-1]["role"] == "user"
            ):
                outbound[-1]["content"] = [*outbound[-1]["content"], *wire["content"]]
            else:
                outbound.append(wire)
            prev_injected = injected

        body: dict[str, Any] = {
            "model": request.model,
            "max_tokens": self._resolve_max_tokens(request),
            "messages": outbound,
        }
        system = [
            {"type": "text", "text": part.text}
            for part in request.system
            if part.text
        ]
        if system:
            body["system"] = system
        if request.tools:
            body["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in request.tools
            ]
        # The neutral spelling is a bare string, which Anthropic wants wrapped
        # as ``{"type": ...}``.
        if request.tool_choice is not None:
            body["tool_choice"] = {"type": request.tool_choice}
        _apply_cache_control(body, request, last_block)
        if request.output_schema is not None:
            body["output_config"] = {
                "format": {"type": "json_schema", "schema": dict(request.output_schema)}
            }
        # Effort rides inside output_config, alongside any format set above;
        # every level (xhigh and max included) passes through verbatim.
        if request.effort is not None:
            body.setdefault("output_config", {})["effort"] = request.effort
        thinking = _thinking_param(request)
        if thinking is not None:
            body["thinking"] = thinking
        return body

    def _resolve_max_tokens(self, request: ModelRequest) -> int:
        """Request beats constructor default beats the catalogued output
        ceiling; none of them → :class:`Fatal`. Anthropic requires
        ``max_tokens``, and a cap this adapter invented would truncate answers
        with no signal, so it never picks one."""
        if request.max_tokens is not None:
            return int(request.max_tokens)
        if self._default_max_tokens is not None:
            return int(self._default_max_tokens)
        spec = models.find_spec(request.model)
        if spec is not None:
            return spec.max_output_tokens
        raise Fatal(
            "Anthropic requires max_tokens; set ModelRequest.max_tokens, pass "
            "AnthropicProvider(default_max_tokens=...), or register the model "
            "in noeta.core.models"
        )


def _thinking_param(request: ModelRequest) -> dict[str, str] | None:
    """``thinking`` → the Messages ``thinking`` parameter.

    ``False`` is an explicit ``{"type": "disabled"}``. ``True`` asks for
    ``{"type": "adaptive"}`` only on a model catalogued as a reasoning model:
    on an unknown or non-reasoning model (Haiku 4.5 takes only the older
    budget form) the parameter is left out and the vendor default applies,
    which is what every request carried before the field became a bool.
    """
    if not request.thinking:
        return {"type": "disabled"}
    spec = models.find_spec(request.model)
    if spec is not None and spec.is_reasoning:
        return {"type": "adaptive"}
    return None


def _apply_cache_control(
    body: dict[str, Any],
    request: ModelRequest,
    last_block: list[dict[str, Any] | None],
) -> None:
    """Stamp ephemeral prompt-cache breakpoints onto the wire body.

    Anthropic renders the prefix ``tools`` → ``system`` → ``messages`` and a
    breakpoint caches everything before it. Candidates, highest priority
    first, until the vendor cap of :data:`_MAX_CACHE_BREAKPOINTS`:

    * each ``cache_after`` message index, newest first — the composer puts
      these on the last recorded block (never a volatile reminder, which would
      key the entry to bytes the next step drops) and on the previous step's
      last block (Anthropic looks back only ~20 blocks for an entry an earlier
      request wrote, so a step that appended more would otherwise miss it);
    * each ``SystemPart`` with ``cache=True``, last first;
    * the last tool — tools render first, so this one covers tools only and
      matters when the system prompt changes.

    A block already stamped is not counted twice.
    """
    targets: list[dict[str, Any]] = []
    for index in sorted(set(request.cache_after), reverse=True):
        if 0 <= index < len(last_block):
            block = last_block[index]
            if block is not None:
                targets.append(block)
    system = body.get("system")
    if isinstance(system, list):
        rendered = iter(system)
        cached_system: list[dict[str, Any]] = []
        for part in request.system:
            if not part.text:
                continue
            block = next(rendered)
            if part.cache:
                cached_system.append(block)
        targets.extend(reversed(cached_system))
    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        targets.append(tools[-1])

    stamped: list[dict[str, Any]] = []
    for target in targets:
        if len(stamped) >= _MAX_CACHE_BREAKPOINTS:
            break
        if any(target is seen for seen in stamped):
            continue
        target["cache_control"] = dict(_CACHE_CONTROL_EPHEMERAL)
        stamped.append(target)


def _image_to_wire(block: Image) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": block.media_type,
            "data": base64.b64encode(block.data).decode("ascii"),
        },
    }


def _wrap_system_reminder(text: str) -> str:
    """Anthropic-only tag syntax. The wrapped text is neutralised first:
    several host-authored turns stand in for text produced elsewhere (a
    background job's stdout, a sub-agent's answer, a recalled memory body),
    and this wrapper is the only thing allowed to open or close the turn."""
    return f"<system-reminder>\n{neutralize_reserved_tags(text)}\n</system-reminder>"


def _message_to_wire(message: Message, vision: bool) -> dict[str, Any]:
    if message.role == "assistant":
        return _assistant_to_wire(message)
    return _user_to_wire(message, vision)


def _user_to_wire(message: Message, vision: bool) -> dict[str, Any]:
    """A user message → one Anthropic user turn. ``tool_result`` blocks lead
    the turn (Anthropic wants them first, answering the preceding
    ``tool_use``); text and images follow in order. Thinking / tool-use blocks
    have no meaning in a user turn and are skipped."""
    wrap = is_host_injected(message)
    results: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, ToolResult):
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.call_id,
                    "content": _tool_result_content(block, vision),
                    "is_error": block.is_error,
                }
            )
        elif isinstance(block, Text):
            text = _wrap_system_reminder(block.text) if wrap else block.text
            rest.append({"type": "text", "text": text})
        elif isinstance(block, Image):
            rest.append(_image_to_wire(block))
    return {"role": "user", "content": [*results, *rest]}


def _tool_result_content(block: ToolResult, vision: bool) -> Any:
    """``tool_result.content`` accepts a bare string or a block array. With no
    images this is the **string**, keeping the text-only path compact. With
    images and a vision-capable model it is an array: one text block, then one
    base64 image block per image. A catalogued non-vision model gets the string
    plus a note, so it does not act as if the tool returned only text."""
    text = tool_result_text(block)
    images = tool_result_images(block)
    if not images:
        return text
    if not vision:
        return text + "\n[image omitted: model is not vision-capable]"
    return [{"type": "text", "text": text}, *(_image_to_wire(i) for i in images)]


def _assistant_to_wire(message: Message) -> dict[str, Any]:
    """Regroup content into Anthropic's required order ``thinking* / text* /
    image* / tool_use*``, stable within each group. Thinking another family
    produced never goes back: its signature is not one Anthropic issued and
    Anthropic rejects foreign or unsigned thinking. A block of unknown family
    (recorded without one) still goes."""
    thinking: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, Thinking):
            if block.family not in (None, FAMILY):
                continue
            if block.data is not None:
                # A redacted (encrypted) reasoning block: re-emit the opaque
                # blob verbatim under its own wire type, never as a
                # ``thinking`` block (an empty-text one would be rejected).
                thinking.append({"type": "redacted_thinking", "data": block.data})
                continue
            entry: dict[str, Any] = {"type": "thinking", "thinking": block.text}
            if block.signature is not None:
                entry["signature"] = block.signature
            thinking.append(entry)
        elif isinstance(block, Text):
            # The API rejects an empty text block, so one never goes out.
            if block.text.strip():
                texts.append({"type": "text", "text": block.text})
        elif isinstance(block, Image):
            images.append(_image_to_wire(block))
        elif isinstance(block, ToolUse):
            calls.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
    return {"role": "assistant", "content": [*thinking, *texts, *images, *calls]}
