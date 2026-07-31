# 你的第一个代理

**你将构建：** 一个用 `@tool` 定义的自定义工具、一份挂载它的 `Options` 配方，以及一个驱动单轮对话的 `Client`——然后读回 fold 后的消息视图。一切都在进程内针对离线的 `FakeLLMProvider` 运行，因此无需服务器，也无需 API key。

所有符号都来自 `noeta.sdk`，也就是 SDK 唯一的公共导入面。

## 前置条件

- Python 3.11+
- `pip install noeta-sdk`

## 1. 定义工具

工具就是一个用 `@tool` 包装的普通函数 `fn(arguments, ctx) -> ToolResult`。`version` 是必需的：`(name, version, risk_level)` 是该工具在编译后的 agent spec 中声明的身份，因此两个行为不同的工具永远不可能共用同一个身份。

```python
from noeta.sdk import ToolContext, ToolResult, tool

_WORD_COUNT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


@tool(
    name="word_count",
    version="1",
    risk_level="low",
    input_schema=_WORD_COUNT_SCHEMA,
    description="Count the whitespace-separated words in `text`.",
)
def word_count(arguments: dict, ctx: ToolContext) -> ToolResult:
    text = str(arguments.get("text", ""))
    return ToolResult(success=True, output=f"{len(text.split())} words")
```

`input_schema` 和 `description` 是面向 LLM 的元数据，会被渲染进 provider 的工具 schema。`arguments` 在调用时不会针对 schema 做校验——函数自己负责处理非法输入。

`@tool` 返回一个 `DecoratedTool`：这一个对象既是可运行的工具，也是其身份 ref 的载体，因此两者不会各自漂移。

## 2. 组装 Options

`Options` 是不可变的配方：系统提示词、工具列表、权限模式，以及所有子代理定义。

```python
from noeta.sdk import Options

options = Options(
    system_prompt="You count words. Use the word_count tool.",
    name="word-counter",
    allowed_tools=(word_count,),
    permission_mode="bypassPermissions",
)
```

`allowed_tools` 是替换，而非追加。一个元组表示*恰好*这些条目——装饰过的工具按值传入，内置工具按名传入。`None` 选中完整的内置集合：11 个名字（`read`、`glob`、`grep`、`edit`、`write`、`apply_patch`、`shell_run`、`shell_poll`、`shell_kill`、`webfetch`、`web_search`），其中 10 个无需额外配置即可挂载——`web_search` 只在设置了 `NOETA_WEB_SEARCH_API_KEY` 时才构建。memory、browser、`open_app`、`run_skill_script` 和 MCP 工具不在这个集合里；它们通过一次 activation 或一次宿主注入来挂载。参见[工具目录](../reference/tools.md)。

`permission_mode` 决定哪些调用会停下来等待批准：

| 模式 | 需要门控的调用 |
| --- | --- |
| `default` | `risk_level` 不是 `low` 的每个工具 |
| `acceptEdits` | 同上，但排除 `edit` / `write` / `apply_patch` |
| `bypassPermissions` | 无 |

`word_count` 是 `low`，因此在三种模式下都无需人工介入即可运行；我们设为 `bypassPermissions`，让这次演练免去一次批准往返。

## 3. 脚本化一个 provider

`FakeLLMProvider` 返回一段预先脚本化的 `LLMResponse` 序列，并记录它收到的每一次请求。真实部署则改用一个在线的 provider——参见[配置 provider](../how-to/configure-provider.md)。

```python
from noeta.sdk import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.sdk.testing import FakeLLMProvider

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

这段脚本调用一次 `word_count`，然后结束。

## 4. 驱动代理

```python
import tempfile
from pathlib import Path

from noeta.sdk import Client

with tempfile.TemporaryDirectory() as tmp:
    with Client(
        options,
        provider=provider,
        workspace_dir=Path(tmp),
        model="stub-model",
        multi_turn=False,
    ) as client:
        outcome = client.start(goal="How many words are in 'hello world from noeta'?")
        for msg in client.messages(outcome.task_id):
            print(msg)
```

`Client` 就是嵌入宿主所使用的同一个驱动器。作为上下文管理器，它保证 `shutdown()` 被调用（拆除 observer、停止 worker、回收 sandbox）。`multi_turn=False` 让本轮抵达真正的 `TaskCompleted` 终态，而不是挂起等待下一个目标。

输出：

```
UserMessage(text="How many words are in 'hello world from noeta'?")
ToolUse(call_id='wc-1', tool_name='word_count', arguments={'text': 'hello world from noeta'})
ToolResultView(call_id='wc-1', tool_name='', success=True, output='"4 words"')
AssistantMessage(text="That's 4 words.")
Result(answer="That's 4 words.", status='completed')
```

`ToolResultView.tool_name` 为空，是因为记录下来的结果载荷只带了 `call_id`；把它和前面的 `ToolUse` 配对即可还原出名字。

### 或者：用一次调用取代四行代码

`query` 做的是同一件事，只是把客户端生命周期折叠了进去。用它替换上面整段代码（脚本化 provider 是一次性的，因此要新建一个）：

```python
from noeta.sdk import query

with tempfile.TemporaryDirectory() as tmp:
    result = query(
        options,
        "How many words are in 'hello world from noeta'?",
        provider=provider,
        workspace_dir=Path(tmp),
        model="stub-model",
    )

print(result.answer())     # raises QueryFailedError if the task did not complete
print(result.messages())   # the same folded view
```

`QueryResult` *就是*完整的 `list[EventEnvelope]`，其 `messages()` 和 `answer()` 会在临时客户端被拆除之前完成 fold 与解引用。

## 5. 刚才发生了什么

每一步——用户消息、工具调用、工具结果、助手回复——都作为一个不可变的 envelope 被追加到 event log；`messages()` 把这条日志 fold 成人类可读的视图。存储默认在内存中。把客户端指向 SQLite，同一个 fold 就能在一个全新进程里恢复出同样的状态：

```python
from noeta.sdk import HostConfig

client = Client(options, provider=provider, workspace_dir=Path(tmp),
                host_config=HostConfig(storage_path="noeta.sqlite"))
```

这就是[事件溯源](../concepts/event-sourcing.md)在起作用。门控——当某个模式启用它时——发生在一个 [Guard](../concepts/guard-observer.md) 中，它会挂起本轮，直到 `client.approve()` 或 `client.deny()` 决断那个待处理的调用。

## 下一步

- **接入真实模型** —— [配置 provider](../how-to/configure-provider.md)。
- **构建更多工具** —— [构建自定义工具](../how-to/build-custom-tools.md) 涵盖风险级别、版本管理，以及把工具打包进一个 MCP 服务器。
- **扇出到子代理** —— [生成子代理](../how-to/spawn-subagents.md)。
- **查阅完整接口面** —— [SDK 参考](../reference/sdk.md)。
- **阅读可运行代码** —— `examples/sdk_minimal.py`、`examples/custom_tool.py` 和 `examples/permission_gate.py` 在这个模式之上做了扩展。
