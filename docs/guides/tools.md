# Custom tools

Give your agent its own tools: write a function, wrap it in `@tool`, list it in
`allowed_tools`. This page covers the decorator, results and failures, risk
levels, and bundling several tools together.

## Define a tool

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
    return ToolResult(success=True, output=f"22°C in {city}")
```

| Parameter | Default | What it does |
| --- | --- | --- |
| `name` | required | The name the model calls. Use provider-safe `snake_case`. |
| `version` | required | Part of the tool's identity. Leaving it out raises `TypeError`. |
| `input_schema` | required | JSON Schema shown to the model. Not enforced: validate `arguments` yourself. |
| `risk_level` | `"low"` | `"low"`, `"medium"` or `"high"`. Decides whether a call waits for approval. |
| `description` | `""` | The model's only explanation of the tool. Always write one. |

`@tool` returns a `DecoratedTool`: the runnable tool plus its `.ref`
(`ToolRef(name, version, risk_level)`), built from the same fields so the two
cannot disagree.

## Return a result

```python
return ToolResult(success=True, output={"temp_c": 22, "city": city})
return ToolResult(success=False, summary="city not found")
```

- `output` is any JSON-encodable value; it is what the model reads back.
- For a failure, put the message in `summary`. The model sees it as the error
  text (`"tool failed"` if empty).
- If your function raises, the call is recorded as a failed result and the
  turn goes on. Return a `summary` yourself when you want the model to see a
  useful message.
- Large or binary bodies go in the content store:
  `ref = ctx.artifact_store.put(body, media_type="image/png")`, then
  `ToolResult(..., artifacts=[ref])`, or `images=[ref]` for a vision model to see.
- `ctx.metadata["task_id"]` tells you which task made the call.

## Add it to the agent

```python
from noeta.sdk import Options

options = Options(
    system_prompt="You are a weather assistant.",
    allowed_tools=("Read", "Grep", fetch_weather),
)
```

`allowed_tools` is the complete list: your tools by value, built-in tools by
name.

| `allowed_tools` | Tools the agent gets |
| --- | --- |
| `None` (default) | the 10 built-ins: `Read`, `Glob`, `Grep`, `Edit`, `Write`, `Bash`, `BashOutput`, `KillShell`, `WebFetch`, `WebSearch` |
| a tuple | exactly those entries |
| `()` | none |

`WebSearch` only mounts when `NOETA_WEB_SEARCH_API_KEY` is set.
`disallowed_tools=("Bash",)` removes names from whichever list applies; it
never adds. A custom tool is available only if it is in `allowed_tools` (or in a bundle, below).
See [built-in tools](../reference/tools.md) for what each one does.

## Pick a risk level

`risk_level` meets `Options.permission_mode`:

| Risk | `default` | `acceptEdits` | `bypassPermissions` |
| --- | --- | --- | --- |
| `low` | runs | runs | runs |
| `medium` | waits for approval | waits for approval | runs |
| `high` | waits for approval | waits for approval | runs |

`acceptEdits` only differs from `default` for the built-in `Edit` and `Write`,
so for a custom tool the two behave the same. Mark anything that writes files,
runs commands or calls an external API as `"high"`; read-only lookups as
`"low"`. The [tutorial](../start/tutorial.md) shows how to
approve or deny a waiting call.

## Bundle several tools

To ship related tools as one unit, bundle them as an in-process MCP server and
mount it on `Options.mcp_servers`:

```python
from noeta.sdk import Options, create_sdk_mcp_server

weather = create_sdk_mcp_server(
    name="weather-tools",
    version="1.0.0",
    tools=(fetch_weather,),
)

options = Options(system_prompt="...", mcp_servers=(weather,))
```

- Every entry must be a `@tool` function, or `create_sdk_mcp_server` raises
  `TypeError`.
- Bundled tools join the agent directly; they need no `allowed_tools` entry.
- They keep their bare names (`fetch_weather`), so avoid built-in names like
  `Read`. Only remote MCP servers get the `mcp__{alias}__{tool}` prefix; see
  [MCP servers](mcp.md).

## Test it

Script a model call to your tool with `FakeLLMProvider` and assert that it
ran. [Testing](testing.md#assert-a-tool-ran) has the full example.
`examples/custom_tool.py` and `examples/mcp_server.py` are runnable versions of
this page.

## Next

- [Test offline](testing.md)
- [MCP servers](mcp.md): connect remote tool servers
- [Built-in tools](../reference/tools.md)
