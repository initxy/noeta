# Connect MCP servers

Give an agent the tools of any MCP (Model Context Protocol) server — an external
`stdio` or HTTP server connected per turn, or your own `@tool` functions bundled
into an in-process server.

| | External server | In-process server |
| --- | --- | --- |
| Configured on | `HostConfig.mcp_server_resolver` | `Options.mcp_servers` |
| Enabled | per turn, by alias (`enabled_mcp=`) | always, for that agent |
| Tool names the model sees | `mcp__{alias}__{tool}` | the bare `@tool` names |
| Runs in | a subprocess, or over HTTP | your process |

## Connect an external server

Hand the host a resolver (`alias -> spec | None`), then name the aliases to
connect on each turn:

```python
from noeta.sdk import Client, HostConfig, McpHttpServerSpec, McpServerSpec, Options
from noeta.sdk.providers import AnthropicProvider

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
    Options(system_prompt="You are a helpful assistant.", name="my-agent"),
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
    host_config=HostConfig(mcp_server_resolver=SERVERS.get),
)

outcome = client.start(goal="List the data directory.", enabled_mcp=("fs",))
```

The model now sees `mcp__fs__read_file` and `mcp__fs__list_directory`.

- `enabled_mcp` is per turn and not stored with the task's configuration. `start`,
  `send_goal`, `seed_start` and `seed_send_goal` all take it. `query()` does
  not — external MCP needs a `Client`.
- Credentials (`headers`, `env`) live only in the spec you build. They never
  reach the event log or the model.
- MCP tools are ordinary tools: guards, permission modes and approvals apply to
  them unchanged.

## Spec fields

| Spec | Field | Notes |
| --- | --- | --- |
| both | `alias` | must match `^[a-z0-9_-]{1,32}$` |
| both | `tool_subset` | raw tool names to keep; `None` keeps every advertised tool |
| `McpServerSpec` | `argv` | launch command, run directly (never through a shell) |
| `McpServerSpec` | `env` | extra environment for the process, as `(("KEY", "value"), …)` |
| `McpHttpServerSpec` | `url` | the single JSON-RPC endpoint |
| `McpHttpServerSpec` | `headers` | static headers sent on every request, as `(("Name", "value"), …)` |
| both | `call_timeout_s` | seconds one `tools/call` may take; `None` = 30 s; zero or negative raises. Other requests keep the default. Part of the connection-pool key |
| both | `deferred` | `True` keeps this server's tool schemas out of every request; the model finds and runs them through `ToolSearch` / `McpCall` (see below). Default `False`; a non-bool raises |

HTTP servers that assign an `Mcp-Session-Id` (Streamable HTTP) get it echoed on
every later request, and an SSE reply returns as soon as the matching response
arrives. A stdio server's `ping` is answered. To use your own HTTP transport,
set `HostConfig.mcp_http_post`.

## Defer a large server's schemas

Every advertised tool's full JSON schema rides in every request. A server with
40 tools can add ~25 KB to each one, for schemas the model rarely uses. Set
`deferred=True` on its spec and the model sees two small tools instead, once for
all deferred servers of the turn:

- `ToolSearch(query)` returns up to 5 matching deferred tools — name,
  description and full input schema. Match is on words from the name and
  description, or on an exact name; an empty query lists every deferred tool
  with a one-line description.
- `McpCall(tool, arguments)` runs the named deferred tool. The call is turned
  into a call to the real `mcp__{alias}__{tool}` before any guard sees it, so
  permission rules, `require_approval_tools`, `can_use_tool` and the audit
  trail all name the real tool. A name that is unknown or not deferred, or a
  missing `arguments` object, is an error for that call only.

```python
"crm": McpHttpServerSpec(alias="crm", url="https://crm.example.com/mcp", deferred=True),
```

The tool set still stays fixed for the turn, so the prompt cache holds. The
trade-off is one extra round trip the first time the model needs a deferred
tool's schema. Defer a server with many tools the model uses occasionally;
leave a small or constantly used one advertised.

## What the model gets back

Listing follows `nextCursor` pagination (up to 100 pages), so a server with
many tools is listed in full. A tool result keeps its text; `structuredContent`
is included too (a large one is stored and referenced as
`structured_content_ref`), images the model can display are handed to it as
images, and any other non-text block is written out as a line of text.

## When a server fails or changes

- A server that cannot connect at turn start is dropped, an `McpServerSkipped`
  event is recorded, and the turn continues with the other servers. A duplicate
  alias raises `McpConfigError`.
- A tool name longer than 64 characters is truncated and given an 8-character
  sha256 suffix; the `mcp__<alias>__` prefix is kept. When sanitising makes two
  tools on one server collide, a name that was already valid keeps it and the
  others take the suffix; the server is never dropped over one name. Two
  servers whose tools end up with the same name raise `McpConfigError`, naming
  both aliases, when the agent is built. An empty name is still an error.
- A call that times out, or a server that closes the stream, retires that
  connection; the next turn reconnects.
- Connections are pooled per host and shared across tasks. Idle ones close
  after `HostConfig.mcp_idle_ttl` (default 1800 s). Call
  `client.reconnect_mcp("fs")` (or `reconnect_mcp()` for all) after you change
  a server's config.
- Serving several tenants? Set `HostConfig.mcp_scope_resolver` so a stateful
  server is never shared between them — see
  [Per-tenant memory](multi-tenant-memory.md).

## Share servers with subagents

A subagent inherits the turn's enabled servers only if its definition activates
`mcp`:

```python
AgentDefinition(description="…", prompt="…", plugins=("mcp",))
```

## Bundle your own tools in-process

```python
from noeta.sdk import Options, ToolContext, ToolResult, create_sdk_mcp_server, tool

@tool(
    name="echo",
    version="1",
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

options = Options(system_prompt="…", name="my-agent", mcp_servers=(my_mcp,))
```

The tools run in your process with no subprocess or network hop, and keep their
bare names — the model sees `echo`, not `mcp__my-tools__echo`. Pick names that
don't collide with a built-in tool. `examples/mcp_server.py` is a runnable version.

## Next

- [Custom tools](tools.md) — write the `@tool` functions you bundle
- [Sandbox](sandbox.md) — MCP stays on the host even when tools run in a container
- [ADR: MCP connectors](https://github.com/initxy/noeta/blob/main/docs/adr/mcp-connectors.md)
