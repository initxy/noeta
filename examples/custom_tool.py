"""SDK example — give an agent a custom tool.

Demonstrated SDK capability
---------------------------
The :func:`noeta.tools.tool` decorator. Wrap a plain
``fn(arguments, ctx) -> ToolResult`` function and you get back a single
object that is **both** a runnable tool and a carrier of the matching
identity ref. Drop it into ``Options.allowed_tools`` by value and the SDK
wires the live closure into the session while the ref enters the agent's
declared identity — the runnable and its identity can never drift apart.

Here the model is scripted to call a ``word_count`` tool once; the
example proves the custom closure actually ran by inspecting the
``ToolCallStarted`` / ``ToolResultRecorded`` envelopes.

Running it
----------
Offline by default (:class:`FakeLLMProvider`, no API key). To drive a real
model, swap ``_demo_provider()`` for ``OpenAICompatProvider`` /
``AnthropicProvider`` (see ``minimal_agent.py``) and let the live model
decide when to call the tool.

    python examples/custom_tool.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from noeta.client import Options, query
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools import tool


_WORD_COUNT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


@tool(
    name="word_count",
    version="1",
    risk_level="low",
    input_schema=_WORD_COUNT_SCHEMA,
)
def word_count(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Count whitespace-separated words in ``arguments['text']``.

    A tool is just a function returning a :class:`ToolResult`. ``version``
    is mandatory (no default) because it is part of the tool's declared
    identity — two behaviourally different tools must never share one.
    """
    n = len(str(arguments.get("text", "")).split())
    return ToolResult(success=True, output=f"{n} words")


def _demo_provider() -> FakeLLMProvider:
    """Scripted: call ``word_count`` once, then finish."""
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
    """Drive one turn and return the list of tool names that ran."""
    options = Options(
        system_prompt="You count words when asked.",
        name="counter",
        # Pass the decorated closure by value — that is how a custom tool
        # gets both wired (runnable) and identified (its ref).
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
