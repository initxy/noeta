# MCP connectors

Noeta supports the [Model Context Protocol](https://modelcontextprotocol.io/)
(MCP) in two ways:

1. **In-process MCP servers** — bundle `@tool` functions into a named,
   in-process server via `create_sdk_mcp_server()`. Tools run in the host
   process (no subprocess, no network).
2. **External MCP servers** — connect to remote HTTP or local stdio MCP
   servers registered in the host config. Tools appear as
   `mcp__<alias>__<tool>`.

## In-process MCP server

```python
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

# --- 1. Define tools with @tool --------------------------------------------

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

# --- 2. Bundle into a named MCP server -------------------------------------
#
# create_sdk_mcp_server returns a frozen SdkMcpServer value object.
# Hand it to Options.mcp_servers — its tools become available by name.

toolbox = create_sdk_mcp_server("toolbox", version="1.0.0", tools=[echo, shout])

# --- 3. Mount on Options ---------------------------------------------------

options = Options(
    system_prompt="You echo or shout text when asked.",
    name="toolbox-user",
    mcp_servers=(toolbox,),
    permission_mode="bypassPermissions",
)

# --- 4. Run -----------------------------------------------------------------

provider = FakeLLMProvider(
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

with tempfile.TemporaryDirectory(prefix="noeta-mcp-") as tmp:
    envelopes = query(
        options,
        goal="Echo 'hello from the toolbox'.",
        provider=provider,
        workspace_dir=Path(tmp),
        model="stub-model",
    )

    called = [
        e.payload.tool_name
        for e in envelopes
        if e.type == "ToolCallStarted"
    ]
    print(f"tools called from MCP server: {called}")
    # → tools called from MCP server: ['echo']
```

### When to use in-process vs. `allowed_tools`

`allowed_tools` mounts one loose tool at a time. `create_sdk_mcp_server`
groups several related tools under one server value object, so a whole
toolbox travels (and is identified) as a unit. Use in-process MCP when:

- You have a cohesive set of tools that belong together
- You want versioned identity for the bundle
- You want the tools discoverable through MCP tool listing APIs

## External MCP servers (app mode)

When running the full `python -m noeta.agent` app, external MCP servers
are registered via the host config (JSON file or HTTP API).

### Register via config file

Add to your `NOETA_AGENT_CONFIG` JSON:

```json
{
  "mcp_servers": {
    "github": {
      "type": "http",
      "url": "https://mcp.github.com/mcp",
      "headers": {
        "Authorization": "Bearer <your-token>"
      }
    },
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]
    }
  }
}
```

### Register via HTTP API

While the agent is running, you can manage MCP servers through the
HTTP API:

```bash
# List registered servers
curl http://127.0.0.1:8765/mcp/servers

# Register an HTTP MCP server
curl -X POST http://127.0.0.1:8765/mcp/servers \
  -H "Content-Type: application/json" \
  -d '{
    "alias": "github",
    "type": "http",
    "url": "https://mcp.github.com/mcp",
    "headers": {"Authorization": "Bearer <token>"}
  }'

# Register a stdio MCP server
curl -X POST http://127.0.0.1:8765/mcp/servers \
  -H "Content-Type: application/json" \
  -d '{
    "alias": "fs",
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
  }'

# List tools from a server
curl http://127.0.0.1:8765/mcp/servers/github/tools

# Remove a server
curl -X DELETE http://127.0.0.1:8765/mcp/servers/github
```

### Enabling MCP servers per session

When creating a task via `POST /tasks`, pass the `enabled_mcp` field to
select which registered servers are available for that session:

```bash
curl -X POST http://127.0.0.1:8765/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Search the codebase for TODOs.",
    "agent": "main",
    "enabled_mcp": ["github", "fs"]
  }'
```

Enabled MCP tools appear in the agent's tool set as `mcp__<alias>__<tool>`.

## Key points

- **In-process = `create_sdk_mcp_server()` + `Options.mcp_servers`.**
  Tools run locally, no network. Good for bundling your own toolkits.
- **External = host config + `/mcp/servers` API.** Connects to remote HTTP
  or local stdio MCP servers. Good for third-party MCP ecosystems.
- **Credentials never travel in request bodies.** They're stored host-side
  and scrubbed from API responses.
- **`mcp_servers` on `Options` is for in-process only.** External servers
  are managed through the host config / API, not the recipe.

## Source

- `examples/mcp_server.py` — full in-process MCP demo
- `noeta.sdk.create_sdk_mcp_server` — `packages/noeta-sdk/noeta/sdk/authoring.py`
- MCP route handlers: `apps/noeta-agent/noeta/agent/backend/mcp_service.py`
- MCP registry: `apps/noeta-agent/noeta/agent/host/mcp_registry.py`
- See also: [Tools Reference](../reference/tools.md),
  [HTTP API](../reference/http-api.md#mcp-server-management),
  [ADR: MCP connectors](../adr/mcp-connectors.md)
