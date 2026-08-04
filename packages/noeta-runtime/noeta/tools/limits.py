"""Inline-size budget helpers shared by every tool.

`ToolResult.output` and `ToolResult.summary` ride **inline** in the EventLog and
in the next LLM round's `ToolResultBlock`, so they must stay small *as
canonically encoded*. UTF-8 byte truncation alone is not enough: the ToolRuntime
serialises `output` with stdlib `json.dumps` (`ensure_ascii=True`), so control
chars / non-ASCII can expand a "small" string up to ~6x — these helpers measure
with the same encoding and shrink to a hard ceiling. The full body always lives
in a ContentStore artifact, never inline.
"""

from __future__ import annotations

import json
from typing import Any


#: Ceiling for an inline `ToolResult.output` of summary / list / confirmation
#: tools (``Grep`` / ``Glob`` and kin) whose output is NOT the main content.
#: Wide enough for a full capped result list; overflowing it means "narrow the
#: query" and the tool says so in its truncation notice.
INLINE_OUTPUT_MAX_BYTES = 32 * 1024

#: Ceiling for tools whose output IS the main content the model asked for —
#: ``Read`` / ``WebFetch`` / ``memory`` / ``mcp``. A safety bound against a
#: pathological body, NOT the working truncation rule: each tool truncates by
#: its own model-facing unit first (``Read``'s line window, ``Bash``'s char
#: cap), and this byte ceiling only fences the runaway case. Inline outputs
#: never enter the EventLog payload (the envelope carries ``output_ref``), so
#: widening this does not touch the 4 KB event cap.
INLINE_CONTENT_MAX_BYTES = 1024 * 1024

#: Character cap for ``Bash`` / ``BashOutput`` inline output, matching the
#: reference agent's 30000-char rule. Over-cap output keeps head and tail
#: halves around an elision marker (:func:`elide_middle`) — for a build or
#: test log both the invocation banner and the final failures matter.
SHELL_INLINE_MAX_CHARS = 30000

#: Max raw bytes for a model-supplied string embedded in a `summary`. Small
#: enough that even worst-case JSON-escape expansion stays far under the
#: EventLog 4 KB payload cap.
SUMMARY_EMBED_MAX_BYTES = 100


def elide_middle(value: str, max_chars: int) -> str:
    """Cap ``value`` at ``max_chars`` characters by dropping the middle.

    The marker names the dropped count so the model knows the elision
    happened and how much is missing. Under the cap, the value passes through
    untouched.
    """
    if len(value) <= max_chars:
        return value
    dropped = len(value) - max_chars
    half = max_chars // 2
    return (
        f"{value[:half]}\n... [{dropped} chars truncated] ...\n"
        f"{value[len(value) - (max_chars - half):]}"
    )


def truncate_bytes(value: str, max_bytes: int) -> str:
    """Truncate ``value`` to at most ``max_bytes`` UTF-8 bytes (no split char)."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def encoded_len(obj: Any) -> int:
    """Byte length of ``obj`` under the ToolRuntime's output serialisation
    (stdlib json, sorted keys, compact separators) — matches
    ``noeta.runtime.tool._encode_output``."""
    return len(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def fit_output_fields(
    output: dict[str, Any], *, shrink_order: list[str], max_bytes: int
) -> dict[str, Any]:
    """Return a copy of ``output`` whose canonical encoding is ``<= max_bytes``.

    Halves one shrinkable string field's bytes per pass, in ``shrink_order``,
    moving to the next field once the current one is empty. Terminates: every
    named field can reach empty, and the non-shrinkable remainder (e.g. a
    `source_ref`) is small. Defends against JSON-escape expansion that raw
    per-field byte caps cannot.
    """
    out = dict(output)
    while encoded_len(out) > max_bytes:
        shrunk = False
        for key in shrink_order:
            value = out.get(key)
            if isinstance(value, str) and value:
                out[key] = truncate_bytes(value, len(value.encode("utf-8")) // 2)
                shrunk = True
                break
        if not shrunk:
            break
    return out
