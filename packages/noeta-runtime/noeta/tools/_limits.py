"""Inline-size budget helpers shared by every tool pack.

`ToolResult.output` and `ToolResult.summary` ride **inline** in the
EventLog (`ToolResultRecordedPayload` stores `summary` and the
`output_ref`, and `output` becomes the next LLM round's `ToolResultBlock`),
so they must stay small *as canonically encoded*. UTF-8 byte truncation
alone is not enough: the ToolRuntime serialises `output` with stdlib
`json.dumps` (`ensure_ascii=True`), so control chars / non-ASCII can expand
a "small" string up to ~6x. These helpers measure with the same encoding
and shrink to a hard ceiling. The full body always lives in a ContentStore
artifact, never inline.

Promoted from `noeta.tools.research._limits` (Phase 4 B16) so the fs tool
pack and any future pack can share the same byte-budget seam without
importing into another pack's private module.
"""

from __future__ import annotations

import json
from typing import Any


#: Hard ceiling for an inline `ToolResult.output`, canonically encoded.
#: Used by summary / list / confirmation tools (grep, glob, edit, patch) whose
#: inline output is NOT the main content — a tight budget keeps the context
#: lean and signals "narrow your query" when results overflow.
INLINE_OUTPUT_MAX_BYTES = 4096

#: Wider ceiling for tools whose output IS the main content the model asked for
#: — ``read`` / ``shell_run`` / ``webfetch`` / ``memory`` / ``mcp``. These are
#: "show me the file / log / page / tool result" tools, so a 4 KB excerpt forces
#: wasteful re-reads; 64 KB lets a mid-size body land in one shot. The full body
#: is always offloaded to the ContentStore (a ``*_ref``), so this is a
#: context-budget knob (how much rides inline to the next LLM turn), NOT a
#: persistence limit — the EventLog payload only ever carries the ref, never the
#: body, so widening this cannot breach the 4 KB envelope cap.
INLINE_CONTENT_MAX_BYTES = 64 * 1024

#: Max raw bytes for a model-supplied string embedded in a `summary`. Small
#: enough that even worst-case JSON-escape expansion stays far under the
#: EventLog 4 KB payload cap.
SUMMARY_EMBED_MAX_BYTES = 100


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
