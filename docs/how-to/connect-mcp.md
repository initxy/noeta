# Connect MCP servers

**Goal:** register remote (stdio or HTTP) MCP servers so Noeta can use
their tools, or bundle your own tools into an in-process MCP server.

**Before you start:** you have a working Noeta installation. You know
what MCP (Model Context Protocol) is and have an MCP server you want to
connect.

## Option A: remote MCP via the coding agent

For `python -m noeta.agent`, MCP servers are registered in the host's
connector store at `~/.noeta/mcp_servers.json`:

```json
{
  "servers": {
    "github": {
      "type": "http",
      "url": "https://mcp.github.com/mcp",
      "headers": {
        "Authorization": "Bearer ghp_…"
      }
    },
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"],
      "env": {}
    }
  }
}
```

Each entry has an `alias` (the key — `github`, `filesystem`) that the
agent uses to reference the server. The `type` is `"http"` or `"stdio"`.

Credentials (header values, env vars) are stored host-side and **never**
travel in request bodies or appear in the `/mcp/servers` discovery
response (which returns credential-scrubbed entries).

### Enable MCP per session

Registered servers are not automatically used. You enable them per
session via the `enabled_mcp` field when creating a task:

```bash
# Via HTTP
curl -X POST http://127.0.0.1:<port>/tasks \
  -H "Content-Type: application/json" \
  -d '{"goal": "Read the repo README", "enabled_mcp": ["github"]}'
```

Or in the web UI, select MCP servers from the dropdown when creating a
new session.

When enabled, the MCP server's tools appear as
`mcp__<alias>__<tool_name>` in the agent's tool allow-list.

### Manage servers via HTTP

The backend exposes CRUD endpoints for the connector store:

| Route | What it does |
| --- | --- |
| `GET /mcp/servers` | List registered servers (credentials scrubbed) |
| `POST /mcp/servers` | Register a new server |
| `PUT /mcp/servers/{alias}` | Merge-edit an existing server |
| `DELETE /mcp/servers/{alias}` | Remove a server |
| `GET /mcp/servers/{alias}/tools` | Discover the server's tool menu |
| `GET /mcp/servers/{alias}/prompts` | Discover the server's prompts |
| `GET /mcp/servers/{alias}/resources` | Discover the server's resources |

See [HTTP API reference](../reference/http-api.md) for the full request
and response shapes.

## Option B: in-process SDK MCP server

For SDK users who want to bundle their own tools into an MCP-shaped
server, use `create_sdk_mcp_server`:

```python
from noeta.sdk import create_sdk_mcp_server, tool
from noeta.protocols.tool import ToolContext, ToolResult

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

Mount it in `Options`:

```python
from noeta.sdk import Options

options = Options(
    system_prompt="...",
    name="my-agent",
    mcp_servers=(my_mcp,),
)
```

The tools appear as `mcp__my-tools__echo` — same naming convention as
remote MCP servers, but they run in-process with no subprocess or
network round-trip.

## Verify the connection

After registering a server, verify the tool discovery works:

```bash
curl http://127.0.0.1:<port>/mcp/servers/github/tools
```

You should see the server's tool menu as a JSON array. If you get a 502,
the server is registered but the connection or handshake failed — check
the URL, headers, and that the MCP server is actually running.

## See also

- [Build custom tools](build-custom-tools.md) — define tools with `@tool`
  and bundle them into SDK MCP servers
- [Coding agent reference](../reference/noeta-agent.md) — env config for
  MCP
- [HTTP API reference](../reference/http-api.md) — MCP route details
- `examples/mcp_server.py` — full in-process MCP example
