# 你的第一个 agent

[快速上手](quickstart.md)跑了一轮脚本化的对话。本教程在它之上构建一个真实的 agent：一个你自己写的工具、一份决定 agent 可以调用什么的白名单、一道在你点头之前拦住高风险调用的审批闸门，以及一个维持多轮对话的 `Client`。它仍然完全离线地跑在 `FakeLLMProvider` 上，所以整篇教程无需凭证即可复现，而且每个符号都来自 `noeta.sdk`。第 4–6 步共同构成一个程序——第 5、6 步里带缩进的代码块是在延续第 4 步打开的 `with` 块。

**前置条件：** Python 3.11+、`uv pip install noeta-sdk`，以及已经读完的[快速上手](quickstart.md)。

## 1. 定义一个工具

工具就是一个用 `@tool` 包装的普通函数 `fn(arguments, ctx) -> ToolResult`。装饰器的参数是这个工具的**声明身份**和它**面向模型的契约** —— 刻意由手写给出，绝不从 docstring 里刮取。

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


print(word_count.ref)
```

```
ToolRef(name='word_count', version='1', risk_level='high')
```

有四件事要知道：

- `version` 是**必填的**。`(name, version, risk_level)` 就是记录进已编译 agent spec 的身份，所以两个行为不同的工具永远不可能共用同一个身份。
- `description` 是模型理解这个工具做什么的唯一事实来源。它会被渲染进 provider 的工具 schema，且绝不会在系统提示里重复一遍。
- `input_schema` 是面向 LLM 的元数据，不是校验器。调用时没有任何东西拿 `arguments` 去对照它 —— 处理坏输入是你函数自己的事。
- `risk_level`（`"low"` / `"medium"` / `"high"`）是权限系统读取的字段。数单词是无害的；我们仍然把它标成 `"high"`，好让第 4 步有一道审批闸门可以演示。

`@tool` 返回一个 `DecoratedTool` —— 它既是可运行的工具，也是自身 `.ref` 的载体，因此闭包和它被记录下来的身份不可能各走各的。

## 2. 组装 Options

`Options` 是 agent 的配方：提示词、工具集、权限模式、子 agent。

```python
from noeta.sdk import Options

options = Options(
    system_prompt="You count words. Use the word_count tool.",
    name="word-counter",
    allowed_tools=(word_count,),
    permission_mode="default",
)
```

`allowed_tools` 是一份**替换式**白名单，而不是追加。给一个元组就意味着*恰好*是这些条目 —— 装饰过的工具按值给出，内置工具按名字给出 —— 所以这个 agent 只有 `word_count`，别无其他。`None`（默认值）选中完整的内置集合：`read`、`glob`、`grep`、`edit`、`write`、`apply_patch`、`shell_run`、`shell_poll`、`shell_kill`、`webfetch`、`web_search`。`()` 表示没有工具。`disallowed_tools` 从当前适用的基准列表里做减法；它从不做加法。

`permission_mode` 决定哪些调用要停下来等人工审批：

| 模式 | 需要设门的调用 |
| --- | --- |
| `default` | 每一个 `risk_level` 不为 `low` 的工具 |
| `acceptEdits` | 同上，但豁免内置的 `edit` / `write` / `apply_patch` |
| `bypassPermissions` | 无 |

`word_count` 是 `high`，所以在 `default` 下它会挂起并等你。

## 3. 脚本化模型

`FakeLLMProvider` 按顺序回放一列 `LLMResponse` 对象。把这一轮需要的两次模型往返写成脚本 —— 先是工具调用，然后是总结 —— 再加上第 6 步那次追问所需的一条。

```python
from noeta.sdk import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.sdk.testing import FakeLLMProvider


def say(text: str) -> LLMResponse:
    return LLMResponse(stop_reason="end_turn", content=[TextBlock(text=text)],
                       usage=Usage(uncached=1, output=1))


provider = FakeLLMProvider(responses=[
    LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id="wc-1", tool_name="word_count",
                              arguments={"text": "hello world from noeta"})],
        usage=Usage(uncached=1, output=1),
    ),
    say("That's 4 words."),
    say("You asked me to count the words in 'hello world from noeta'."),
])
```

脚本化的 provider 是一次性的 —— 每条响应按顺序被消费一次。

## 4. 驱动这一轮，并批准那次调用

`Client` 就是嵌入方 host 用的同一个驱动器。作为上下文管理器，它保证 `shutdown()` 一定被调用 —— Observer 拆卸、worker 停止、sandbox 回收。

```python
import tempfile
from pathlib import Path
from noeta.sdk import Client

with tempfile.TemporaryDirectory() as tmp:
    with Client(options, provider=provider, workspace_dir=Path(tmp),
                model="stub-model") as client:
        turn = client.start(goal="How many words are in 'hello world from noeta'?")
        print(turn.status, turn.wake_handle)
```

```
suspended approval-wc-1
```

这一轮并没有失败 —— 它**挂起**了。权限 Guard 看到在 `permission_mode="default"` 下有一次 `high` 风险的调用，于是 Task 把自己停在一个唤醒条件上，现在正等着一个人。`wake_handle` 精确说明了它在等什么：对调用 `wc-1` 的批准。

把它解决掉，这一轮就从停下的地方继续：

```python
        turn = client.approve(turn.task_id, call_id="wc-1")
        print(turn.status, turn.wake_handle)
        for item in client.messages(turn.task_id):
            print(item)
```

```
suspended noeta-code-next-goal

UserMessage(text="How many words are in 'hello world from noeta'?")
ToolUse(call_id='wc-1', tool_name='word_count', arguments={'text': 'hello world from noeta'})
ToolResultView(call_id='wc-1', tool_name='', success=True, output='"4 words"')
AssistantMessage(text="That's 4 words.")
```

仍然是 `suspended`，但换了一个 handle：`noeta-code-next-goal`（即常量 `NEXT_GOAL_WAKE_HANDLE`）表示"这场对话空闲了，正等你说下一句话"。这就是一个健康的多轮 agent 的静息状态 —— 一个挂起的 Task 在睡觉期间不花任何成本。`client.deny(...)` 是另一半；模型会被告知调用被拒绝，然后自己决定下一步做什么。

（`ToolResultView.tool_name` 是空的，因为被记录下来的结果载荷只携带 `call_id`。把它和前面那条 `ToolUse` 配对就能还原出名字。）

## 5. 这一轮内部发生了什么

<p align="center">
  <img src="../../assets/diagrams/turn-sequence.svg" alt="Noeta 的一轮 —— host 代码到 Client 到 Engine 到 Provider 到 Tool 到 EventLog，以及返回路径" width="820">
</p>

一轮就是 Engine 的 `run_one_step`，而它在内部循环，而不是每次模型往返之后就返回：

1. **组装（compose）** —— 把日志 fold 成一个 `View`：提示词与工具 schema 放进字节稳定的 `stable_prefix`，常驻内容放进 `semi_stable`，对话放进 `dynamic_suffix`。
2. **决策（decide）** —— Policy 询问 provider 并拿到一个 `Decision`（这里是 `tool_calls`）。
3. **派发（dispatch）** —— Guard 先跑（审批闸门就是在这里触发的）；被放行的调用执行，其结果被追加，循环回到第 1 步。
4. **落定（settle）** —— 一个非 `tool_calls` 的决策让这一步终止于挂起或终态。

步与步之间没有任何东西留在内存里：状态永远是 `fold(events)`，这正是为什么几分钟后才到来的一次批准，能让同一轮从它停下的地方精确恢复。参见[引擎与执行](../concepts/engine-execution.md)。

## 6. 让对话继续

因为 Task 正歇在 next-goal 这个 handle 上，再来一轮只需一次调用：

```python
        turn = client.send_goal(turn.task_id, goal="What did I just ask?")
        for item in client.messages(turn.task_id):
            print(item)
```

```
...                                    # the four items from the first turn
UserMessage(text='What did I just ask?')
AssistantMessage(text="You asked me to count the words in 'hello world from noeta'.")
```

这里没有会话对象，也没有对话句柄 —— 一场多轮对话*就是*一个 Task 反复接收用户输入，而 `task_id` 是你需要携带的唯一身份。`query` 是这一整套的一次性版本，把客户端生命周期折了进去。

## 7. 让它持久化

存储默认在内存里，所以账本会随进程一起消亡。把 `HostConfig.storage_path` 指向一个 SQLite 文件，另一个*不同的*客户端 —— 后来的进程、重启后的容器 —— 就能把同一场对话 fold 回来：

```python
from noeta.sdk import Client, HostConfig

db = HostConfig(storage_path="./noeta.sqlite")

with Client(options, provider=provider, model="stub-model", host_config=db) as c:
    task_id = c.start(goal="How many words are in 'hello world'?").task_id

# ... later, a fresh process ...
with Client(options, provider=provider, model="stub-model", host_config=db) as c:
    for item in c.messages(task_id):
        print(item)
```

第二个客户端从没被告知发生过什么；它 fold 了日志，然后就知道了。`storage_path` 也接受一个 `postgresql://` DSN，那正是多主机部署所用的形式。这就是[事件溯源](../concepts/event-sourcing.md)在起作用。

## 下一步

- **写更多工具** —— [构建自定义工具](../how-to/build-custom-tools.md)。
- **接上真实模型** —— [配置 Provider](../how-to/configure-provider.md)。
- **把工作委派出去** —— [生成子代理](../how-to/spawn-subagents.md)。
- **查阅这套面** —— [SDK 参考](../reference/sdk.md)，或看[核心概念](../concepts/index.md)理解它背后的模型。

可运行的 `examples/sdk_minimal.py`、`examples/custom_tool.py` 和 `examples/permission_gate.py` 就是这一模式的扩展版本。
