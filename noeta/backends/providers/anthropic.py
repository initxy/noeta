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
measures silence rather than total generation time. The wait for the
response headers is bounded apart, by Claude Code's request timeout (10
minutes): a gateway that holds the stream until the answer is complete sends
nothing before then, and a silence bound there would retry a long answer
(a compaction summary) until the budget ran out. A stream that breaks
after it began raises :class:`~noeta.core.errors.StreamBroken`, and the
retry helper goes on with :meth:`AnthropicProvider.complete_whole`, Claude
Code's fallback: the same body sent without streaming, its output capped at
64,000 tokens, with a 300 s client timeout.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from contextlib import aclosing
from typing import Any

import httpx

from noeta.backends.providers._anthropic_stream import StreamAccumulator, parse_message
from noeta.backends.providers._anthropic_user_id import user_id
from noeta.backends.providers._anthropic_wire import (
    FAMILY,
    MIN_THINKING_BUDGET,
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
    FIRST_BYTE_TIMEOUT_SECONDS,
    bounded_silence,
    guard_vision,
    model_admits_images,
    stream_timeout,
    translate_http_error,
    transport_error,
)
from noeta.backends.providers._sse import iter_sse_events
from noeta.core import models
from noeta.core.errors import Fatal, StreamBroken, TimedOut
from noeta.core.provider import OnDelta
from noeta.core.types import ModelRequest, ModelResponse

__all__ = ["DEFAULT_BETAS", "FAMILY", "PER_TURN_BETA", "AnthropicProvider", "per_turn_model"]

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
#: The order Claude Code 2.1.312 lists the betas it sends in (the header is a
#: set to the API; a beta a request adds goes in at its place here, one this
#: list lacks at the end).
BETA_ORDER: tuple[str, ...] = (
    "claude-code-20250219", "oauth-2025-04-20", "interleaved-thinking-2025-05-14",
    "redact-thinking-2026-02-12", "context-management-2025-06-27",
    "per-turn-control-2026-07-01", "mid-conversation-tool-changes-2026-07-01",
    "inline-tools-2026-09-15", "advisor-tool-2026-03-01", "thinking-token-count-2026-05-13",
    "advanced-tool-use-2025-11-20", "effort-2025-11-24", "structured-outputs-2025-12-15",
    "fallback-credit-2026-06-01", "dangerous-tool-use-2026-09-03",
    "thinking-display-updates-2026-08-18", "afk-mode-2026-01-31",
    "extended-cache-ttl-2025-04-11",
)
_BETA_RANK = {b: i for i, b in enumerate(BETA_ORDER)}


def _beta_place(have: Sequence[str], beta: str) -> int:
    """Where ``beta`` goes in ``have``: before the first beta Claude Code
    lists after it, else at the end."""
    rank = _BETA_RANK.get(beta)
    if rank is not None:
        for i, other in enumerate(have):
            if _BETA_RANK.get(other, -1) > rank:
                return i
    return len(have)
#: Claude Code sends this beta of its set only for a model whose catalog row
#: has the ``per_turn_effort`` capability (2.1.312: the four below, matched
#: in the id as it matches its Claude models: ``claude-opus-5-5[1m]``,
#: ``us.anthropic.claude-opus-5-5-v1:0``).
PER_TURN_BETA = "per-turn-control-2026-07-01"
PER_TURN_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5",
                   "claude-fable-5-1")


default_effort = models.default_effort


def per_turn_model(model: str) -> bool:
    """Whether Claude Code sends :data:`PER_TURN_BETA` with ``model``."""
    real = models.resolve_alias(model).lower()
    return any(known in real for known in PER_TURN_MODELS)


#: Claude Code's ceilings for a call answered in one piece after a failed
#: stream: its output and its client timeout (seconds).
WHOLE_MAX_TOKENS = 64_000
WHOLE_TIMEOUT_SECONDS = 300.0
#: Client timeout of a token count: an estimate stands in when it passes.
COUNT_TOKENS_TIMEOUT_SECONDS = 10.0


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
        first_byte_timeout_seconds: float = FIRST_BYTE_TIMEOUT_SECONDS,
    ) -> None:
        """``api_key`` defaults to the ``ANTHROPIC_API_KEY`` environment
        variable; an explicit argument still wins. Missing credentials raise
        here rather than yielding a client that fails its first call with an
        opaque 401. ``timeout_seconds`` bounds silence on the stream, not the
        whole generation; ``first_byte_timeout_seconds`` the wait for its
        response headers. ``betas`` is the ``anthropic-beta`` header (an
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
        self._silence = timeout_seconds
        self._first_byte_timeout = first_byte_timeout_seconds
        #: Models whose token count the endpoint refused (:meth:`count_tokens`).
        self._count_unavailable: set[str] = set()
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
        #: Claude Code's set, so its per-model rule applies (a set the host
        #: chose is sent as given).
        self._default_betas = tuple(betas) == DEFAULT_BETAS \
            and "anthropic-beta" not in (extra_headers or {})
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
        given = headers.get("anthropic-beta", self._betas)
        have = [b for b in given.split(",") if b]
        if self._default_betas and "anthropic-beta" not in request.headers \
                and not per_turn_model(request.model):
            have = [b for b in have if b != PER_TURN_BETA]
        for beta in request.betas:
            if beta not in have:
                have.insert(_beta_place(have, beta), beta)
        if ",".join(have) != given:
            headers["anthropic-beta"] = ",".join(have)
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
                timeout=stream_timeout(self._silence, self._first_byte_timeout),
            ) as response:
                if response.is_error:
                    # An unread streaming response cannot be sniffed for the
                    # overflow markers, so the (small) error body is read first.
                    await response.aread()
                    raise translate_http_error(response)
                began = True
                lines = bounded_silence(response.aiter_lines(), self._silence)
                async with aclosing(iter_sse_events(lines)) as events:
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
        :data:`WHOLE_MAX_TOKENS`, the whole call (not each read) bounded by
        the constructor's ``whole_timeout_seconds`` (:class:`TimedOut` past
        it). A body that is not a JSON message (a proxy's HTML error page)
        is :class:`Fatal`, as Claude Code does not retry it."""
        guard_vision(request)
        body = self.build_body(request)
        body["max_tokens"] = min(int(body["max_tokens"]), WHOLE_MAX_TOKENS)
        thinking = body.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            # The budget stays under the lower cap (Claude Code's), and
            # thinking goes when the cap leaves no room for the API's least.
            budget = min(int(thinking["budget_tokens"]), body["max_tokens"] - 1)
            if budget < MIN_THINKING_BUDGET:
                del body["thinking"]
            else:
                body["thinking"] = {**thinking, "budget_tokens": budget}
        body["stream"] = False
        try:
            response = await asyncio.wait_for(self._client.post(
                _MESSAGES_ENDPOINT, json=body, headers=self.request_headers(request) or None,
                timeout=self._whole_timeout), self._whole_timeout)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise TimedOut(f"Anthropic call in one piece got no answer within "
                           f"{self._whole_timeout:g} s") from exc
        except httpx.RequestError as exc:
            raise transport_error(exc) from exc
        if response.is_error:
            raise translate_http_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise Fatal(_not_a_message(response), status=response.status_code,
                        body=response.text[:2000]) from exc
        if not isinstance(payload, dict):
            raise Fatal(_not_a_message(response), status=response.status_code,
                        body=response.text[:2000])
        return parse_message(payload, request.model)

    async def count_tokens(self, model: str, text: str) -> int | None:
        """The exact input-token count of one user message holding ``text``
        (``/v1/messages/count_tokens``, as Claude Code measures a large tool
        result); ``None`` when the endpoint gives no count. The request is
        bounded by :data:`COUNT_TOKENS_TIMEOUT_SECONDS`, and a model the
        endpoint refused (a gateway without it: a 4xx other than 429, or a
        body with no count) is not asked again by this provider."""
        if not text:
            return 0
        if model in self._count_unavailable:
            return None
        body = {"model": model, "messages": [{"role": "user", "content": text}], "tools": []}
        betas = [b for b in self._client.headers.get("anthropic-beta", "").split(",") if b]
        try:
            response = await self._client.post(
                _COUNT_TOKENS_ENDPOINT, json=body,
                headers={"anthropic-beta": ",".join([*betas, _COUNT_TOKENS_BETA])},
                timeout=COUNT_TOKENS_TIMEOUT_SECONDS)
        except httpx.RequestError:
            return None
        if response.is_error:
            if 400 <= response.status_code < 500 and response.status_code != 429:
                self._count_unavailable.add(model)
            return None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        count = payload.get("input_tokens") if isinstance(payload, dict) else None
        if not isinstance(count, int):
            self._count_unavailable.add(model)
            return None
        return count

    # ------------------------------------------------------------------
    # Outbound translation
    # ------------------------------------------------------------------

    def build_body(self, request: ModelRequest) -> dict[str, Any]:
        """The wire body for ``request`` (without ``stream``). Public so tests
        and diagnostics can see exactly what would be sent."""
        vision = model_admits_images(request.model)
        outbound, last_block, blocks_of = messages_to_wire(
            request.messages, vision, self._system_turns,
            tools=[t.name for t in request.tools])
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
        thinking = thinking_param(request, int(body["max_tokens"]))
        if thinking is not None:
            body["thinking"] = thinking
        adaptive = thinking is not None and thinking["type"] == "adaptive"
        if adaptive:
            body["context_management"] = {
                "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}
        # Effort rides inside output_config, alongside any format set above;
        # every level (xhigh and max included) passes through verbatim.
        effort = request.effort or (default_effort(request.model) if adaptive else None)
        if effort is not None:
            body.setdefault("output_config", {})["effort"] = effort
        for key, value in request.vendor_fields.items():
            body.setdefault(key, value)
        return body

    def _resolve_max_tokens(self, request: ModelRequest) -> int:
        """Request beats constructor default beats the catalogued default
        output (``ModelSpec.output_default``); none of them → :class:`Fatal`. Anthropic requires
        ``max_tokens``, and a cap this adapter invented would truncate answers
        with no signal, so it never picks one."""
        if request.max_tokens is not None:
            return int(request.max_tokens)
        if self._default_max_tokens is not None:
            return int(self._default_max_tokens)
        spec = models.find_spec(request.model)
        if spec is not None:
            return spec.output_default
        raise Fatal(
            "Anthropic requires max_tokens; set ModelRequest.max_tokens, pass "
            "AnthropicProvider(default_max_tokens=...), or register the model "
            "in noeta.core.models"
        )


def _not_a_message(response: httpx.Response) -> str:
    """The error for a one-piece answer whose body is not a JSON message."""
    kind = response.headers.get("content-type", "").split(";")[0].strip() or "no content type"
    return (f"Anthropic answered HTTP {response.status_code} with a body that is not a "
            f"JSON message ({kind}): a proxy or gateway may have intercepted the request")
