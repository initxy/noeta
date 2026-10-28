"""Anthropic ``/v1/messages`` adapter.

Every loss of fidelity caused by the Messages wire shape is contained to this
module (and its stream accumulator in :mod:`._anthropic_stream`), so the loop
only ever sees :mod:`noeta.core.types`. The provider pins no model —
``ModelRequest.model`` is forwarded per call — and ``max_tokens`` is never
invented: Anthropic requires it, and a guessed cap truncates answers
invisibly, so it comes from the request, the constructor default, or the
model's catalogued output ceiling, and failing all three the call is refused.
The body is the one Claude Code sends an API-key user: its beta set,
adaptive thinking with summarized updates, old thinking kept
(``context_management``), effort ``medium`` unless the request sets one,
``metadata.user_id``, harness notes as mid-conversation ``system`` turns,
and three ephemeral cache breakpoints (the two system blocks and the last
message). Host context in the user's turn is framed as
``<system-reminder>``.

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
from collections.abc import Sequence
from contextlib import aclosing
from typing import Any

import httpx

from noeta.backends.providers._anthropic_stream import StreamAccumulator, parse_message
from noeta.backends.providers._anthropic_user_id import user_id
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
from noeta.core.runner.texts import REJECTED_MARKER
from noeta.core.types import (
    Image,
    Message,
    ModelRequest,
    ModelResponse,
    Text,
    Thinking,
    ToolReference,
    ToolResult,
    ToolUse,
)

__all__ = ["DEFAULT_BETAS", "FAMILY", "AnthropicProvider"]

#: ``Provider.family`` and ``Thinking.family`` for this adapter.
FAMILY = "anthropic"

_API_VERSION_DEFAULT = "2023-06-01"
_API_KEY_ENV = "ANTHROPIC_API_KEY"
_MESSAGES_ENDPOINT = "/v1/messages"
_COUNT_TOKENS_ENDPOINT = "/v1/messages/count_tokens?beta=true"
_COUNT_TOKENS_BETA = "token-counting-2024-11-01"

#: Anthropic's per-request cap on ``cache_control`` breakpoints.
_MAX_CACHE_BREAKPOINTS = 4

#: The ``anthropic-beta`` set Claude Code 2.1.312 sends an API-key user in
#: its default permission mode (mid-conversation system turns, adaptive
#: thinking updates, effort and thinking clearing ride on these).
DEFAULT_BETAS: tuple[str, ...] = (
    "claude-code-20250219",
    "interleaved-thinking-2025-05-14",
    "context-management-2025-06-27",
    "per-turn-control-2026-07-01",
    "mid-conversation-tool-changes-2026-07-01",
    "thinking-token-count-2026-05-13",
    "advanced-tool-use-2025-11-20",
    "effort-2025-11-24",
    "thinking-display-updates-2026-08-18",
)
#: How a message the person sent during a running turn is shown, inside the
#: user turn that carries the tool results.
MID_TURN_MESSAGE = (
    "While you were working, the user sent this message:\n{text}\n\n"
    "This is how a message sent during a turn reaches you: inside the turn, usually "
    "alongside the next tool result. Address it as you carry on."
)
#: Effort sent with adaptive thinking when the request names none.
_DEFAULT_EFFORT = "medium"

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
        betas: Sequence[str] = DEFAULT_BETAS,
        device_id: str | None = None,
        system_turns: bool = True,
    ) -> None:
        """``api_key`` defaults to the ``ANTHROPIC_API_KEY`` environment
        variable; an explicit argument still wins. Missing credentials raise
        here rather than yielding a client that fails its first call with an
        opaque 401. ``timeout_seconds`` bounds silence on the stream, not the
        whole generation. ``betas`` is the ``anthropic-beta`` header (an
        ``extra_headers`` entry wins); ``device_id`` the stable id in
        ``metadata.user_id`` (default: one per process). ``system_turns=False``
        is for an endpoint without mid-conversation ``system`` turns: they go
        as ``<system-reminder>`` text in the user's turn instead."""
        resolved_key = api_key if api_key is not None else os.environ.get(_API_KEY_ENV)
        if not resolved_key:
            raise ValueError(
                "AnthropicProvider needs an API key: pass api_key=... or set "
                f"the {_API_KEY_ENV} environment variable"
            )
        self._default_max_tokens = default_max_tokens
        self._device_id = device_id
        self._system_turns = system_turns
        headers: dict[str, str] = {
            "x-api-key": resolved_key,
            "anthropic-version": anthropic_version,
            "content-type": "application/json",
        }
        if betas:
            headers["anthropic-beta"] = ",".join(betas)
        if extra_headers:
            headers.update(extra_headers)
        self._betas = headers.get("anthropic-beta", "")
        self._client = httpx.AsyncClient(
            base_url=(base_url or "https://api.anthropic.com").rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def request_headers(self, request: ModelRequest) -> dict[str, str]:
        """The per-call headers: the request's own, and the beta header
        widened by ``request.betas``."""
        headers = dict(request.headers)
        if request.betas:
            have = [b for b in headers.get("anthropic-beta", self._betas).split(",") if b]
            headers["anthropic-beta"] = ",".join(
                [*have, *(b for b in request.betas if b not in have)])
        return headers

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
                headers=self.request_headers(request) or None,
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

    async def count_tokens(self, model: str, text: str) -> int | None:
        """The exact input-token count of one user message holding ``text``
        (``/v1/messages/count_tokens``, as Claude Code measures a large tool
        result); ``None`` when the endpoint gives no count."""
        if not text:
            return 0
        body = {"model": model, "messages": [{"role": "user", "content": text}], "tools": []}
        betas = [b for b in self._client.headers.get("anthropic-beta", "").split(",") if b]
        try:
            response = await self._client.post(
                _COUNT_TOKENS_ENDPOINT, json=body,
                headers={"anthropic-beta": ",".join([*betas, _COUNT_TOKENS_BETA])})
        except httpx.RequestError:
            return None
        if response.is_error:
            return None
        try:
            count = response.json().get("input_tokens")
        except ValueError:
            return None
        return count if isinstance(count, int) else None

    # ------------------------------------------------------------------
    # Outbound translation
    # ------------------------------------------------------------------

    def build_body(self, request: ModelRequest) -> dict[str, Any]:
        """The wire body for ``request`` (without ``stream``). Public so tests
        and diagnostics can see exactly what would be sent."""
        vision = model_admits_images(request.model)

        # Host context in the user channel renders as <system-reminder>-
        # wrapped text and merges into the adjacent user wire turn. Only
        # pairs touching an injected turn merge; plain consecutive user turns
        # keep their 1:1 rendering. Adjacent system turns join into one,
        # their texts a blank line apart.
        outbound: list[dict[str, Any]] = []
        #: The last wire block at or before each message index — what a
        #: ``cache_after`` index stamps. The dicts are shared by reference with
        #: ``outbound`` (merging spreads the same dicts), so stamping later
        #: lands on the body.
        last_block: list[dict[str, Any] | None] = []
        #: Each message's own wire blocks (``cache_blocks`` stamps one).
        blocks_of: list[list[dict[str, Any]]] = []
        prev_injected = False
        for message in request.messages:
            if message.role == "system" and not self._system_turns:
                message = Message(role="user", content=message.content, origin="system")
            wire = _message_to_wire(message, vision)
            blocks_of.append(list(wire["content"]))
            injected = is_host_injected(message)
            previous = last_block[-1] if last_block else None
            if (wire["role"] == "system" and wire["content"] and outbound
                    and outbound[-1]["role"] == "system"):
                joined = outbound[-1]["content"][-1]
                joined["text"] += "\n\n" + "\n\n".join(b["text"] for b in wire["content"])
                last_block.append(joined)
                continue
            last_block.append(wire["content"][-1] if wire["content"] else previous)
            mid_turn = (
                message.role == "user" and message.origin == "human" and outbound
                and outbound[-1]["role"] == "user"
                and any(b.get("type") == "tool_result" for b in outbound[-1]["content"])
                and not any(b.get("type") == "tool_result" for b in wire["content"])
            )
            after_decline = (
                message.role == "user" and message.origin == "human" and outbound
                and outbound[-1]["role"] == "user" and outbound[-1]["content"]
                and outbound[-1]["content"][-1].get("text") == REJECTED_MARKER)
            if after_decline:
                # The turn stopped at a declined call: the next prompt joins
                # the results' turn after the marker, unframed (Claude Code).
                outbound[-1]["content"] = [*outbound[-1]["content"], *wire["content"]]
                prev_injected = injected
                continue
            if mid_turn:
                # A message the person sent while the turn ran rides in the
                # tool results' turn, framed as Claude Code frames it.
                for block in wire["content"]:
                    if block.get("type") == "text":
                        block["text"] = MID_TURN_MESSAGE.format(text=block["text"])
            if (
                (injected or prev_injected or mid_turn)
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
                    **({"defer_loading": True} if tool.defer_loading else {}),
                }
                for tool in request.tools
            ]
        # The neutral spelling is a bare string, which Anthropic wants wrapped
        # as ``{"type": ...}``.
        if request.tool_choice is not None:
            body["tool_choice"] = {"type": request.tool_choice}
        _apply_cache_control(body, request, last_block, blocks_of)
        _bare_strings(outbound)
        if request.stop_sequences:
            body["stop_sequences"] = list(request.stop_sequences)
        body["metadata"] = {"user_id": user_id(self._device_id, request.root_id)}
        if request.output_schema is not None:
            body["output_config"] = {
                "format": {"type": "json_schema", "schema": dict(request.output_schema)}
            }
        thinking = _thinking_param(request)
        if thinking is not None:
            body["thinking"] = thinking
        adaptive = thinking is not None and thinking["type"] == "adaptive"
        if adaptive:
            body["context_management"] = {
                "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}
        # Effort rides inside output_config, alongside any format set above;
        # every level (xhigh and max included) passes through verbatim.
        effort = request.effort or (_DEFAULT_EFFORT if adaptive else None)
        if effort is not None:
            body.setdefault("output_config", {})["effort"] = effort
        for key, value in request.vendor_fields.items():
            body.setdefault(key, value)
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

    ``False`` is an explicit ``{"type": "disabled"}``, ``None`` leaves the
    parameter out. ``True`` asks for ``{"type": "adaptive"}`` only on a model
    catalogued as a reasoning model: on an unknown or non-reasoning model
    (Haiku 4.5 takes only the older budget form) the parameter is left out
    and the vendor default applies, which is what every request carried
    before the field became a bool.
    """
    if request.thinking is None:
        return None
    if not request.thinking:
        return {"type": "disabled"}
    spec = models.find_spec(request.model)
    if spec is not None and spec.is_reasoning:
        return {"type": "adaptive", "display": "updates"}
    return None


def _bare_strings(outbound: list[dict[str, Any]]) -> None:
    """A user or system turn of one text block with no breakpoint goes as a
    bare string, as Claude Code sends it."""
    for wire in outbound:
        content = wire["content"]
        if (wire["role"] != "assistant" and len(content) == 1
                and content[0].get("type") == "text" and "cache_control" not in content[0]):
            wire["content"] = content[0]["text"]


def _apply_cache_control(
    body: dict[str, Any],
    request: ModelRequest,
    last_block: list[dict[str, Any] | None],
    blocks_of: list[list[dict[str, Any]]] | None = None,
) -> None:
    """Stamp ephemeral prompt-cache breakpoints onto the wire body.

    Anthropic renders the prefix ``tools`` → ``system`` → ``messages`` and a
    breakpoint caches everything before it. Candidates, highest priority
    first, until the vendor cap of :data:`_MAX_CACHE_BREAKPOINTS`:

    * each ``cache_after`` message index, newest first (the composer names
      the last message, as Claude Code does);
    * each ``cache_blocks`` block, newest first;
    * each ``SystemPart`` with ``cache=True``, last first.

    The tools get no mark of their own: the first system block's covers
    them. A block already stamped is not counted twice.
    """
    targets: list[dict[str, Any]] = []
    for index in sorted(set(request.cache_after), reverse=True):
        if 0 <= index < len(last_block):
            block = last_block[index]
            if block is not None:
                targets.append(block)
    for mi, bi in sorted(set(request.cache_blocks), reverse=True):
        if blocks_of is not None and 0 <= mi < len(blocks_of) and \
                0 <= bi < len(blocks_of[mi]):
            targets.append(blocks_of[mi][bi])
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
    if message.role == "system":
        return {"role": "system", "content": [
            {"type": "text", "text": neutralize_reserved_tags(b.text)}
            for b in message.content if isinstance(b, Text) and b.text]}
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
    refs = [b for b in block.content if isinstance(b, ToolReference)]
    if refs:
        return [{"type": "tool_reference", "tool_name": r.tool_name} for r in refs]
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
