# 构建自定义工具

**目标：** 用 `@tool` 定义你自己的工具，将它们接入代理，并可选地将它们打包为进程内 MCP 服务器。

**开始之前：** 你已完成[你的第一个代理](../tutorials/first-agent.md)的学习，并熟悉 `Options` 和 `Client`。

## 用 `@tool` 定义工具

工具是一个普通函数 `fn(arguments: dict, ctx: ToolContext) -> ToolResult`，用 `@tool` 装饰器包装：

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
    # ... 你的实现 ...
    return ToolResult(success=True, output=f"22°C in {city}")
```

### 装饰器参数

| 参数 | 是否必需 | 用途 |
| --- | --- | --- |
| `name` | 是 | 模型调用时使用的字符串。必须为 `snake_case`。 |
| `version` | 是 | 提供工具的身份指纹。行为变化时请递增。 |
| `risk_level` | 是 | `"low"`、`"medium"` 或 `"high"`。由权限系统使用。 |
| `description` | 是 | 模型理解工具语义的主要来源。请写得清晰明了。 |
| `input_schema` | 是 | 描述预期参数的 JSON Schema。面向 LLM 的元数据。 |

### `ToolResult`

成功调用返回 `ToolResult(success=True, output="...")`，失败返回 `ToolResult(success=False, output="error message")`。`output` 是模型读取的字符串——保持简洁清晰。

`ToolResult` 还接受 `artifacts`（`Artifact` 对象列表）和 `output_ref`（指向大输出的 `ContentRef`），但对大多数工具来说，`success` + `output` 就足够了。

## 将工具接入代理

通过 `Options.allowed_tools` 传入工具：

```python
from noeta.sdk import Options, Client

options = Options(
    system_prompt="You are a weather assistant.",
    name="weather-bot",
    allowed_tools=(fetch_weather,),
)

client = Client(options, provider=my_provider, workspace_dir="./")
```

当 `allowed_tools` 是 `DecoratedTool` 实例的元组时，只有这些工具可用。传入 `None` 可获得所有内置工具加上你的工具，或使用 `disallowed_tools` 从完整集合中减去。

## 风险等级与权限

你工具上的 `risk_level` 与 `permission_mode` 相互作用：

| 风险 | `default` 模式 | `acceptEdits` 模式 | `bypassPermissions` 模式 |
| --- | --- | --- | --- |
| `low` | 自动批准 | 自动批准 | 自动批准 |
| `medium` | 需要批准 | 需要批准 | 自动批准 |
| `high` | 需要批准 | 需要批准 | 自动批准 |

将写入文件、运行命令或发起外部 API 调用的工具标记为 `"high"`。只读工具为 `"low"`。

## 将工具打包为 MCP 服务器

如果你想在多个代理之间共享工具，或通过 MCP 协议提供它们，请将它们打包为进程内 MCP 服务器：

```python
from noeta.sdk import create_sdk_mcp_server

weather_mcp = create_sdk_mcp_server(
    name="weather-tools",
    version="1.0.0",
    tools=(fetch_weather,),
)
```

然后在 `Options` 中挂载：

```python
options = Options(
    system_prompt="...",
    name="my-agent",
    mcp_servers=(weather_mcp,),
    allowed_tools=None,  # 所有内置工具 + MCP 工具
)
```

MCP 服务器的工具在工具允许列表中显示为 `mcp__weather-tools__fetch_weather`。代理可以像调用内置工具一样调用它们。

## 离线测试你的工具

使用 `FakeLLMProvider` 脚化对你工具的调用并验证它能正常运行：

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

用 `Client` 驱动它，并在消息流中验证 `ToolResult`。

## 另请参阅

- [SDK 参考](../reference/sdk.md) — `@tool`、`create_sdk_mcp_server`、`ToolResult` 完整签名
- [连接 MCP](connect-mcp.md) — 注册远程 MCP 服务器
- [Guard 与 Observer](../concepts/guard-observer.md) — 权限系统如何工作
