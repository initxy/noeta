# 把工作交给子 agent

在 `Options.agents` 里声明子 agent，父 agent 就多了一个 `Task` 工具，可以把活派出去：一次派一个、同时派几个，或者放到后台跑。每个子 agent 都是一个独立的持久化任务，有自己的事件日志。

## 定义子 agent

```python
from noeta.sdk import AgentDefinition, Client, Options
from noeta.sdk.providers import AnthropicProvider

researcher = AgentDefinition(
    description="Read-only researcher that finds and reports facts.",
    prompt="You are a researcher. Read files and report what you find. Do not edit anything.",
    tools=("Read", "Glob", "Grep", "Bash"),
)

options = Options(
    system_prompt="You are a lead engineer. Delegate research to the researcher subagent.",
    name="lead",
    agents={"researcher": researcher},
)

client = Client(options, provider=AnthropicProvider(), model="claude-sonnet-5", workspace_dir=".")
outcome = client.start(goal="Review the auth and billing modules.")
```

填上 `agents` 就够了：父 agent 会拿到 `Task` 工具，`subagent_type` 可选值里有 `researcher`。

| `AgentDefinition` 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `description` | 必填 | 父 agent 的模型在子 agent 列表里看到的说明 |
| `prompt` | 必填 | 子 agent 的系统提示词 |
| `tools` | `None`，即全部内置工具 | 内置工具名或 `@tool` 函数 |
| `model` | `None`，即用 host 默认模型 | 这个子 agent 用的模型；`haiku` 这类别名会解析成模型目录里的真实 id |
| `plugins` | `()` | 这个子 agent 启用的插件（比如 `"memory"`、`"mcp"`、`"delegation"`） |

子 agent 自己没有 `agents` 字段。要更深的层级，就把所有 agent 都声明在最外层；需要继续往下派活的子 agent 要写上 `plugins=("delegation",)`，没写就没有 `Task` 工具。官方的 `explore`、`plan`、`general-purpose` 都不带它。

## 父 agent 怎么调用

模型调用 `Task` 时传 `{description, prompt, subagent_type}`，一次调用对应一个子 agent。`prompt` 为空，或 `background` 不是布尔值，都会作为工具错误退回给模型：

| 模型发出 | 结果 |
| --- | --- |
| 一个 `Task` 调用 | 父 agent 等这个子 agent 做完 |
| 同一条回复里多个 `Task` 调用 | 子 agent 并行跑，父 agent 等全部做完 |
| 一个带 `background=true` 的 `Task` 调用 | 父 agent 接着干自己的，结果稍后以通知的形式送回 |

等待期间父任务是挂起，不占线程：派两个子 agent 会依次记下 `SubtaskSpawned` ×2、`TaskSuspended`、`SubtaskCompleted` ×2、`TaskWoken`。中途 worker 挂了，子任务做完后会有别的 worker 把父任务接着跑下去。

如果一条回复里 `Task` 和别的工具调用混在一起，这条回复会被退回，模型会被告知重发，任务本身不受影响。

### 子 agent 要审批或要人回答时

前台子 agent 发起需要审批的工具调用，或者向人提问时，和普通任务一样停下来等，而且这个等待会交到根任务上。设了 `Options.can_use_tool` 时，子 agent 的调用也会交给这个回调（回调签名是 `(tool_name, args)`，看不到是哪个任务发起的）。没设回调时，这一轮会交回给你：根任务的 `DriveOutcome.wake_handle` 就是子任务的 handle（比如 `approval-cw1`），`query()` 会报错，原因里写明 handle 和子任务 id。调用 `approve`、`deny` 或 `answer` 时，传根任务 id 或子任务 id 都行；子 agent 做完后父 agent 接着跑。子 agent 在等定时器时，根任务停在 `wake_handle=None`。

## 调并发

| 开关 | 默认值 | 作用 |
| --- | --- | --- |
| `NOETA_MAX_SUBTASK_CONCURRENCY`（环境变量） | `min(8, CPU 核数)` | 一次并行派发里最多同时跑几个子 agent |
| `NOETA_SUBTASK_CONCURRENCY`（环境变量） | 开 | 设成 `0` / `false` / `off` / `no` 就改成一个一个跑 |
| `HostConfig.max_background_subagents_per_root_task` | `8` | 每个根任务最多几个后台子 agent，超了这次调用会被拒绝 |

## 查看子 agent 的记录

```python
envelopes = client.events(outcome.task_id)
child_ids = [e.payload.subtask_id for e in envelopes if e.type == "SubtaskSpawned"]

for item in client.messages(child_ids[0]):
    print(item)
```

```
UserMessage(text='Review the auth module ...')
AssistantMessage(text='...')
Result(answer='...', status='completed')
```

子任务的记录和普通任务读法一样。父 agent 只看到每个子 agent 的最终结果。

## 离线测试

把父 agent 和子 agent 的每一轮回复按顺序写进同一个假 provider，做法见[测试](testing.md)。只派一个子 agent 的可运行例子见 `examples/spawn_subtask.py`。

## 下一步

- [任务与唤醒](../how-it-works/tasks-and-waking.md)：子任务做完怎么唤醒父任务
- [按租户隔离记忆](multi-tenant-memory.md)：子 agent 用自己的任务 id 查找记忆目录
- [ADR：后台子 agent](https://github.com/initxy/noeta/blob/main/docs/adr/background-subagent.md)
