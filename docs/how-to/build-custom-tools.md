# Build custom tools

This guide shows you how to define your own tools with `@tool`, wire them into an
agent, and bundle several of them as one in-process MCP server. You need to be
comfortable with `Options` and `Client` from
[Your first agent](../tutorials/first-agent.md).

## 1. Define a tool with `@tool`

A tool is a plain function `fn(arguments: dict, ctx: ToolContext) ->
ToolResult`, wrapped with the `@tool` decorator:

```python
from noeta.sdk import ToolContext, ToolResult, tool

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

| Parameter | Default | Purpose |
| --- | --- | --- |
| `name` | required | The string the model calls. Provider-safe `snake_case`. |
| `version` | required | Part of the tool's declared identity. Omitting it raises `TypeError` — a default would let two behaviourally different tools share one identity inside an agent. |
| `input_schema` | required | A hand-written JSON-Schema-shaped dict. LLM-facing metadata only: Noeta does **not** validate `arguments` against it at call time. |
| `risk_level` | `"low"` | `"low"`, `"medium"`, or `"high"`. Read by the permission system. |
| `description` | `""` | The model's single source of tool semantics, rendered into the provider tool schema. |

Pass `risk_level` and `description` even though they have defaults: an empty
description leaves the model guessing, and `"low"` auto-approves the tool in
every permission mode — right for a read-only lookup, wrong for anything that
writes.

The decorator returns a `DecoratedTool`: it satisfies the `Tool` protocol
structurally **and** exposes `.ref`, the `ToolRef` identity an agent declares.
Both halves are built from the same fields, so the runnable closure and its
recorded identity cannot drift apart.

### `ToolResult`

`output` is any JSON-encodable value and is what the model reads back. For a
failure, put the message in `summary`, not `output` — the projection renders a
`success=False` result with `summary` as its error text (falling back to
`"tool failed"` when empty):

```python
return ToolResult(success=False, summary="city not found")
```

`artifacts` and `images` are `list[ContentRef]`: put large or binary bodies
into the ContentStore with `ctx.artifact_store.put(body, media_type=...)` and
reference them here. `output_ref` is assigned by the runtime after it offloads
the output; tools never set it.

## 2. Wire it into your agent

Pass the tool object by value in `Options.allowed_tools`:

```python
from noeta.sdk import Client, Options

options = Options(
    system_prompt="You are a weather assistant.",
    name="weather-bot",
    allowed_tools=("Read", "Grep", fetch_weather),
)

client = Client(options, provider=my_provider, workspace_dir="./")
```

`allowed_tools` **is** the selection: a custom tool is available only if it
appears there, so list your own alongside the built-ins you want. `None`
selects the 10-name built-in whitelist and picks up nothing custom.
`disallowed_tools` subtracts names from the parsed list; it never adds
anything.

## 3. Pick the right risk level

`risk_level` interacts with `Options.permission_mode`:

| Risk | `default` | `acceptEdits` | `bypassPermissions` |
| --- | --- | --- | --- |
| `low` | auto-approved | auto-approved | auto-approved |
| `medium` | requires approval | requires approval | auto-approved |
| `high` | requires approval | requires approval | auto-approved |

`acceptEdits` differs from `default` only by exempting the three built-in edit
tools (`Edit`, `Write`); it changes nothing for a custom tool.

Mark tools that write files, run commands, or make external API calls as
`"high"`. Read-only lookups are `"low"`.

## 4. Optional: bundle several tools as one server

To ship several related tools as a unit, bundle them with
`create_sdk_mcp_server`:

```python
from noeta.sdk import create_sdk_mcp_server

weather_mcp = create_sdk_mcp_server(
    name="weather-tools",
    version="1.0.0",
    tools=(fetch_weather,),
)
```

Every entry must be a `@tool`-decorated function; anything else raises
`TypeError` at authoring time. Mount the bundle on `Options.mcp_servers`:

```python
options = Options(
    system_prompt="...",
    name="my-agent",
    mcp_servers=(weather_mcp,),
)
```

An in-process server's tools keep their **bare** `@tool` name — the model sees
`fetch_weather`, not `mcp__weather-tools__fetch_weather`. The server name is a
grouping label, not a namespace, so pick tool names that will not collide with
a built-in (`fetch_weather`, not `Read`). Its tools are added to the agent's
tool set directly; they need no `allowed_tools` entry.

> The `mcp__{alias}__{tool}` prefix belongs to **remote** MCP servers, which a
> host connects per turn — see [Connect MCP](connect-mcp.md). Those are
> namespaced because independent third-party servers do collide on tool names.

## 5. Test it offline

Script a call with `FakeLLMProvider` and drive one turn with `query`, which
returns the full envelope list — so the assertion proves the closure ran:

```python
from pathlib import Path

from noeta.sdk import LLMResponse, TextBlock, ToolUseBlock, Usage, query
from noeta.sdk.testing import FakeLLMProvider

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

result = query(
    options,
    goal="What is the weather in Tokyo?",
    provider=provider,
    workspace_dir=Path("./"),
    model="stub-model",
)

called = [e.payload.tool_name for e in result if e.type == "ToolCallStarted"]
print(called)
assert called == ["fetch_weather"]
```

```
['fetch_weather']
```

`examples/custom_tool.py` and `examples/mcp_server.py` are runnable versions of
both halves of this page.

## Next steps

- [Connect MCP](connect-mcp.md) — remote MCP servers and their `mcp__` namespace
- [Built-in tools](../reference/tools.md) — the 10-name whitelist and its risk
  levels
- [Guard vs Observer](../concepts/guard-observer.md) — how the permission system
  decides
- [SDK reference](../reference/sdk.md) — `@tool`, `create_sdk_mcp_server`, and
  `ToolResult` in full
