"""Inbound half of the Chat Completions adapter: the stream accumulator that
folds chunks back into the batch-shaped payload, and the parser from that
payload to a :class:`~noeta.core.types.ModelResponse` (one parser, so
streamed and non-streamed semantics cannot drift)."""

from __future__ import annotations

from typing import Any, Literal

from noeta.backends.providers._common import decode_tool_arguments
from noeta.core.errors import Fatal, MalformedToolArguments
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
FAMILY = "openai_compat"

_FINISH_REASON_MAP: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}

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


class ChatStreamAccumulator:
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
