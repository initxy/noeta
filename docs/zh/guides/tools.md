# 自定义工具

给 agent 加自己的工具：写一个函数，套上 `@tool`，列进 `allowed_tools`。本页讲装饰器、返回结果和报错、风险等级，以及怎么把几个工具打包在一起。

## 定义工具

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

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `name` | 必填 | 模型调用时用的名字，用各家 provider 都接受的 `snake_case`。 |
| `version` | 必填 | 工具身份的一部分。不写会抛 `TypeError`。 |
| `input_schema` | 必填 | 给模型看的 JSON Schema。运行时不校验，`arguments` 要自己检查。 |
| `risk_level` | `"low"` | `"low"`、`"medium"` 或 `"high"`，决定调用要不要等审批。 |
| `description` | `""` | 模型了解这个工具的唯一途径，一定要写。 |

`@tool` 返回一个 `DecoratedTool`：既是能跑的工具，也带着 `.ref`（`ToolRef(name, version, risk_level)`）。两者出自同一组参数，不会对不上。

## 返回结果

```python
return ToolResult(success=True, output={"temp_c": 22, "city": city})
return ToolResult(success=False, summary="city not found")
```

- `output` 可以是任何能转成 JSON 的值，模型读到的就是它。
- 失败时把原因写进 `summary`，模型会把它当错误信息看到（为空时显示 `"tool failed"`）。
- 函数抛了异常，这次调用会记成失败，这一轮照常继续。想让模型看到有用的提示，就自己返回带 `summary` 的结果。
- 大块内容或二进制放进内容存储：`ref = ctx.artifact_store.put(body, media_type="image/png")`，再写 `ToolResult(..., artifacts=[ref])`；要让支持看图的模型看到，就用 `images=[ref]`。
- `ctx.metadata["task_id"]` 是发起这次调用的任务。

## 加进 agent

```python
from noeta.sdk import Options

options = Options(
    system_prompt="You are a weather assistant.",
    allowed_tools=("Read", "Grep", fetch_weather),
)
```

`allowed_tools` 是完整清单：自定义工具直接放对象，内置工具写名字。

| `allowed_tools` | agent 拿到的工具 |
| --- | --- |
| `None`（默认） | 10 个内置工具：`Read`、`Glob`、`Grep`、`Edit`、`Write`、`Bash`、`BashOutput`、`KillShell`、`WebFetch`、`WebSearch` |
| 一个元组 | 正好是元组里这些 |
| `()` | 没有工具 |

`WebSearch` 只在设了 `NOETA_WEB_SEARCH_API_KEY` 时才会加载。`disallowed_tools=("Bash",)` 从上面得到的清单里去掉某些工具，只减不加。自定义工具必须写进 `allowed_tools` 才能用（打包的除外，见下文）。各内置工具的用途见[内置工具](../reference/tools.md)。

## 选风险等级

`risk_level` 和 `Options.permission_mode` 一起决定要不要审批：

| 风险 | `default` | `acceptEdits` | `bypassPermissions` |
| --- | --- | --- | --- |
| `low` | 直接跑 | 直接跑 | 直接跑 |
| `medium` | 等审批 | 等审批 | 直接跑 |
| `high` | 等审批 | 等审批 | 直接跑 |

`acceptEdits` 和 `default` 的区别只在内置的 `Edit`、`Write` 上，对自定义工具两者一样。会写文件、跑命令、调外部 API 的工具标 `"high"`，只读查询标 `"low"`。怎么批准或拒绝一次等待中的调用，见[教程](../start/tutorial.md)。

## 打包多个工具

几个相关的工具想一起发布，可以打包成进程内 MCP server，挂到 `Options.mcp_servers` 上：

```python
from noeta.sdk import Options, create_sdk_mcp_server

weather = create_sdk_mcp_server(
    name="weather-tools",
    version="1.0.0",
    tools=(fetch_weather,),
)

options = Options(system_prompt="...", mcp_servers=(weather,))
```

- 每一项都必须是 `@tool` 函数，否则 `create_sdk_mcp_server` 抛 `TypeError`。
- 打包的工具直接加进 agent，不用再写进 `allowed_tools`。
- 它们保留原名（`fetch_weather`），所以别和 `Read` 这类内置工具重名。只有远程 MCP server 的工具才带 `mcp__{alias}__{tool}` 前缀，见 [MCP server](mcp.md)。

## 测试

用 `FakeLLMProvider` 模拟模型调你的工具，再断言它确实跑了，完整例子见[离线测试](testing.md#断言工具被调用)。`examples/custom_tool.py` 和 `examples/mcp_server.py` 是本页内容的可运行版本。

## 下一步

- [离线测试](testing.md)
- [MCP server](mcp.md)：接入远程工具服务
- [内置工具](../reference/tools.md)
