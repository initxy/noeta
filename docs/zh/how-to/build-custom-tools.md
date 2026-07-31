# 构建自定义工具

**目标：** 用 `@tool` 定义你自己的工具，把它们接入 Agent，并可选地打包成进程内 MCP 服务器。

**开始之前：** 你已经跑过[你的第一个 Agent](../tutorials/first-agent.md)，并熟悉 `Options` 和 `Client`。

## 用 `@tool` 定义工具

工具就是一个普通函数 `fn(arguments: dict, ctx: ToolContext) -> ToolResult`，用 `@tool` 装饰器包一层：

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

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `name` | 必填 | 模型调用时使用的字符串。要用 Provider 安全的 `snake_case`。 |
| `version` | 必填 | 工具声明身份的一部分。省略会抛 `TypeError` —— 若给默认值，两个行为不同的工具就会在同一个 Agent 内共用一个身份。 |
| `input_schema` | 必填 | 一份手写的、符合 JSON Schema 形状的 dict。仅作面向 LLM 的元数据：Noeta **不会**在调用时用它校验 `arguments`。 |
| `risk_level` | `"low"` | `"low"`、`"medium"` 或 `"high"`。由权限系统读取。 |
| `description` | `""` | 模型理解工具语义的唯一来源，会被渲染进 Provider 的工具 schema。 |

即便 `risk_level` 和 `description` 有默认值，也请显式传入：空描述会让模型只能靠猜，而 `"low"` 会让工具在所有权限模式下自动批准 —— 对只读查询是对的，对任何会写入的操作则是错的。

装饰器返回一个 `DecoratedTool`：它在结构上满足 `Tool` 协议，**同时**暴露 `.ref`，也就是 Agent 声明时用的 `ToolRef` 身份。两半都由同一批字段构建，因此可运行的闭包与它被记录下来的身份不会彼此漂移。

### `ToolResult`

`output` 是任何可 JSON 编码的值，也是模型读回的内容。表示失败时，把消息放进 `summary` 而非 `output` —— 投影会渲染出一个 `success=False` 的结果，并以 `summary` 作为错误文本（为空时回退为 `"tool failed"`）：

```python
return ToolResult(success=False, summary="city not found")
```

`artifacts` 和 `images` 是 `list[ContentRef]`：用 `ctx.artifact_store.put(body, media_type=...)` 把大体积或二进制内容放进 ContentStore，再在这里引用它们。`output_ref` 由运行时在卸载 output 之后赋值；工具永远不要自己设置它。

## 将工具接入 Agent

在 `Options.allowed_tools` 里按值传入工具对象：

```python
from noeta.sdk import Client, Options

options = Options(
    system_prompt="You are a weather assistant.",
    name="weather-bot",
    allowed_tools=("read", "grep", fetch_weather),
)

client = Client(options, provider=my_provider, workspace_dir="./")
```

`allowed_tools` **就是**这份选择清单：自定义工具只有出现在其中才可用，所以要把你自己的工具和想要的内置工具一起列出来。`None` 会选中那份 11 个名字的内置白名单，不会带上任何自定义工具。`disallowed_tools` 从解析出的清单里减去名字；它永远不会添加任何东西。

## 风险等级与权限

`risk_level` 与 `Options.permission_mode` 相互作用：

| 风险 | `default` | `acceptEdits` | `bypassPermissions` |
| --- | --- | --- | --- |
| `low` | 自动批准 | 自动批准 | 自动批准 |
| `medium` | 需要批准 | 需要批准 | 自动批准 |
| `high` | 需要批准 | 需要批准 | 自动批准 |

`acceptEdits` 与 `default` 的唯一区别，是豁免三个内置的编辑工具（`edit`、`write`、`apply_patch`）；它对自定义工具没有任何改变。

把会写入文件、运行命令或发起外部 API 调用的工具标记为 `"high"`。只读查询是 `"low"`。

## 把工具打包成进程内 MCP 服务器

要把几个相关工具作为一个整体交付，用 `create_sdk_mcp_server` 打包它们：

```python
from noeta.sdk import create_sdk_mcp_server

weather_mcp = create_sdk_mcp_server(
    name="weather-tools",
    version="1.0.0",
    tools=(fetch_weather,),
)
```

每一项都必须是 `@tool` 装饰过的函数；其他任何东西都会在编写期抛出 `TypeError`。把这个包挂到 `Options.mcp_servers` 上：

```python
options = Options(
    system_prompt="...",
    name="my-agent",
    mcp_servers=(weather_mcp,),
)
```

进程内服务器的工具保留其**裸的** `@tool` 名字 —— 模型看到的是 `fetch_weather`，而不是 `mcp__weather-tools__fetch_weather`。服务器名字只是一个分组标签，而非命名空间，所以要挑选不会与内置工具冲突的工具名（用 `fetch_weather`，别用 `read`）。它的工具会被直接加入 Agent 的工具集；它们不需要 `allowed_tools` 条目。

> `mcp__{alias}__{tool}` 这个前缀属于**远程** MCP 服务器 —— host 会按回合连接它们，见[连接 MCP](connect-mcp.md)。那些工具需要命名空间，因为相互独立的第三方服务器确实会在工具名上撞车。

## 离线测试你的工具

用 `FakeLLMProvider` 脚本化一次调用，再用 `query` 驱动一个回合；`query` 返回完整的信封列表 —— 于是断言就能证明闭包真的运行了：

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

assert [e.payload.tool_name for e in result if e.type == "ToolCallStarted"] == [
    "fetch_weather"
]
```

`examples/custom_tool.py` 和 `examples/mcp_server.py` 是本页两部分内容的可运行版本。

## 另请参阅

- [SDK 参考](../reference/sdk.md) —— `@tool`、`create_sdk_mcp_server`、`ToolResult` 的完整签名
- [内置工具](../reference/tools.md) —— 那份 11 个名字的白名单及其风险等级
- [连接 MCP](connect-mcp.md) —— 远程 MCP 服务器
- [Guard 与 Observer](../concepts/guard-observer.md) —— 权限系统如何工作
