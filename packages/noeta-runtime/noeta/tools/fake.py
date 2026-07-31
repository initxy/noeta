"""FakeTool: a scripted-mapping Tool for tests.

It exercises the real offload path — an output over the EventLog payload
ceiling goes to the ContentStore and comes back as a ``ToolResult.artifacts``
entry with an empty inline ``output`` — so tests see the same two shapes a
production tool produces.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from noeta.protocols.tool import ToolContext, ToolResult


# The EventLog payload cap. Anything strictly larger is offloaded out of the
# inline ``output`` field — the same threshold the ToolRuntime applies when it
# decides where to record the canonical body, so the two stay in lock-step.
INLINE_OUTPUT_LIMIT_BYTES = 4 * 1024
_ARTIFACT_MEDIA_TYPE = "application/octet-stream"


@dataclass
class FakeTool:
    """Deterministic, scripted tool.

    ``script`` maps a value tuple — the call's argument values, sorted by key —
    to the desired output, which may be a string or any JSON-serialisable value.
    ``input_schema`` defaults to the lax ``additionalProperties: True`` object
    schema only because the Tool Protocol requires the attribute; tests override
    it per fixture.
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
        # Key on the values of the sorted-by-key items so a script never depends
        # on the caller's dict iteration order.
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
