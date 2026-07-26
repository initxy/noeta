# Build custom tools

**Goal:** define your own tools with `@tool`, wire them into an agent, and
optionally bundle them as an in-process MCP server.

**Before you start:** you have run through [Your first agent](../tutorials/first-agent.md)
and are comfortable with `Options` and `Client`.

## Define a tool with `@tool`

A tool is a plain function `fn(arguments: dict, ctx: ToolContext) ->
ToolResult`, wrapped with the `@tool` decorator:

```python
from noeta.sdk import tool
from noeta.protocols.tool import ToolContext, ToolResult

@tool(
    name="fetch_weather",
    version="1",
    risk_level="low",
    description="Fetch the current weather for a city.",
    input_schema={
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "City name"},
            "units": {"type": "string", "enum": ["celsius", "fahrenheit"]},
        },
        "required": ["city"],
        "additionalProperties": False,
    },
)
def fetch_weather(arguments: dict, ctx: ToolContext) -> ToolResult:
    city = arguments["city"]
    units = arguments.get("units", "celsius")
    # ... your implementation ...
    return ToolResult(success=True, output=f"22°C in {city}")
```

### Decorator parameters

| Parameter | Required | Purpose |
| --- | --- | --- |
| `name` | yes | The string the model calls. Must be `snake_case`. |
| `input_schema` | yes | JSON Schema describing the expected arguments. LLM-facing metadata. |
| `version` | no (`None`) | Feeds the tool's identity fingerprint. Bump when behavior changes. |
| `risk_level` | no (`"low"`) | `"low"`, `"medium"`, or `"high"`. Used by the permission system. |
| `description` | no (`""`) | The model's primary source of tool semantics. Write it clearly. |

Only `name` and `input_schema` have no default. Pass the other three anyway:
an empty `description` leaves the model guessing what the tool does, and the
default `risk_level="low"` means the tool is auto-approved in every permission
mode — fine for a read-only lookup, wrong for anything that writes.

### `ToolResult`

Return `ToolResult(success=True, output="...")` for a successful call, or
`ToolResult(success=False, output="error message")` for a failure. The
`output` is a string the model reads — keep it concise and clear.

`ToolResult` also accepts `artifacts` (a list of `Artifact` objects) and
`output_ref` (a `ContentRef` to large output), but for most tools
`success` + `output` is enough.

## Wire it into your agent

Pass the tool via `Options.allowed_tools`:

```python
from noeta.sdk import Options, Client

options = Options(
    system_prompt="You are a weather assistant.",
    name="weather-bot",
    allowed_tools=(fetch_weather,),
)

client = Client(options, provider=my_provider, workspace_dir="./")
```

`allowed_tools` **is** the selection — a custom tool is available only if it
appears there. `None` means "the built-in whitelist", which does *not* pick up
a tool merely because you defined it, so list your own alongside the built-ins
you want:

```python
allowed_tools=("read", "grep", fetch_weather)
```

`disallowed_tools` subtracts from the built-in set; it does not add custom
tools either.

## Risk levels and permissions

The `risk_level` on your tool interacts with the `permission_mode`:

| Risk | `default` mode | `acceptEdits` mode | `bypassPermissions` mode |
| --- | --- | --- | --- |
| `low` | auto-approved | auto-approved | auto-approved |
| `medium` | requires approval | requires approval | auto-approved |
| `high` | requires approval | requires approval | auto-approved |

Mark tools that write files, run commands, or make external API calls as
`"high"`. Read-only tools are `"low"`.

## Bundle tools into an MCP server

If you want to share your tools across multiple agents or make them
available via the MCP protocol, bundle them into an in-process MCP
server:

```python
from noeta.sdk import create_sdk_mcp_server

weather_mcp = create_sdk_mcp_server(
    name="weather-tools",
    version="1.0.0",
    tools=(fetch_weather,),
)
```

Then mount it in `Options`:

```python
options = Options(
    system_prompt="...",
    name="my-agent",
    mcp_servers=(weather_mcp,),
    allowed_tools=None,  # all built-ins + this server's tools
)
```

An in-process server's tools keep their **bare** `@tool` name — the model sees
`fetch_weather`, not `mcp__weather-tools__fetch_weather`. Bundling them this
way is about grouping and reuse across agents; it does not rename them.

> The `mcp__{alias}__{tool}` prefix belongs to **remote** MCP servers — the
> ones a host connects per turn through `HostConfig.mcp_server_resolver` (see
> [Connect MCP](connect-mcp.md)). Those are namespaced because independent
> third-party servers can and do collide on tool names.

Because the names are bare, an in-process server's tool collides with a
built-in of the same name. Pick names that will not clash (`fetch_weather`,
not `read`).

## Test your tool offline

Use `FakeLLMProvider` to script a call to your tool and verify it runs:

```python
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.protocols.messages import (
    LLMResponse, TextBlock, ToolUseBlock, Usage,
)

provider = FakeLLMProvider(
    responses=[
        LLMResponse(
            stop_reason="tool_use",
            content=[ToolUseBlock(
                call_id="t1",
                tool_name="fetch_weather",
                arguments={"city": "Tokyo"},
            )],
            usage=Usage(uncached=1, output=1),
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="Done.")],
            usage=Usage(uncached=1, output=1),
        ),
    ]
)
```

Drive it with `Client` and verify the `ToolResult` in the message stream.

## See also

- [SDK reference](../reference/sdk.md) — `@tool`, `create_sdk_mcp_server`,
  `ToolResult` full signatures
- [Connect MCP](connect-mcp.md) — register remote MCP servers
- [Guard vs Observer](../concepts/guard-observer.md) — how the permission
  system works
