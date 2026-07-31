# 生成子代理

本指南教你在 `Options.agents` 里定义子 agent，并让父 agent 把工作分发给它们。你需要[你的第一个 agent](../tutorials/first-agent.md) 里的 SDK 基础。

## 1. 定义子 agent

子 agent 以 `AgentDefinition` 条目的形式声明在 `Options.agents` 里。每个子 agent 都是一份扁平的配方 —— 自己的提示词、工具和模型。子 agent 是叶子：`AgentDefinition` 没有 `agents` 字段，编译出来的 `spawnable` 是空的，因此它无法继续往下委派。

```python
from noeta.sdk import Options, AgentDefinition

researcher = AgentDefinition(
    description="Read-only researcher that finds and reports facts.",
    prompt="You are a researcher. Read files and report what you find. Do not edit anything.",
    tools=("read", "glob", "grep", "shell_run"),  # read-only subset
    model=None,  # inherits the host default
)

options = Options(
    system_prompt="You are a lead engineer. Delegate research to the researcher subagent.",
    name="lead",
    agents={"researcher": researcher},
)
```

这就是全部的开启动作：填上 `agents` 会让 `compile_options` 把 `"delegation"` 折进父 agent 的身份，并把子 agent 的名字并入它的 `spawnable`，从而挂上 `spawn_subagent` 控制工具，并在它的 schema 枚举里带上 `researcher`。

## 2. 理解一次 spawn 记录了什么

`spawn_subagent` 接收一个必填的 `spawns` 数组，元素是 `{agent, goal}`。一个条目就是一次"委派并等待"；同一次调用里的多个条目是一次并发扇出 —— 扇出的形状体现在批量数组上，而不是靠重复的工具调用。

当父 agent 的模型调用它时，运行时会：

1. 为每个条目创建一个子 Task，各自按其具名的 agent 定义配置，并在父 Task 的流上记录一个 `SubtaskSpawned` 事件。
2. 把父 Task 挂在一个屏障上（`TaskSuspended`）。
3. 把子 Task 跑到终态（完成、失败或取消），并为每一个在父 Task 的流上记录一个 `SubtaskCompleted`。
4. 带着子 Task 的结果唤醒父 Task（`TaskWoken`）。

所以一次两条目的扇出会记录 `SubtaskSpawned`、`SubtaskSpawned`、`TaskSuspended`、`SubtaskCompleted`、`SubtaskCompleted`、`TaskWoken`。每个子 Task 都是一个独立的事件溯源 Task，有自己的 trace、工具调用和 LLM 轮次；父 Task 只看到最终结果。

同一批次里的子 Task 在一个有界的进程内池上并发运行（`min(8, CPU count)`）。两个环境变量可以调它：`NOETA_MAX_SUBTASK_CONCURRENCY` 直接覆盖上限，而把 `NOETA_SUBTASK_CONCURRENCY` 设为 `0`、`false`、`off` 或 `no` 则强制改为顺序排空。

一次调用也可以在只有单个 spawn 时传 `background=true`：父 Task 不会挂起，它拿到一张"已启动"的回执，子 Task 的结果稍后以通知的形式到达 —— 见[后台子代理](https://github.com/initxy/noeta/blob/main/docs/adr/background-subagent.md)。

## 3. 查看子 Task 的事件流

```python
from noeta.sdk import Client

client = Client(options, provider=my_provider, workspace_dir="./")
outcome = client.start(goal="Analyze the codebase and report findings.")

# Child task ids come off the envelope stream: SubtaskSpawned carries the
# child's id in payload.subtask_id; SubtaskCompleted carries its result.
envelopes = client.events(outcome.task_id)
child_ids = [e.payload.subtask_id for e in envelopes if e.type == "SubtaskSpawned"]
print(child_ids)

# A child's own stream reads like any other task's.
for item in client.messages(child_ids[0]):
    print(item)
```

```
['task-3f9c1a7e4b2d4c8f9a1e6b0d5c7a2e13']
UserMessage(text='analyze the auth module')
AssistantMessage(text='researcher: auth looks fine')
Result(answer='researcher: auth looks fine', status='completed')
```

## 4. 离线测试它

每个 host 只有一个 provider，而子 Task 只是它上面的一个普通 Task，所以脚本化测试不需要第二个 provider —— 把父 Task 和各子 Task 的轮次按被消费的顺序写进同一个 `responses` 列表：

```python
from noeta.sdk import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.sdk.testing import FakeLLMProvider
from noeta.policies.control_semantics import SPAWN_SUBAGENT_TOOL


def _finish(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


provider = FakeLLMProvider(
    responses=[
        # 1. the parent fans out to two children in one call
        LLMResponse(
            stop_reason="tool_use",
            content=[ToolUseBlock(
                call_id="spawn-1",
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"spawns": [
                    {"agent": "researcher", "goal": "analyze the auth module"},
                    {"agent": "researcher", "goal": "analyze the billing module"},
                ]},
            )],
            usage=Usage(uncached=1, output=1),
        ),
        # 2. and 3. the two children's turns
        _finish("researcher: auth looks fine"),
        _finish("researcher: billing looks fine"),
        # 4. the parent resumes with both results
        _finish("Both modules reviewed."),
    ]
)
```

`SPAWN_SUBAGENT_TOOL` 是这里唯一一处深层导入：控制工具的线上名字属于运行时词汇，不属于配方面，所以只有需要伪造模型工具调用的脚本才会用到它。

可运行的单次 spawn 版本见 `examples/spawn_subtask.py`。

## 下一步

- [任务模型](../concepts/task-model.md) —— 父子 Task 的关系
- [唤醒与恢复](../concepts/wake-resume.md) —— `SubtaskCompleted` 如何唤醒父 Task
- [扩展平面](../architecture/extension-planes.md) —— 为什么 `delegation` 是一个 activation
- [SDK 参考](../reference/sdk.md) —— `AgentDefinition`、`Options.agents`
