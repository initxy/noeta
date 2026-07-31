"""SDK example — expose tools through an in-process MCP server.

Demonstrated SDK capability
---------------------------
:func:`noeta.sdk.create_sdk_mcp_server` and ``Options.mcp_servers``. The
returned server is a frozen value object whose tools run in the host process —
nothing to spawn, no network round-trip — which is the reason to reach for it
over listing loose tools in ``Options.allowed_tools``: a whole toolbox travels
and is identified as one unit.

An in-process server does not namespace what it carries: the model sees the
bare ``@tool`` names, so pick names that will not collide with a built-in. The
``mcp__{alias}__{tool}`` prefix belongs to remote servers, where third-party
collisions are the real risk.

The provider is scripted so the example needs no API key; pass a live provider
from ``noeta.sdk.providers`` to :func:`run` and a real model decides for itself
when to call the tools.

    python examples/mcp_server.py
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
    create_sdk_mcp_server,
    query,
    tool,
)
from noeta.sdk.testing import FakeLLMProvider


_TEXT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


@tool(name="echo", version="1", risk_level="low", input_schema=_TEXT_SCHEMA)
def echo(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(success=True, output=str(arguments.get("text", "")))


@tool(name="shout", version="1", risk_level="low", input_schema=_TEXT_SCHEMA)
def shout(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(success=True, output=str(arguments.get("text", "")).upper())


# Every entry must be a ``@tool``-decorated object; anything else raises here,
# at authoring time, rather than yielding a server with a non-runnable tool.
TOOLBOX = create_sdk_mcp_server("toolbox", version="1.0.0", tools=[echo, shout])


def _demo_provider() -> FakeLLMProvider:
    """A network-free provider scripted to call ``echo`` once, then finish."""
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
    """Drive one turn and return the tool names the agent actually invoked."""
    options = Options(
        system_prompt="You echo or shout text when asked.",
        name="toolbox-user",
        # Mounting the bundle is enough; its tools need no ``allowed_tools``
        # entry of their own.
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
