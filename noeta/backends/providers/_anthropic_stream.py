"""Inbound half of the Anthropic adapter: the SSE accumulator that rebuilds
the vendor-shaped ``message`` payload, and the parser from that payload to a
:class:`~noeta.core.types.ModelResponse`.

Rebuilding the batch-shaped payload first (rather than assembling the response
directly from events) keeps one parser, so every consistency check applies to
whatever the stream carried.
"""

from __future__ import annotations

import json
from typing import Any

from noeta.backends.providers._common import indicates_context_overflow
from noeta.core.errors import (
    ContextOverflow,
    Fatal,
    MalformedToolArguments,
    ModelError,
    RateLimited,
    Transient,
)
from noeta.core.provider import OnDelta
from noeta.core.types import (
    Block,
    Delta,
    Message,
    ModelResponse,
    StopReason,
    Text,
    Thinking,
    ToolUse,
    Usage,
)

__all__ = ["StreamAccumulator", "parse_message", "translate_usage"]

_FAMILY = "anthropic"

#: The stop_reason a current model returns when the *input* overflowed the
#: context window on an otherwise-successful HTTP 200 turn. It is the same
#: failure the 400 body sniffer catches, arriving over a different transport,
#: so it raises the same :class:`ContextOverflow` — left unrecognised it would
#: kill the task exactly where compaction would have rescued it.
_OVERFLOW_STOP_REASON = "model_context_window_exceeded"

_STOP_REASON_MAP: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "end_turn",
    # A safety-classifier refusal is a completed HTTP-200 turn whose content
    # carries the refusal text; it reaches the caller as such.
    "refusal": "refusal",
    # ``pause_turn`` is absent on purpose — no server-side tools are wired, so
    # it is unreachable, and calling it ``end_turn`` would silently truncate a
    # turn the API expects to be resumed. Unknown values are Fatal.
}


def translate_usage(usage_raw: Any) -> Usage:
    """Anthropic usage → :class:`Usage`. ``input_tokens`` maps straight to
    ``uncached`` because Anthropic already excludes the cache buckets from it
    and bills them separately — the opposite of OpenAI, whose prompt count is
    a total the cached part must be subtracted from. Anthropic reports no
    reasoning-token field, so that stays 0; a missing or non-dict ``usage``
    degrades to an empty ``Usage()``."""
    if not isinstance(usage_raw, dict):
        return Usage()
    return Usage(
        uncached=int(usage_raw.get("input_tokens", 0) or 0),
        cache_read=int(usage_raw.get("cache_read_input_tokens", 0) or 0),
        cache_write=int(usage_raw.get("cache_creation_input_tokens", 0) or 0),
        output=int(usage_raw.get("output_tokens", 0) or 0),
    )


def parse_message(payload: dict[str, Any], model: str = "") -> ModelResponse:
    """A complete vendor-shaped ``message`` payload → :class:`ModelResponse`."""
    if payload.get("type") != "message":
        raise Fatal(
            f"Anthropic response 'type' was not 'message': got {payload.get('type')!r}"
        )
    if payload.get("role") != "assistant":
        raise Fatal(
            f"Anthropic response 'role' was not 'assistant': got {payload.get('role')!r}"
        )
    content_raw = payload.get("content")
    if not isinstance(content_raw, list):
        raise Fatal(
            "Anthropic response 'content' must be a list: "
            f"got type={type(content_raw).__name__}"
        )
    content = _parse_content(content_raw)

    raw_stop = payload.get("stop_reason")
    if raw_stop == _OVERFLOW_STOP_REASON:
        raise ContextOverflow(
            f"Anthropic stop_reason {_OVERFLOW_STOP_REASON!r}: the input "
            "exceeded the model's context window"
        )
    stop_reason = _STOP_REASON_MAP.get(raw_stop or "")
    if stop_reason is None:
        raise Fatal(f"Anthropic response has unsupported stop_reason {raw_stop!r}")
    if stop_reason == "refusal":
        # A refusal can interrupt a tool call mid-generation. The call never
        # completes and must not run, so the turn ends on whatever text came
        # with it.
        content = [b for b in content if not isinstance(b, ToolUse)]

    has_tool_use = any(isinstance(b, ToolUse) for b in content)
    if stop_reason == "tool_use" and not has_tool_use:
        raise Fatal(
            "inconsistent Anthropic response: stop_reason='tool_use' but "
            "content has no tool_use block"
        )
    if stop_reason == "end_turn" and has_tool_use:
        raise Fatal(
            "inconsistent Anthropic response: stop_reason='end_turn' but "
            "content has tool_use block(s)"
        )
    returned_model = payload.get("model")
    return ModelResponse(
        message=Message(role="assistant", content=content),
        stop_reason=stop_reason,
        usage=translate_usage(payload.get("usage")),
        model=returned_model if isinstance(returned_model, str) else model,
    )


def _parse_content(content_raw: list[Any]) -> list[Block]:
    """Response content blocks → blocks. Unknown block types are skipped to
    stay forward-compatible with new Anthropic block types."""
    blocks: list[Block] = []
    for entry in content_raw:
        if not isinstance(entry, dict):
            raise Fatal(f"Anthropic content entry not a dict: {entry!r}")
        entry_type = entry.get("type")
        if entry_type == "text":
            text = entry.get("text", "")
            if not isinstance(text, str):
                raise Fatal(f"Anthropic 'text' block 'text' not a str: {entry!r}")
            # A text block with no visible text (a streamed block that started
            # and stopped with no ``text_delta``, or a whitespace-only one)
            # carries nothing, and resending it is a 400 ("text content blocks
            # must be non-empty").
            if text.strip():
                blocks.append(Text(text=text))
        elif entry_type == "thinking":
            text = entry.get("thinking", "")
            signature = entry.get("signature")
            blocks.append(
                Thinking(
                    text=text if isinstance(text, str) else "",
                    signature=signature if isinstance(signature, str) else None,
                    family=_FAMILY,
                )
            )
        elif entry_type == "redacted_thinking":
            # Encrypted reasoning the safety system redacted. Nothing is
            # human-readable, but the opaque ``data`` blob MUST round-trip
            # verbatim on the next request (a tool-use turn that carried
            # thinking is rejected if its reasoning blocks are missing). With
            # no blob there is nothing to round-trip, and an empty thinking
            # block would be rejected outbound, so it is dropped.
            data = entry.get("data")
            if isinstance(data, str):
                blocks.append(Thinking(text="", data=data, family=_FAMILY))
        elif entry_type == "tool_use":
            call_id = entry.get("id", "")
            name = entry.get("name", "")
            arguments = entry.get("input", {})
            if not isinstance(arguments, dict):
                raise MalformedToolArguments(
                    f"Anthropic 'tool_use.input' not a JSON object: {entry!r}"
                )
            blocks.append(
                ToolUse(
                    id=call_id if isinstance(call_id, str) else "",
                    name=name if isinstance(name, str) else "",
                    input=arguments,
                )
            )
    return blocks


#: In-band SSE ``error`` event types that are worth retrying — the same
#: failure classes their HTTP-status counterparts land in (429 / 500 / 529).
_TRANSIENT_STREAM_ERROR_TYPES: frozenset[str] = frozenset(
    {"api_error", "overloaded_error"}
)


def _translate_stream_error_event(payload: dict[str, Any]) -> ModelError:
    """Map an in-band SSE ``error`` event into the neutral taxonomy.

    The 200 header is already on the wire by then, so mid-stream failures
    arrive as ``event: error`` frames and this classifies on the error *type*.
    A frame carries no headers, hence no ``retry_after`` — the retry helper
    falls back to exponential backoff.
    """
    error = payload.get("error")
    if not isinstance(error, dict):
        error = {}
    error_type = str(error.get("type") or "")
    message = str(error.get("message") or "")
    described = f"Anthropic stream error event: {error_type}: {message}"
    if error_type == "rate_limit_error":
        return RateLimited(described)
    if error_type in _TRANSIENT_STREAM_ERROR_TYPES:
        return Transient(described)
    if error_type in (
        "invalid_request_error",
        "request_too_large",
    ) and indicates_context_overflow(message):
        return ContextOverflow(described)
    return Fatal(described)


class StreamAccumulator:
    """Rebuilds the vendor-shaped ``message`` payload from Messages-API stream
    events, firing :class:`Delta` side effects along the way.

    Only ``text_delta`` / ``thinking_delta`` fragments surface as deltas,
    plus each finished reasoning block whole (``kind="block"``).
    ``input_json_delta`` (tool arguments) and ``signature_delta`` accumulate
    **silently** — argument JSON is undecodable while partial and the
    signature is an opaque continuation token. ``redacted_thinking`` blocks
    arrive whole on ``content_block_start``. Unknown event / delta types are
    skipped, mirroring the parser's forward-compatibility stance.
    """

    def __init__(self, on_delta: OnDelta | None) -> None:
        self._on_delta = on_delta
        self._message: dict[str, Any] | None = None
        self._blocks: dict[int, dict[str, Any]] = {}
        self._json_fragments: dict[int, list[str]] = {}
        #: ``tool_use`` blocks whose argument JSON did not decode, with the
        #: decode error. Judged at assembly: cut off by ``max_tokens`` they are
        #: dropped, otherwise the stream is corrupt and the error is raised.
        self._undecodable: dict[int, str] = {}

    def _emit(self, delta: Delta) -> None:
        if self._on_delta is not None:
            self._on_delta(delta)

    def feed(self, event_name: str | None, data: str) -> None:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise Fatal(f"Anthropic stream event was not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise Fatal(
                "Anthropic stream event root was not a JSON object: "
                f"type={type(payload).__name__}"
            )
        if event_name == "message_start":
            message = payload.get("message")
            self._message = dict(message) if isinstance(message, dict) else {}
        elif event_name == "content_block_start":
            self._start_block(payload)
        elif event_name == "content_block_delta":
            self._apply_delta(payload)
        elif event_name == "content_block_stop":
            index = payload.get("index")
            if isinstance(index, int):
                self._finalize_block(index)
                self._emit_finished_reasoning(index)
        elif event_name == "message_delta":
            self._apply_message_delta(payload)
        elif event_name == "error":
            raise _translate_stream_error_event(payload)
        # message_stop / ping / unknown event types: nothing to accumulate.

    def _start_block(self, payload: dict[str, Any]) -> None:
        index = payload.get("index")
        block = payload.get("content_block")
        if not isinstance(index, int) or not isinstance(block, dict):
            return
        # Copied because the dict is mutated as deltas accumulate.
        self._blocks[index] = dict(block)
        if block.get("type") == "tool_use":
            self._json_fragments[index] = []

    def _apply_delta(self, payload: dict[str, Any]) -> None:
        index = payload.get("index")
        delta = payload.get("delta")
        if not isinstance(index, int) or not isinstance(delta, dict):
            return
        block = self._blocks.get(index)
        if block is None:
            # A delta for a block that never started: skip the accumulation
            # AND the preview together, so the emitted deltas never disagree
            # with the final content.
            return
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            fragment = delta.get("text")
            if isinstance(fragment, str):
                block["text"] = str(block.get("text") or "") + fragment
                self._emit(Delta(kind="text", text=fragment, index=index))
        elif delta_type == "thinking_delta":
            fragment = delta.get("thinking")
            if isinstance(fragment, str):
                block["thinking"] = str(block.get("thinking") or "") + fragment
                self._emit(Delta(kind="thinking", text=fragment, index=index))
        elif delta_type == "input_json_delta":
            fragment = delta.get("partial_json")
            if isinstance(fragment, str):
                self._json_fragments.setdefault(index, []).append(fragment)
        elif delta_type == "signature_delta":
            fragment = delta.get("signature")
            if isinstance(fragment, str):
                block["signature"] = str(block.get("signature") or "") + fragment

    def _finalize_block(self, index: int) -> None:
        """Decode a ``tool_use`` block's accumulated argument fragments. An
        empty accumulation keeps the ``input`` the block started with (``{}``
        — a no-argument call). A decode failure is only noted here: whether it
        is a truncation or a corrupt stream depends on the stop reason, which
        arrives later. Idempotent: fragments are popped."""
        fragments = self._json_fragments.pop(index, None)
        if not fragments:
            return
        joined = "".join(fragments)
        if not joined.strip():
            return
        block = self._blocks.get(index)
        if block is None:
            return
        try:
            block["input"] = json.loads(joined)
        except json.JSONDecodeError as exc:
            self._undecodable[index] = str(exc)

    def _emit_finished_reasoning(self, index: int) -> None:
        """A reasoning block that just ended goes to the runner whole (a
        ``block`` delta): a reply stopped later keeps it. One with nothing in
        it is not sent; a reply that ends normally carries it anyway."""
        block = self._blocks.get(index)
        if block is None or block.get("type") not in ("thinking", "redacted_thinking"):
            return
        for parsed in _parse_content([block]):
            if isinstance(parsed, Thinking) and (parsed.text or parsed.signature or parsed.data):
                self._emit(Delta(kind="block", index=index, block=parsed))

    def _apply_message_delta(self, payload: dict[str, Any]) -> None:
        """``message_delta``: the terminal stop signal + output-side usage.
        Usage keys merge over the ``message_start`` snapshot — the input side
        is reported up front and the (cumulative) output side here."""
        if self._message is None:
            return
        delta = payload.get("delta")
        if isinstance(delta, dict):
            for key in ("stop_reason", "stop_sequence", "stop_details"):
                if key in delta:
                    self._message[key] = delta.get(key)
        usage = payload.get("usage")
        if isinstance(usage, dict):
            merged = self._message.get("usage")
            merged = dict(merged) if isinstance(merged, dict) else {}
            merged.update(usage)
            self._message["usage"] = merged

    def message_payload(self) -> dict[str, Any]:
        """The reconstructed vendor-shaped ``message`` dict."""
        if self._message is None:
            raise Fatal("Anthropic stream ended without a message_start event")
        # ``message_delta`` carries the stop reason; a stream that closed
        # cleanly without one was cut mid-generation. That is the same failure
        # as a dropped connection — retryable — not a finished turn.
        if self._message.get("stop_reason") is None:
            raise Transient("Anthropic stream ended before message_delta (no stop_reason)")
        # Decode any tool_use block whose content_block_stop never arrived.
        for index in list(self._json_fragments):
            self._finalize_block(index)
        if self._undecodable:
            if self._message.get("stop_reason") != "max_tokens":
                first = self._undecodable[min(self._undecodable)]
                raise MalformedToolArguments(
                    f"Anthropic streamed tool_use input was not valid JSON: {first}"
                )
            # The output cap cut the call's arguments mid-JSON: keep the
            # complete calls and report the truncation as ``max_tokens``.
            for index in self._undecodable:
                self._blocks.pop(index, None)
        self._message["content"] = [self._blocks[i] for i in sorted(self._blocks)]
        return self._message
