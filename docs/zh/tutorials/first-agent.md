# 你的第一个代理：20 分钟构建一个 SDK 代理

**你将完成以下操作：** 用 `@tool` 定义自定义工具、组装 `Options`、用 `Client` 驱动代理，并查看生成的消息流。所有操作都在进程内完成 —— 无需服务器，无需 API 密钥。我们使用 `FakeLLMProvider`，因此示例完全离线且结果确定。

## 前置条件

- Python 3.11+
- 已安装 `noeta-sdk`（`pip install noeta-sdk`）

## 1. 定义工具

工具就是用 `@tool` 装饰器包装的普通函数。函数接收 `(arguments: dict, ctx: ToolContext)` 并返回 `ToolResult`。`version` 字段是必需的 —— 它用于生成工具的身份指纹，因此修改它会告知 runtime 该工具的行为可能已发生变化。

```python
from noeta.sdk import tool
from noeta.protocols.tool import ToolContext, ToolResult

_WORD_COUNT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

@tool(name="word_count", version="1", risk_level="low",
      input_schema=_WORD_COUNT_SCHEMA)
def word_count(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Count whitespace-separated words."""
    text = str(arguments.get("text", ""))
    count = len(text.split())
    return ToolResult(success=True, output=f"{count} words")
```

`input_schema` 是面向 LLM 的元数据 —— 它告诉模型该工具期望什么参数。调用时不会验证它；函数本身负责处理非法输入。

## 2. 构建 Options

`Options` 是你代理的不可变配方。它包含系统提示词、工具白名单、权限模式以及所有子代理定义。

```python
from noeta.sdk import Options

options = Options(
    system_prompt="You count words. Use the word_count tool.",
    name="word-counter",
    allowed_tools=(word_count,),
    permission_mode="bypassPermissions",
)
```

`allowed_tools` 控制模型可以调用哪些工具。传入 `None` 可获得全部 13 个内置工具，或传入 `DecoratedTool` 实例的元组（如我们的 `word_count`）以限制可用范围。

`permission_mode="bypassPermissions"` 表示工具调用不受限 —— 对于 `word_count` 这类低风险工具很有用。对于写入文件或运行 shell 命令的工具，使用 `"default"`（用户必须批准每次调用）或 `"acceptEdits"`（编辑自动批准，shell 调用仍需批准）。

## 3. 创建脚本化 provider

本教程中我们使用 `FakeLLMProvider` —— 一个确定性替身，它返回预设的响应序列。在实际部署中，你会使用 `AnthropicProvider` 或 `OpenAICompatProvider`。

```python
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.protocols.messages import (
    LLMResponse, TextBlock, ToolUseBlock, Usage,
)

provider = FakeLLMProvider(
    responses=[
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="wc-1",
                    tool_name="word_count",
                    arguments={"text": "hello world from noeta"},
                )
            ],
            usage=Usage(uncached=1, output=1),
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="That's 4 words.")],
            usage=Usage(uncached=1, output=1),
        ),
    ]
)
```

脚本化 provider 调用一次 `word_count`（传入 "hello world from noeta"），然后以 "That's 4 words." 结束。

## 4. 驱动代理

```python
from pathlib import Path
import tempfile
from noeta.sdk import Client

with tempfile.TemporaryDirectory() as tmp:
    client = Client(
        options,
        provider=provider,
        workspace_dir=Path(tmp),
        model="stub-model",
        multi_turn=False,
    )
    try:
        outcome = client.start(goal="How many words are in 'hello world from noeta'?")
        messages = client.messages(outcome.task_id)
        for msg in messages:
            print(msg)
    finally:
        client.shutdown()
```

`Client` 就是平台所嵌入的那套主机机制 —— 它创建一个临时任务，驱动它到终止状态，然后关闭。`client.messages(task_id)` 返回 fold 后的人类可读视图：用户消息、工具使用、工具结果、助手回复。

运行后你应该看到类似以下的输出：

```
UserMessage(text="How many words are in 'hello world from noeta'?")
ToolUse(call_id='wc-1', tool_name='word_count', arguments={'text': 'hello world from noeta'})
ToolResultView(call_id='wc-1', tool_name='word_count', success=True, output='4 words')
AssistantMessage(text="That's 4 words.")
Result(answer="That's 4 words.", status='completed')
```

## 5. 刚才发生了什么

每一步 —— 用户消息、工具调用、工具结果、助手回复 —— 都被追加到内存中的 EventLog。`messages()` 调用将该日志 fold 成人类可读视图。如果你让客户端指向一个 SQLite 文件而不是 `:memory:`，你可以关闭进程、启动新进程，然后 fold 同一个日志来恢复完全相同的状态。这就是 [事件溯源](../concepts/event-sourcing.md) 的实际应用。

工具调用没有被限制，因为我们设置了 `permission_mode="bypassPermissions"`。如果使用 `"default"`，`PermissionGuard` 会拦截调用并要求通过 `client.approve()` 显式批准。参见 [Guard 与 Observer](../concepts/guard-observer.md) 了解其工作原理。

## 下一步

- **接入真实模型** —— [配置 provider](../how-to/configure-provider.md) 展示了如何接入 Anthropic 或兼容 OpenAI 的端点。
- **构建更多工具** —— [构建自定义工具](../how-to/build-custom-tools.md) 涵盖了 `risk_level`、工具版本，以及将工具打包成 MCP 服务器。
- **扇出到子代理** —— [生成子代理](../how-to/spawn-subagents.md) 演示了并行任务执行。
- **查阅完整 SDK 接口** —— [SDK 参考](../reference/sdk.md) 记录了 `noeta.sdk` 中的每一个符号。
- **运行示例** —— `examples/sdk_minimal.py` 和 `examples/custom_tool.py` 更详细地扩展了这个模式。
