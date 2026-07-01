"""FakeTool: scripted-mapping tool for tests.

The Tool resolves a call by hashing its arguments into a key and looking
up a predetermined output. If the output exceeds the EventLog payload
ceiling (SDD §Data), the body is offloaded to ContentStore
via the supplied ``ToolContext.artifact_store`` and surfaced as a
``ToolResult.artifacts`` entry; the inline ``output`` field is left empty
in that case.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from noeta.protocols.tool import ToolContext, ToolResult


# The EventLog payload cap is 4 KB. We offload anything
# strictly larger than this from the inline ``output`` field. The same
# constant is what the ToolRuntime uses to decide where to record the
# canonical body, so they stay in lock-step.
INLINE_OUTPUT_LIMIT_BYTES = 4 * 1024
_ARTIFACT_MEDIA_TYPE = "application/octet-stream"


@dataclass
class FakeTool:
    """Deterministic, scripted tool.

    ``script`` is a mapping from one positional value tuple (the values
    of the call's arguments, sorted by key) to the desired output. The
    scripted ``output`` may be a string or any JSON-serialisable value.

    ``input_schema`` defaults to the lax ``additionalProperties: True``
    object schema; the :class:`noeta.protocols.tool.Tool`
    Protocol requires the attribute as LLM-facing metadata. Tests can
    override per fixture.
    """

    name: str = "fake"
    description: str = (
        "Deterministic scripted test tool that returns pre-configured outputs "
        "keyed by argument values."
    )
    risk_level: str = "low"
    summary_prefix: str = ""
    script: dict[tuple[Any, ...], Any] = field(default_factory=dict)
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "additionalProperties": True}
    )

    def invoke(
        self, arguments: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        # Tests script by argument *values* keyed in insertion order.
        # We compare by the value-tuple of the sorted-by-key items so
        # callers do not need to depend on dict iteration order.
        values_key = tuple(v for _, v in sorted(arguments.items()))
        if values_key not in self.script:
            return ToolResult(
                success=False,
                summary=f"{self.summary_prefix}no script entry for {values_key!r}",
            )

        output = self.script[values_key]
        body = _encode(output)
        if len(body) > INLINE_OUTPUT_LIMIT_BYTES:
            ref = ctx.artifact_store.put(body, media_type=_ARTIFACT_MEDIA_TYPE)
            return ToolResult(
                success=True,
                output=None,
                artifacts=[ref],
                summary=f"{self.summary_prefix}artifact ({ref.size}B)",
            )
        return ToolResult(
            success=True,
            output=output,
            summary=f"{self.summary_prefix}ok",
        )


def _encode(output: Any) -> bytes:
    """Encode a tool output to bytes for size-checking and storage.

    Strings round-trip as UTF-8; anything else is canonical JSON so the
    size comparison and any subsequent ContentRef hash are stable.
    """
    if isinstance(output, (bytes, bytearray)):
        return bytes(output)
    if isinstance(output, str):
        return output.encode("utf-8")
    return json.dumps(output, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
