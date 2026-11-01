"""Anthropic ``/v1/messages`` adapter.

Every loss of fidelity caused by the Messages wire shape is contained to this
module (with its outbound translation in :mod:`._anthropic_wire` and its
stream accumulator in :mod:`._anthropic_stream`), so the loop
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

**Every request is transported over SSE first.** The non-streaming POST
holds one socket idle for the whole generation and dies on the client
timeout: a long answer on a 128K cap reliably outran 60 s and then burned the
retry budget re-running the same doomed call. A streamed connection keeps
producing bytes (Anthropic also sends ``ping`` events), so the timeout
measures silence rather than total generation time. A stream that breaks
after it began raises :class:`~noeta.core.errors.StreamBroken`, and the
retry helper goes on with :meth:`AnthropicProvider.complete_whole`, Claude
Code's fallback: the same body sent without streaming, its output capped at
64,000 tokens, with a 300 s client timeout.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from contextlib import aclosing
from typing import Any

import httpx

from noeta.backends.providers._anthropic_stream import StreamAccumulator, parse_message
from noeta.backends.providers._anthropic_user_id import user_id
from noeta.backends.providers._anthropic_wire import (
    FAMILY,
    apply_cache_control,
    bare_strings,
    messages_to_wire,
    thinking_param,
)
from noeta.backends.providers._anthropic_wire import (
    MID_TURN_MESSAGE as MID_TURN_MESSAGE,
)
from noeta.backends.providers._anthropic_wire import (
    _wrap_system_reminder as _wrap_system_reminder,
)
from noeta.backends.providers._common import (
    guard_vision,
    model_admits_images,
    translate_http_error,
    transport_error,
)
from noeta.backends.providers._sse import iter_sse_events
from noeta.core import models
from noeta.core.errors import Fatal, StreamBroken, Transient
from noeta.core.provider import OnDelta
from noeta.core.types import ModelRequest, ModelResponse

__all__ = ["DEFAULT_BETAS", "FAMILY", "AnthropicProvider"]

_API_VERSION_DEFAULT = "2023-06-01"
_API_KEY_ENV = "ANTHROPIC_API_KEY"
_MESSAGES_ENDPOINT = "/v1/messages"
_COUNT_TOKENS_ENDPOINT = "/v1/messages/count_tokens?beta=true"
_COUNT_TOKENS_BETA = "token-counting-2024-11-01"

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
#: Effort sent with adaptive thinking when the request names none.
_DEFAULT_EFFORT = "medium"
#: Claude Code's ceilings for a call answered in one piece after a failed
#: stream: its output and its client timeout (seconds).
WHOLE_MAX_TOKENS = 64_000
WHOLE_TIMEOUT_SECONDS = 300.0


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
        whole_timeout_seconds: float = WHOLE_TIMEOUT_SECONDS,
    ) -> None:
        """``api_key`` defaults to the ``ANTHROPIC_API_KEY`` environment
        variable; an explicit argument still wins. Missing credentials raise
        here rather than yielding a client that fails its first call with an
        opaque 401. ``timeout_seconds`` bounds silence on the stream, not the
        whole generation. ``betas`` is the ``anthropic-beta`` header (an
        ``extra_headers`` entry wins); ``device_id`` the stable id in
        ``metadata.user_id`` (default: one per process). ``system_turns=False``
        is for an endpoint without mid-conversation ``system`` turns: they go
        as ``<system-reminder>`` text in the user's turn instead.
        ``whole_timeout_seconds`` bounds a call answered in one piece
        (:meth:`complete_whole`) from start to end."""
        resolved_key = api_key if api_key is not None else os.environ.get(_API_KEY_ENV)
        if not resolved_key:
            raise ValueError(
                "AnthropicProvider needs an API key: pass api_key=... or set "
                f"the {_API_KEY_ENV} environment variable"
            )
        self._default_max_tokens = default_max_tokens
        self._device_id = device_id
        self._system_turns = system_turns
        self._whole_timeout = whole_timeout_seconds
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
        began = False
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
                began = True
                async with aclosing(iter_sse_events(response.aiter_lines())) as events:
                    async for event_name, data in events:
                        accumulator.feed(event_name, data)
        except httpx.RequestError as exc:
            # Timeouts and transport failures stay transient so the retry
            # helper applies. Once the stream began, a dropped connection is
            # streamed again within the budget and silence once (Claude
            # Code's counts) before the call goes on in one piece.
            if not began:
                raise transport_error(exc) from exc
            silent = isinstance(exc, httpx.TimeoutException)
            raise StreamBroken(f"Anthropic stream broke: {type(exc).__name__}: {exc}",
                               stream_retries=1 if silent else None) from exc
        return parse_message(accumulator.message_payload(), request.model)

    async def complete_whole(self, request: ModelRequest) -> ModelResponse:
        """``request`` answered in one piece (no streaming), as Claude Code
        sends a call whose stream failed: output at most
        :data:`WHOLE_MAX_TOKENS`, the client timeout the constructor's
        ``whole_timeout_seconds``."""
        guard_vision(request)
        body = self.build_body(request)
        body["max_tokens"] = min(int(body["max_tokens"]), WHOLE_MAX_TOKENS)
        body["stream"] = False
        try:
            response = await self._client.post(
                _MESSAGES_ENDPOINT, json=body, headers=self.request_headers(request) or None,
                timeout=self._whole_timeout)
        except httpx.RequestError as exc:
            raise transport_error(exc) from exc
        if response.is_error:
            raise translate_http_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise Transient(f"Anthropic response was not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise Transient("Anthropic response was not a JSON object")
        return parse_message(payload, request.model)

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
        outbound, last_block, blocks_of = messages_to_wire(
            request.messages, vision, self._system_turns)
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
        apply_cache_control(body, request, last_block, blocks_of)
        bare_strings(outbound)
        if request.stop_sequences:
            body["stop_sequences"] = list(request.stop_sequences)
        body["metadata"] = {"user_id": user_id(self._device_id, request.root_id)}
        if request.output_schema is not None:
            body["output_config"] = {
                "format": {"type": "json_schema", "schema": dict(request.output_schema)}
            }
        thinking = thinking_param(request)
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
