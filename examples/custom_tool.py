"""SDK example — give an agent a custom tool.

Demonstrated SDK capability
---------------------------
The :func:`noeta.sdk.tool` decorator. Wrapping a plain
``fn(arguments, ctx) -> ToolResult`` yields one object that is both the
runnable tool and the carrier of its identity ref, so listing that object by
value in ``Options.allowed_tools`` wires the live closure and declares the
identity from a single definition — the two cannot drift apart.

The provider is scripted so the example needs no API key; pass a live
provider from ``noeta.sdk.providers`` to :func:`run` and a real model decides
for itself when to call the tool.

    python examples/custom_tool.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from noeta.sdk import (
    LLMResponse,
    Options,
    TextBlock,
    ToolContext,
    ToolResult,
    ToolUseBlock,
    Usage,
    query,
    tool,
)
from noeta.sdk.testing import FakeLLMProvider


_WORD_COUNT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


# ``version`` is required rather than defaulted: it is part of the tool's
# declared identity, so a silent default would let two behaviourally different
# tools share one.
@tool(
    name="word_count",
    version="1",
    risk_level="low",
    input_schema=_WORD_COUNT_SCHEMA,
)
def word_count(arguments: dict, ctx: ToolContext) -> ToolResult:
    # ``input_schema`` is model-facing metadata only — nothing validates
    # ``arguments`` against it, so a tool defends itself.
    n = len(str(arguments.get("text", "")).split())
    return ToolResult(success=True, output=f"{n} words")


def _demo_provider() -> FakeLLMProvider:
    """A network-free provider scripted to call ``word_count`` once, then finish."""
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id="wc-1",
                        tool_name="word_count",
                        arguments={"text": "the quick brown fox"},
                    )
                ],
                usage=Usage(uncached=1, output=1),
            ),
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="Counted the words.")],
                usage=Usage(uncached=1, output=1),
            ),
        ]
    )


def run(*, provider=None, workspace_dir: Path, model: str = "stub-model"):
    """Drive one turn and return the tool names the agent actually invoked."""
    options = Options(
        system_prompt="You count words when asked.",
        name="counter",
        # The decorated object, not its name: a bare string would resolve to a
        # built-in and there is no built-in ``word_count``.
        allowed_tools=(word_count,),
        permission_mode="bypassPermissions",
    )
    envelopes = query(
        options,
        goal="How many words are in 'the quick brown fox'?",
        provider=provider if provider is not None else _demo_provider(),
        workspace_dir=workspace_dir,
        model=model,
    )
    return [
        e.payload.tool_name
        for e in envelopes
        if e.type == "ToolCallStarted"
    ]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="noeta-customtool-") as tmp:
        called = run(workspace_dir=Path(tmp))
    print(f"tools the agent called: {called}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
