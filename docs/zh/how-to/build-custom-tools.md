# 构建自定义工具

本指南教你用 `@tool` 定义自己的工具、把它们接进一个 agent，并把若干个打包成一个进程内 MCP 服务器。你需要对[你的第一个 agent](../tutorials/first-agent.md) 里的 `Options` 和 `Client` 比较熟悉。

## 1. 用 `@tool` 定义一个工具

工具就是一个普通函数 `fn(arguments: dict, ctx: ToolContext) -> ToolResult`，用 `@tool` 装饰器包装：

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

### 装饰器参数

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `name` | 必填 | 模型调用时使用的字符串。用 provider 安全的 `snake_case`。 |
| `version` | 必填 | 工具声明身份的一部分。省略它会抛 `TypeError` —— 有默认值就意味着两个行为不同的工具可能在同一个 agent 内共用一个身份。 |
| `input_schema` | 必填 | 一个手写的、JSON-Schema 形状的 dict。只是面向 LLM 的元数据：Noeta 在调用时**不会**拿 `arguments` 去对照它做校验。 |
| `risk_level` | `"low"` | `"low"`、`"medium"` 或 `"high"`。由权限系统读取。 |
| `description` | `""` | 模型理解工具语义的唯一事实来源，会被渲染进 provider 的工具 schema。 |

即使 `risk_level` 和 `description` 有默认值也请显式传：空描述会让模型只能猜，而 `"low"` 会在每种权限模式下自动放行这个工具 —— 对只读查询是对的，对任何有写入的东西都是错的。

装饰器返回一个 `DecoratedTool`：它在结构上满足 `Tool` 协议，**并且**暴露 `.ref`，也就是 agent 所声明的 `ToolRef` 身份。两半都由同一组字段构建，所以可运行的闭包和它被记录下来的身份不可能各走各的。

### `ToolResult`

`output` 是任意可 JSON 编码的值，也是模型读回去的东西。对于失败，把消息放进 `summary` 而不是 `output` —— 投影会把 `success=False` 的结果渲染成以 `summary` 作为错误文本（为空时回退到 `"tool failed"`）：

```python
return ToolResult(success=False, summary="city not found")
```

`artifacts` 和 `images` 是 `list[ContentRef]`：用 `ctx.artifact_store.put(body, media_type=...)` 把大体积或二进制的内容放进 ContentStore，然后在这里引用它们。`output_ref` 由运行时在卸载 output 之后赋值；工具从不设置它。

## 2. 把它接进你的 agent

在 `Options.allowed_tools` 里按值传入工具对象：

```python
from noeta.sdk import Client, Options

options = Options(
    system_prompt="You are a weather assistant.",
    name="weather-bot",
    allowed_tools=("Read", "Grep", fetch_weather),
)

client = Client(options, provider=my_provider, workspace_dir="./")
```

`allowed_tools` **就是**这份选择：一个自定义工具只有出现在里面才可用，所以要把你自己的工具和你想要的内置工具一起列出来。`None` 选中那份 11 个名字的内置白名单，且不会捎上任何自定义工具。`disallowed_tools` 从解析出来的列表里减掉名字；它从不添加任何东西。

## 3. 选对风险级别

`risk_level` 与 `Options.permission_mode` 相互作用：

| 风险 | `default` | `acceptEdits` | `bypassPermissions` |
| --- | --- | --- | --- |
| `low` | 自动放行 | 自动放行 | 自动放行 |
| `medium` | 需要审批 | 需要审批 | 自动放行 |
| `high` | 需要审批 | 需要审批 | 自动放行 |

`acceptEdits` 与 `default` 的唯一差别是豁免了三个内置编辑工具（`edit`、`write`、`apply_patch`）；对自定义工具没有任何影响。

把会写文件、运行命令或发起外部 API 调用的工具标成 `"high"`。只读查询是 `"low"`。

## 4. 可选：把多个工具打包成一个服务器

要把若干相关工具作为一个整体发布，用 `create_sdk_mcp_server` 打包：

```python
from noeta.sdk import create_sdk_mcp_server

weather_mcp = create_sdk_mcp_server(
    name="weather-tools",
    version="1.0.0",
    tools=(fetch_weather,),
)
```

每一项都必须是 `@tool` 装饰过的函数；其他任何东西都会在编写期就抛 `TypeError`。把这个包挂到 `Options.mcp_servers` 上：

```python
options = Options(
    system_prompt="...",
    name="my-agent",
    mcp_servers=(weather_mcp,),
)
```

进程内服务器的工具保留其**裸的** `@tool` 名字 —— 模型看到的是 `fetch_weather`，而不是 `mcp__weather-tools__fetch_weather`。服务器名字只是一个分组标签，不是命名空间，所以挑那些不会和内置工具撞车的工具名（用 `fetch_weather`，别用 `read`）。它的工具会被直接加进 agent 的工具集；它们不需要 `allowed_tools` 条目。

> `mcp__{alias}__{tool}` 这个前缀属于**远程** MCP 服务器，由 host 按回合连接 —— 见[连接 MCP](connect-mcp.md)。那些之所以要加命名空间，是因为互相独立的第三方服务器确实会在工具名上撞车。

## 5. 离线测试它

用 `FakeLLMProvider` 脚本化一次调用，再用 `query` 驱动一轮 —— 它返回完整的信封列表，所以断言证明的是闭包确实跑了：

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

`examples/custom_tool.py` 和 `examples/mcp_server.py` 是本页两半内容的可运行版本。

## 下一步

- [连接 MCP](connect-mcp.md) —— 远程 MCP 服务器及其 `mcp__` 命名空间
- [内置工具](../reference/tools.md) —— 那份 11 个名字的白名单及各自的风险级别
- [Guard 与 Observer](../concepts/guard-observer.md) —— 权限系统如何裁决
- [SDK 参考](../reference/sdk.md) —— `@tool`、`create_sdk_mcp_server` 与 `ToolResult` 的完整说明
