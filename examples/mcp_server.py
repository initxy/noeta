"""SDK example — expose tools through an in-process MCP server.

Demonstrated SDK capability
---------------------------
:func:`noeta.sdk.create_sdk_mcp_server`. Bundle a set of ``@tool`` functions
into a single named, in-process ("sdk" transport) MCP server, then mount the
bundle on ``Options.mcp_servers``. This is the noeta analogue of
claude-agent-sdk's ``create_sdk_mcp_server``: the tools run in the host process
— no subprocess to spawn, no network round-trip — and the agent calls them by
name like any other tool.

Where ``custom_tool.py`` mounts one loose tool via ``Options.allowed_tools``,
this groups several related tools under one server value object, so a whole
toolbox travels (and is identified) as a unit.

Here the model is scripted to call the bundled ``echo`` tool once; the example
proves the closure ran by inspecting the ``ToolCallStarted`` envelopes.

Running it
----------
Offline by default (:class:`FakeLLMProvider`, no API key). To drive a real
model, swap ``_demo_provider()`` for ``OpenAICompatProvider`` /
``AnthropicProvider`` (see ``minimal_agent.py``) and let the live model decide
when to call the tools.

    python examples/mcp_server.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.sdk import Options, create_sdk_mcp_server, query, tool
from noeta.testing.fake_llm import FakeLLMProvider


_TEXT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


@tool(name="echo", version="1", risk_level="low", input_schema=_TEXT_SCHEMA)
def echo(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Return the input text unchanged."""
    return ToolResult(success=True, output=str(arguments.get("text", "")))


@tool(name="shout", version="1", risk_level="low", input_schema=_TEXT_SCHEMA)
def shout(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Return the input text upper-cased."""
    return ToolResult(success=True, output=str(arguments.get("text", "")).upper())


# Bundle both tools into one named, in-process MCP server. The returned
# SdkMcpServer is a frozen value object — hand it to Options.mcp_servers.
TOOLBOX = create_sdk_mcp_server("toolbox", version="1.0.0", tools=[echo, shout])


def _demo_provider() -> FakeLLMProvider:
    """Scripted: call the bundled ``echo`` tool once, then finish."""
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id="e-1",
                        tool_name="echo",
                        arguments={"text": "hello from the toolbox"},
                    )
                ],
                usage=Usage(uncached=1, output=1),
            ),
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="Echoed it.")],
                usage=Usage(uncached=1, output=1),
            ),
        ]
    )


def run(*, provider=None, workspace_dir: Path, model: str = "stub-model"):
    """Drive one turn and return the list of tool names that ran."""
    options = Options(
        system_prompt="You echo or shout text when asked.",
        name="toolbox-user",
        # Mount the whole server bundle. Its tools become available to the
        # agent by name — no allowed_tools entry needed.
        mcp_servers=(TOOLBOX,),
        permission_mode="bypassPermissions",
    )
    envelopes = query(
        options,
        goal="Echo 'hello from the toolbox'.",
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
    with tempfile.TemporaryDirectory(prefix="noeta-mcp-") as tmp:
        called = run(workspace_dir=Path(tmp))
    print(f"tools the agent called from the MCP server: {called}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
