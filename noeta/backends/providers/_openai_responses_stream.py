"""Inbound half of the Responses adapter: draining the SSE stream to its
terminal response object, in-stream errors, and the parser from that object
to a :class:`~noeta.core.types.ModelResponse`."""

from __future__ import annotations

import json
from contextlib import aclosing
from typing import Any, Literal

import httpx

from noeta.backends.providers._common import (
    bounded_silence,
    decode_tool_arguments,
    error_dict_is_context_overflow,
)
from noeta.backends.providers._sse import iter_sse_events
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

#: ``Provider.family`` and ``Thinking.family`` for this adapter.
FAMILY = "openai_responses"


def _output_index(payload: dict[str, Any]) -> int:
    """A delta event's ``output_index`` (the item's position in ``output[]``,
    monotonic across the response) → ``Delta.index``; malformed → 0."""
    value = payload.get("output_index")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


async def consume_stream(
    response: httpx.Response, on_delta: OnDelta | None, silence: float
) -> dict[str, Any]:
    """Drain one Responses SSE stream, emitting deltas; return the terminal
    response object. The SSE ``event:`` name is authoritative; the JSON
    ``type`` field is the fallback for nameless frames.

    * ``response.output_text.delta`` → text deltas;
      ``response.reasoning_summary_text.delta`` → thinking deltas.
    * ``response.output_item.done`` of a reasoning item carrying its
      ciphertext → a ``block`` delta with the finished block.
    * ``response.function_call_arguments.delta`` → swallowed: the terminal
      object carries every call with its complete ``arguments``.
    * ``response.completed`` / ``response.incomplete`` / a ``response.failed``
      without an error payload → the carried response object.
    * A ``response.failed`` **with** an error payload, and top-level ``error``
      events → raised through :func:`_translate_stream_error`.
    * Everything else is skipped; vendor stream vocabularies drift.

    A stream that ends without a terminal event is truncated → Transient;
    one silent for ``silence`` seconds raises :class:`httpx.ReadTimeout`.
    """
    final: dict[str, Any] | None = None
    lines = bounded_silence(response.aiter_lines(), silence)
    async with aclosing(iter_sse_events(lines)) as events:
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
            elif event_type == "response.output_item.done":
                item = payload.get("item")
                if (on_delta is not None and isinstance(item, dict)
                        and item.get("type") == "reasoning"
                        and isinstance(item.get("encrypted_content"), str)):
                    # Only a reasoning item with its ciphertext can be sent
                    # back; it goes to the runner whole for a stopped reply.
                    on_delta(Delta(kind="block", index=_output_index(payload),
                                   block=_reasoning_item_to_block(item)))
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


def is_invalid_encrypted_content(response: httpx.Response) -> bool:
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
