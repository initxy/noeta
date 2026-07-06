"""Minimal SSE (``text/event-stream``) event parser shared by the provider
adapters' streaming paths.

Deliberately transport-blind: it consumes an iterable of already-decoded
text lines (each adapter passes ``httpx.Response.iter_lines()``) and yields
``(event_name, data)`` pairs, one per dispatched SSE event. Everything
vendor-specific — which event names exist, what the JSON payloads mean, the
``[DONE]`` sentinel — stays inside each adapter.

Spec subset implemented (the part all three vendor streams use):

* ``event: <name>`` sets the pending event's name (``None`` when absent —
  OpenAI Chat emits nameless, data-only events).
* ``data: <payload>`` appends one data line; multiple data lines join with
  ``\\n``.
* A blank line dispatches the pending event; events with no accumulated
  data (e.g. a lone ``event:`` line or a comment keep-alive) are skipped.
* Lines starting with ``:`` are comments — ignored.
* ``id:`` / ``retry:`` and unknown fields are ignored (the resume-cursor
  machinery of SSE is irrelevant to a one-shot provider call).
"""

from __future__ import annotations

from typing import Iterable, Iterator, Optional, Tuple


__all__ = ["iter_sse_events"]


def iter_sse_events(
    lines: Iterable[str],
) -> Iterator[Tuple[Optional[str], str]]:
    """Parse decoded ``text/event-stream`` lines into ``(event, data)`` pairs.

    ``event`` is the SSE event name or ``None`` for nameless events; ``data``
    is the joined data payload (never empty — dataless events are skipped).
    A final unterminated event (stream ended without the trailing blank
    line) is still dispatched, matching how the vendors' own SDKs behave on
    a clean-but-unterminated close.
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
        # id / retry / unknown fields: ignored.

    flushed = _flush()
    if flushed is not None:
        yield flushed
