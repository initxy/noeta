# Connect MCP servers

**Goal:** give a space's agent access to MCP (Model Context Protocol)
servers, or bundle your own tools into an in-process MCP server for an SDK
agent.

**Before you start:** you have a running platform (or a working SDK setup)
and an MCP server you want to connect.

## Option A: per-space connectors on the platform

MCP connectors are **space-scoped** configuration, managed on the space's
MCP page (or over the API). There is no global registry file — the retired
`~/.noeta/mcp_servers.json` mechanism is gone; each space carries its own
connector set in the application database.

Register a connector under an alias, with one of two transports:

- **`http`** — a URL plus optional headers (bearer tokens etc.).
- **`stdio`** — a command, args, and env for a local server process.

Then, per connector:

- **Enable / disable it.** Enabled connectors are resolved into the agent
  host **every turn** — no session restart needed; their tools appear to the
  model as `mcp__<alias>__<tool>`.
- **Restrict the tool subset.** Discover the server's full tool menu and
  keep only the tools you want exposed (`null` = all).

Credentials (header values, env values) are stored server-side and **never**
echoed back — listing connectors returns credential-scrubbed entries. Only
the space owner can manage connectors; members can see them.

Over HTTP (all under `/api/v1/spaces/{space_id}/mcp`):

| Route | What it does |
| --- | --- |
| `GET /servers` | List connectors (credentials scrubbed) |
| `POST /servers` | Register a connector (`alias`, `type`, transport fields, optional `tools` subset) |
| `PUT /servers/{alias}` | Merge-edit an existing connector |
| `PATCH /servers/{alias}` | Enable / disable |
| `DELETE /servers/{alias}` | Remove |
| `GET /servers/{alias}/tools` | Discover the tool menu |
| `PUT /servers/{alias}/tools` | Set the enabled tool subset (`null` = all) |
| `GET /servers/{alias}/prompts` · `/resources` | Discover prompts / resources |

Discovery is HTTP-only: a `stdio` connector's discovery answers 400 (the
server does not spawn operator-configured subprocesses from a management
GET); a failed connect/handshake answers 502 — check the URL, headers, and
that the MCP server is actually running.

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

## See also

- [Build custom tools](build-custom-tools.md) — define tools with `@tool`
  and bundle them into SDK MCP servers
- [HTTP API reference](../reference/http-api.md#mcp-connectors) — request
  and response shapes
- `examples/mcp_server.py` — full in-process MCP example
