# 动手做一个 agent

这篇教程带你做一个完整的 agent：有你自己写的工具，有白名单限定它能调什么，有审批关卡在你点头前拦住调用，能多轮对话，数据存下来进程重启也不丢。全程大约 60 行 Python，接的是真实的 Claude 模型。

**准备：** Python 3.11+，`uv pip install noeta-sdk`，环境变量里设好 `ANTHROPIC_API_KEY`。先跑一遍[快速上手](quickstart.md)会更顺。

::: tip 输出会不一样
这里接的是真模型，你看到的措辞和 `call_id` 会和下面贴的不同。但每一步的状态和结构是一样的。
:::

## 1. 写一个工具

工具就是一个普通函数 `fn(arguments, ctx) -> ToolResult`，套上 `@tool`：

```python
from noeta.sdk import ToolContext, ToolResult, tool


@tool(
    name="word_count",
    version="1",
    risk_level="high",
    description="Count the whitespace-separated words in `text`.",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
)
def word_count(arguments: dict, ctx: ToolContext) -> ToolResult:
    text = str(arguments.get("text", ""))
    return ToolResult(success=True, output=f"{len(text.split())} words")
```

- 模型看到的是 `description` 和 `input_schema`。运行时不会拿 schema 去校验 `arguments`，坏输入要函数自己处理。
- `version` 必填，`(name, version, risk_level)` 三者合起来就是这个工具的身份。
- 数单词本来没什么风险，这里故意标成 `risk_level="high"`，好让第 3 步演示审批。

## 2. 限定能调哪些工具

```python
from noeta.sdk import Options

options = Options(
    system_prompt="You count words. Always use the word_count tool.",
    name="word-counter",
    allowed_tools=(word_count,),
    permission_mode="default",
)
```

`allowed_tools` 写的是完整清单，不是在默认基础上追加：这个 agent 只有 `word_count`。不写这个参数就是全部内置工具；也可以混着写：`("Read", "Grep", word_count)`。

`permission_mode` 决定哪些调用要等人批：

| 模式 | 需要审批的调用 |
| --- | --- |
| `default` | 所有 `risk_level` 不是 `low` 的工具 |
| `acceptEdits` | 同上，但内置的 `Edit`、`Write` 不用批 |
| `bypassPermissions` | 都不用批 |

## 3. 跑一轮，停在审批上

```python
from noeta.sdk import Client
from noeta.sdk.providers import AnthropicProvider

with Client(options, provider=AnthropicProvider(), model="claude-sonnet-5") as client:
    turn = client.start(goal="How many words are in 'hello world from noeta'?")
    print(turn.status, turn.wake_handle)
```

```
suspended approval-call_ln6dcrmn0w80aqou15fyyryt
```

这一轮没有失败。模型想调 `word_count`，权限检查发现是 `high` 风险，任务就**挂起**了。`wake_handle` 写明了它在等什么：等你批准这次调用。挂起的任务不占线程，等多久都不花钱。

## 4. 批准调用

接着写在 `with` 块里：

```python
    call_id = turn.wake_handle.removeprefix("approval-")
    turn = client.approve(turn.task_id, call_id=call_id)
    print(turn.status, turn.wake_handle)
    for item in client.messages(turn.task_id):
        print(item)
```

```
suspended noeta-code-next-goal
UserMessage(text="How many words are in 'hello world from noeta'?")
ToolUse(call_id='call_ln6dcrmn0w80aqou15fyyryt', tool_name='word_count', arguments={'text': 'hello world from noeta'})
ToolResultView(call_id='call_ln6dcrmn0w80aqou15fyyryt', tool_name='', success=True, output='"4 words"')
AssistantMessage(text='There are **4 words** in "hello world from noeta".')
```

工具跑了，模型也回答了。任务又挂起了，这次停在 `noeta-code-next-goal`（常量 `NEXT_GOAL_WAKE_HANDLE`）上，意思是对话空着，等你说下一句。

- 想拒绝就用 `client.deny(task_id, call_id=..., reason=...)`，模型会收到拒绝，自己决定接下来怎么办。
- 想用代码自动判断，就设 `Options(can_use_tool=lambda name, args: ...)`：返回 `True` 放行，`False` 拒绝。它的决定和人工审批一样记进日志。

## 5. 接着聊

```python
    turn = client.send_goal(turn.task_id, goal="What did I just ask you?")
    print(client.messages(turn.task_id)[-1])
```

```
AssistantMessage(text='You just asked: "How many words are in \'hello world from noeta\'?" — and the answer was 4 words.')
```

一段对话就是一个任务在不断接收新消息。轮与轮之间，你只需要记住 `task_id`。

## 6. 存下来

默认所有数据都在内存里，进程一退就没了。把 `HostConfig.storage_path` 指向一个 SQLite 文件：

```python
from noeta.sdk import HostConfig

db = HostConfig(storage_path="./noeta.sqlite")

with Client(options, provider=AnthropicProvider(), model="claude-sonnet-5",
            host_config=db) as client:
    ...  # 第 3–5 步不变；最后把 turn.task_id 打印出来
```

之后换一个进程，只凭这个文件和 `task_id` 就能接上同一段对话：

```python
with Client(options, provider=AnthropicProvider(), model="claude-sonnet-5",
            host_config=db) as client:
    print(client.task_status(task_id))
    turn = client.send_goal(task_id, goal="Count the words in 'one two three'.")
    print(turn.status, turn.wake_handle)
```

```
TaskStatus(task_id='task-53b3…', status='suspended', closed=False, wake_handle='noeta-code-next-goal', parent_task_id=None)
suspended approval-call_xwfag88siz0teynq8on9bi57
```

新的 client 事先什么都不知道，它从事件日志里把任务重建出来，接着往下跑，连审批关卡都还在。多台机器共用一个存储时，`storage_path` 也可以填 `postgresql://` 连接串。

## 下一步

- [自定义工具](../guides/tools.md)：返回结果、报错、风险等级、打包
- [接入模型](../guides/models.md)：OpenAI 兼容网关，以及注册自己的模型 id
- [离线测试](../guides/testing.md)：用脚本模拟模型，对事件日志做断言
