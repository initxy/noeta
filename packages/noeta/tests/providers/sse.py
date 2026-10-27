"""SSE fixture builders for the provider tests.

Every adapter streams, so a fixture that describes a whole response (the
shape a batch API would return) is re-encoded here as the stream that
produces it. Each builder is a pure re-encoding: what the adapter rebuilds
from the frames is the payload the fixture describes. Fragmented delivery
(text split across many deltas, tool arguments in pieces) is written out by
the streaming tests themselves with :func:`sse_bytes`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx

__all__ = [
    "RaisingStream",
    "anthropic_sse",
    "chat_sse",
    "responses_sse",
    "sse_bytes",
    "sse_response",
]


def sse_bytes(frames: Iterable[tuple[str | None, Any]]) -> bytes:
    """``(event_name, payload)`` frames → raw ``text/event-stream`` bytes. A
    ``None`` name writes a data-only frame; a ``str`` payload is written as is
    (``"[DONE]"``), anything else JSON-encoded."""
    out: list[str] = []
    for name, payload in frames:
        data = payload if isinstance(payload, str) else json.dumps(payload)
        head = f"event: {name}\n" if name is not None else ""
        out.append(f"{head}data: {data}\n\n")
    return "".join(out).encode("utf-8")


def sse_response(body: bytes, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def anthropic_sse(message: dict[str, Any]) -> bytes:
    """A complete Anthropic ``message`` dict → the SSE frames that produce it.
    Each content block is delivered whole on its ``content_block_start``; the
    stop reason (and usage) ride ``message_delta`` the way the API sends them.
    """
    head = {k: v for k, v in message.items() if k not in ("content", "stop_reason")}
    head["stop_reason"] = None
    frames: list[tuple[str | None, Any]] = [
        ("message_start", {"type": "message_start", "message": head})
    ]
    for index, block in enumerate(message.get("content") or []):
        frames.append(
            (
                "content_block_start",
                {"type": "content_block_start", "index": index, "content_block": block},
            )
        )
        frames.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
    delta: dict[str, Any] = {"type": "message_delta", "delta": {}}
    if "stop_reason" in message:
        delta["delta"]["stop_reason"] = message["stop_reason"]
    frames.append(("message_delta", delta))
    frames.append(("message_stop", {"type": "message_stop"}))
    return sse_bytes(frames)


# ---------------------------------------------------------------------------
# OpenAI Chat Completions
# ---------------------------------------------------------------------------


def chat_sse(payload: dict[str, Any], *, done: bool = True) -> bytes:
    """A batch Chat Completions payload → the chunk stream that rebuilds it:
    reasoning, content and each tool call as one delta chunk apiece, then the
    ``finish_reason`` chunk, a usage-only chunk, and ``[DONE]``."""
    meta = {k: payload[k] for k in ("id", "model") if k in payload}
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    chunks: list[dict[str, Any]] = []

    def chunk(delta: dict[str, Any], finish: str | None = None) -> None:
        chunks.append(
            {**meta, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        )

    for key in ("reasoning_content", "reasoning", "encrypted_reasoning"):
        if isinstance(message.get(key), str) and message[key]:
            chunk({key: message[key]})
    if isinstance(message.get("content"), str) and message["content"]:
        chunk({"content": message["content"]})
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        chunk(
            {
                "tool_calls": [
                    {
                        "index": index,
                        "id": call.get("id"),
                        "type": "function",
                        "function": {
                            "name": function.get("name"),
                            "arguments": function.get("arguments"),
                        },
                    }
                ]
            }
        )
    chunk({}, choice.get("finish_reason"))
    frames: list[tuple[str | None, Any]] = [(None, c) for c in chunks]
    if "usage" in payload:
        frames.append((None, {**meta, "choices": [], "usage": payload["usage"]}))
    if done:
        frames.append((None, "[DONE]"))
    return sse_bytes(frames)


# ---------------------------------------------------------------------------
# OpenAI Responses
# ---------------------------------------------------------------------------


def responses_sse(response: dict[str, Any], *, event: str | None = None) -> bytes:
    """A complete Responses response object → ``response.created`` plus the
    terminal event carrying it (``response.completed`` by default,
    ``response.incomplete`` when the status says so)."""
    if event is None:
        status = response.get("status")
        event = {"incomplete": "response.incomplete", "failed": "response.failed"}.get(
            status or "", "response.completed"
        )
    return sse_bytes(
        [
            (
                "response.created",
                {"type": "response.created", "response": {"status": "in_progress"}},
            ),
            (event, {"type": event, "response": response}),
        ]
    )


# ---------------------------------------------------------------------------
# Mid-stream failure
# ---------------------------------------------------------------------------


class RaisingStream(httpx.AsyncByteStream):
    """Yields ``chunks`` then raises ``exc`` — a connection dropping mid-body."""

    def __init__(self, chunks: list[bytes], exc: Exception) -> None:
        self._chunks = chunks
        self._exc = exc

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for c in self._chunks:
            yield c
        raise self._exc
