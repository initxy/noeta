# Connect MCP servers

**Goal:** give your SDK agent access to MCP (Model Context Protocol) servers —
external `stdio` / `http` servers, or your own tools bundled into an in-process
server.

**Before you start:** a working `noeta.sdk` setup and an MCP server you want to
connect.

MCP servers are mounted through `Options.mcp_servers`. Their tools appear to
the model as `mcp__<alias>__<tool>`; a per-server `tool_subset` keeps only the
tools you want exposed (`None` = all).

## External servers (`stdio` / `http`)

`noeta.sdk` exports two server specs — a subprocess (`stdio`) server and a
remote (`http`) one:

```python
from noeta.sdk import Options, McpServerSpec, McpHttpServerSpec

stdio = McpServerSpec(
    alias="fs",
    argv=("npx", "-y", "@modelcontextprotocol/server-filesystem", "/data"),
    tool_subset=("read_file", "list_directory"),   # None = all tools
)
http = McpHttpServerSpec(
    alias="search",
    url="https://mcp.example.com/sse",
    headers=(("Authorization", "Bearer …"),),
)

options = Options(
    system_prompt="…",
    name="my-agent",
    mcp_servers=(stdio, http),
)
```

Credentials (headers, env) live only in the spec you construct — they are
never written into the recording or the model-visible tool schema.

## In-process SDK MCP server

To bundle your own tools into an MCP-shaped server, use
`create_sdk_mcp_server`:

```python
from noeta.sdk import create_sdk_mcp_server, tool, ToolContext, ToolResult

@tool(name="echo", version="1", risk_level="low",
      input_schema={"type": "object", "properties":
                    {"text": {"type": "string"}}, "required": ["text"]})
def echo(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(success=True, output=arguments["text"])

my_mcp = create_sdk_mcp_server(
    name="my-tools",
    version="1.0.0",
    tools=(echo,),
)
```

Mount it the same way:

```python
from noeta.sdk import Options

options = Options(
    system_prompt="...",
    name="my-agent",
    mcp_servers=(my_mcp,),
)
```

The tools appear as `mcp__my-tools__echo` — same naming convention as
external MCP servers, but they run in-process with no subprocess or network
round-trip.

## See also

- [Build custom tools](build-custom-tools.md) — define tools with `@tool`
  and bundle them into SDK MCP servers
- [ADR: MCP connectors](https://github.com/initxy/noeta/blob/main/docs/adr/mcp-connectors.md) — the design behind per-connector tool subsets
- `examples/mcp_server.py` — full in-process MCP example
