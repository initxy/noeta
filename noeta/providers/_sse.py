"""Minimal SSE (``text/event-stream``) event parser shared by the adapters.

Transport-blind on purpose: it consumes already-decoded text lines and yields
``(event_name, data)`` pairs, leaving everything vendor-specific — which event
names exist, what the payloads mean, the ``[DONE]`` sentinel — inside each
adapter. Only the subset all three vendor streams use is implemented; ``id:``
and ``retry:`` are ignored because SSE's resume-cursor machinery is irrelevant
to a one-shot provider call.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterable

__all__ = ["iter_sse_events"]


async def iter_sse_events(
    lines: AsyncIterable[str],
) -> AsyncGenerator[tuple[str | None, str], None]:
    """Parse decoded ``text/event-stream`` lines into ``(event, data)`` pairs.

    ``event`` is ``None`` for nameless events (OpenAI Chat emits data-only
    frames). Dataless events are dropped, and a final unterminated event —
    stream ended without the trailing blank line — is still dispatched,
    matching how the vendors' own SDKs treat a clean-but-unterminated close.
    """
    event: str | None = None
    data_lines: list[str] = []

    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                yield event, "\n".join(data_lines)
            event = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)

    if data_lines:
        yield event, "\n".join(data_lines)
