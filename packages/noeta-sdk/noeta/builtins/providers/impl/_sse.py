"""Minimal SSE (``text/event-stream``) event parser shared by the provider
adapters' streaming paths.

Transport-blind on purpose: it consumes already-decoded text lines and yields
``(event_name, data)`` pairs, leaving everything vendor-specific — which event
names exist, what the payloads mean, the ``[DONE]`` sentinel — inside each
adapter. Only the subset all three vendor streams use is implemented; ``id:``
and ``retry:`` are ignored because SSE's resume-cursor machinery is irrelevant
to a one-shot provider call.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Optional, Tuple


__all__ = ["iter_sse_events"]


def iter_sse_events(
    lines: Iterable[str],
) -> Iterator[Tuple[Optional[str], str]]:
    """Parse decoded ``text/event-stream`` lines into ``(event, data)`` pairs.

    ``event`` is ``None`` for nameless events (OpenAI Chat emits data-only
    frames). Dataless events are dropped, and a final unterminated event —
    stream ended without the trailing blank line — is still dispatched,
    matching how the vendors' own SDKs treat a clean-but-unterminated close.
    """
    event: Optional[str] = None
    data_lines: list[str] = []

    def _flush() -> Optional[Tuple[Optional[str], str]]:
        nonlocal event, data_lines
        if not data_lines:
            event = None
            return None
        out = (event, "\n".join(data_lines))
        event = None
        data_lines = []
        return out

    for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            flushed = _flush()
            if flushed is not None:
                yield flushed
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)

    flushed = _flush()
    if flushed is not None:
        yield flushed
