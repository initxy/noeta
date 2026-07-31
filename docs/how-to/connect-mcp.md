# Connect MCP servers

This guide shows you how to give an agent access to MCP (Model Context Protocol)
servers — external `stdio` / `http` servers, or your own tools bundled into an
in-process one. You need a working `noeta.sdk` setup and a server to connect.

Pick the right mechanism first; they are not interchangeable:

| | External servers | In-process server |
| --- | --- | --- |
| Mounted on | `HostConfig.mcp_server_resolver` | `Options.mcp_servers` |
| Enabled | per turn, by alias | always, for that agent |
| Tool names | `mcp__{alias}__{tool}` | the bare `@tool` names |
| Runs in | a subprocess or over HTTP | your process |

## Option A — external servers (`stdio` / `http`)

The host never hands the SDK a config store. It supplies a **resolver** —
`alias -> spec | None` — and names the aliases to connect on each turn. That
keeps credentials host-side: they live only in the spec you construct, and
never reach the recording or the model-visible tool schema.

```python
from noeta.sdk import Client, HostConfig, McpHttpServerSpec, McpServerSpec, Options

SERVERS = {
    "fs": McpServerSpec(
        alias="fs",
        argv=("npx", "-y", "@modelcontextprotocol/server-filesystem", "/data"),
        tool_subset=("read_file", "list_directory"),   # None = every tool
    ),
    "search": McpHttpServerSpec(
        alias="search",
        url="https://mcp.example.com/rpc",
        headers=(("Authorization", "Bearer …"),),
    ),
}

client = Client(
    Options(system_prompt="…", name="my-agent"),
    provider=my_provider,
    host_config=HostConfig(mcp_server_resolver=SERVERS.get),
)

outcome = client.start(goal="list the data directory", enabled_mcp=("fs",))
```

With the `fs` alias enabled, the two subset tools reach the model under their
namespaced names:

```
mcp__fs__read_file
mcp__fs__list_directory
```

`enabled_mcp` is a per-turn, non-durable knob. Every turn-driving verb accepts
it (`start`, `send_goal`, `seed_start`, `seed_send_goal`), so a conversation
can enable a server for one turn and not the next. `query` has no `enabled_mcp`
parameter — external MCP needs a `Client`.

Both specs are frozen dataclasses validated at construction:

- `alias` must match `^[a-z0-9_-]{1,32}$`.
- `McpServerSpec` needs a non-empty `argv`; it is executed directly, never
  through a shell. `env` adds environment variables for the spawned process.
- `McpHttpServerSpec` needs a non-empty `url` — one JSON-RPC endpoint. The
  client posts one request and reads one response over the standard library;
  `HostConfig.mcp_http_post` replaces that transport if you need your own.
- `tool_subset` filters by the server's **raw** tool name. `None` keeps every
  advertised tool; a tuple keeps only the listed ones, and the rest never enter
  the tool set.

Each discovered tool becomes an ordinary Tool named
`mcp__{alias}__{tool}`, with characters outside `[A-Za-z0-9_-]` replaced. The
whole name must match `^[A-Za-z0-9_-]{1,64}$`; an empty, over-long, or
colliding name fails fast rather than being silently truncated. Because they
are ordinary Tools, they flow through the composer schema, the Policy, and the
permission Guard with no special casing.

If one server fails to connect at turn start, it is dropped and the rest of
the turn proceeds; a duplicate alias is always a hard `McpConfigError`.

## Option B — an in-process SDK MCP server

To bundle your own tools into an MCP-shaped server, use `create_sdk_mcp_server`:

```python
from noeta.sdk import Options, ToolContext, ToolResult, create_sdk_mcp_server, tool

@tool(
    name="echo",
    version="1",
    risk_level="low",
    description="Return the given text unchanged.",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
)
def echo(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(success=True, output=arguments["text"])

my_mcp = create_sdk_mcp_server(name="my-tools", version="1.0.0", tools=(echo,))

options = Options(
    system_prompt="...",
    name="my-agent",
    mcp_servers=(my_mcp,),
)
```

`Options.mcp_servers` accepts only these in-process servers. Their tools run
with no subprocess and no network round-trip, and they keep their **bare**
`@tool` names — the model sees `echo`, not `mcp__my-tools__echo`. The server
name groups the bundle; it does not namespace it, so choose names that will
not collide with a built-in tool.

## Next steps

- [Build custom tools](build-custom-tools.md) — define the tools you are
  bundling
- [Use a sandbox](use-sandbox.md) — MCP stays host-side even under a container
- [SDK reference](../reference/sdk.md) — `McpServerSpec`, `McpHttpServerSpec`,
  `HostConfig`
- [ADR: MCP connectors](https://github.com/initxy/noeta/blob/main/docs/adr/mcp-connectors.md)
  — the scope of the client and where credentials live

`examples/mcp_server.py` is a runnable in-process MCP example.
