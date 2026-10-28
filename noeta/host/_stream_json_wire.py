"""Message shapes of Claude Code's headless protocol for
:class:`~noeta.host.stream_json.StreamJson`: usage and content blocks as
Anthropic writes them, a ``user`` message's content read back, and live
output as Anthropic's streaming events."""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

from noeta.core.types import Delta, Image, Message, Text, Thinking, ToolUse, Usage


def usage_json(u: Usage) -> dict[str, Any]:
    return {"input_tokens": u.uncached, "cache_creation_input_tokens": u.cache_write,
            "cache_read_input_tokens": u.cache_read, "output_tokens": u.output,
            "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
            "service_tier": "standard"}


def block_json(block: Any) -> dict[str, Any] | None:
    if isinstance(block, Text):
        return {"type": "text", "text": block.text}
    if isinstance(block, Thinking):
        return {"type": "thinking", "thinking": block.text, "signature": block.signature or ""}
    if isinstance(block, ToolUse):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, Image):
        return {"type": "image", "source": {"type": "base64", "media_type": block.media_type,
                                            "data": base64.b64encode(block.data).decode()}}
    return None


def message_from(content: Any) -> Message:
    """A ``user`` message's content (a string or Anthropic content blocks)."""
    if isinstance(content, str):
        return Message(role="user", content=[Text(text=content)], origin="human")
    blocks: list[Any] = []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and isinstance(b.get("text"), str):
            blocks.append(Text(text=b["text"]))
        elif b.get("type") == "image" and isinstance(b.get("source"), dict) \
                and b["source"].get("type") == "base64":
            blocks.append(Image(media_type=str(b["source"].get("media_type") or "image/png"),
                                data=base64.b64decode(b["source"].get("data") or "")))
    if not blocks:
        raise ValueError("a user message needs text or image content")
    return Message(role="user", content=blocks, origin="human")


class PartialMessages:
    """Live output (``--include-partial-messages``) as Anthropic's streaming
    events, one message per model call; ``event`` writes one."""

    def __init__(self, event: Callable[[dict[str, Any]], None]) -> None:
        self._event = event
        #: Model call seq -> the attempt being streamed.
        self._streaming: dict[int, int] = {}
        self._open_block = -1

    def delta(self, delta: Delta, model: str) -> None:
        call = delta.call_seq
        if delta.attempt < self._streaming.get(call, 1):
            return  # the tail of an attempt that failed
        if call in self._streaming and delta.attempt > self._streaming[call]:
            # A retry starts over: the failed attempt's message ends unfinished.
            if self._open_block >= 0:
                self._event({"type": "content_block_stop", "index": self._open_block})
            self._event({"type": "message_stop"})
            del self._streaming[call]
        if call not in self._streaming:
            self._streaming[call] = delta.attempt
            self._event({"type": "message_start", "message": {
                "id": f"msg_{call}", "type": "message", "role": "assistant",
                "model": model, "content": [], "stop_reason": None,
                "stop_sequence": None, "usage": usage_json(Usage())}})
            self._open_block = -1
        if delta.index != self._open_block:
            if self._open_block >= 0:
                self._event({"type": "content_block_stop", "index": self._open_block})
            self._open_block = delta.index
            block = {"type": "text", "text": ""} if delta.kind == "text" \
                else {"type": "thinking", "thinking": "", "signature": ""}
            self._event({"type": "content_block_start", "index": delta.index,
                         "content_block": block})
        piece = {"type": "text_delta", "text": delta.text} if delta.kind == "text" \
            else {"type": "thinking_delta", "thinking": delta.text}
        self._event({"type": "content_block_delta", "index": delta.index, "delta": piece})

    def replied(self, call: int, stop_reason: Any, output_tokens: int) -> None:
        """The reply of model call ``call`` is recorded: its streamed
        message, if any, ends."""
        if call not in self._streaming:
            return
        if self._open_block >= 0:
            self._event({"type": "content_block_stop", "index": self._open_block})
            self._open_block = -1
        self._event({"type": "message_delta", "delta": {
            "stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens}})
        self._event({"type": "message_stop"})
