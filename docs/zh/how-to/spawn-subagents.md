# 派生子代理

**目标：** 在 `Options.agents` 中定义子代理，让父代理把工作扇出（fan out）给它们。

**开始之前：** 你已通过[你的第一个代理](../tutorials/first-agent.md)熟悉了 SDK。

## 定义子代理

子代理在 `Options.agents` 中以 `AgentDefinition` 条目声明。每个子代理是一份扁平配方——有自己的 prompt、工具和模型。子代理是叶子节点：`AgentDefinition` 没有 `agents` 字段，编译出的 `spawnable` 为空，因此它无法再向下委托。

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

这就是全部的 opt-in：填充 `agents` 会让 `compile_options` 把 `"delegation"` 折进父代理的身份，并把子代理名并入它的 `spawnable`，于是 `spawn_subagent` 控制工具挂载起来，且 `researcher` 出现在它 schema 的 enum 里。

## 派生如何工作

`spawn_subagent` 接受一个必填的 `spawns` 数组，元素为 `{agent, goal}`。一个元素是一次「委托并等待」；同一次调用里的多个元素是一次并发扇出——扇出的形状就是这个批量数组，而不是重复的工具调用。

当父模型调用它时，运行时：

1. 为每个元素创建一个子任务（Task），各自按其指名的代理定义配置，并在父级流上记录一个 `SubtaskSpawned` 事件。
2. 在一个屏障上挂起父代理（`TaskSuspended`）。
3. 将子任务运行至终止状态（完成、失败或取消），并为每个子任务在父级流上记录一个 `SubtaskCompleted`。
4. 唤醒父代理（`TaskWoken`），附带子任务的结果。

所以一次两元素的扇出会记录 `SubtaskSpawned`、`SubtaskSpawned`、`TaskSuspended`、`SubtaskCompleted`、`SubtaskCompleted`、`TaskWoken`。每个子任务都是独立的事件溯源任务，有自己的 trace、工具调用和 LLM 轮次；父代理只看到最终结果。

同一批的子任务并发运行。把 `NOETA_SUBTASK_CONCURRENCY` 设为 `0`、`false`、`off` 或 `no`，则强制改为顺序逐个排空。

一次调用也可以带上 `background=true` 且只有单个 spawn：父代理不被挂起，它拿到一张「已启动」回执，子任务的结果稍后作为一条 notice 到达——见 [Background subagents](https://github.com/initxy/noeta/blob/main/docs/adr/background-subagent.md)。

## 检查子任务流

```python
from noeta.sdk import Client

client = Client(options, provider=my_provider, workspace_dir="./")
outcome = client.start(goal="Analyze the codebase and report findings.")

# The parent's messages
parent_msgs = client.messages(outcome.task_id)

# Child task ids come off the envelope stream: SubtaskSpawned carries the
# child's id in payload.subtask_id; SubtaskCompleted carries its result.
envelopes = client.events(outcome.task_id)
spawned = [e for e in envelopes if e.type == "SubtaskSpawned"]
child_ids = [e.payload.subtask_id for e in spawned]

# A child's own stream reads like any other task's
child_msgs = client.messages(child_ids[0])
```

## 用 FakeLLMProvider 离线测试

一个 host 只有一个 provider，而子任务不过是它上面的一个普通 Task，所以脚本化测试不需要第二个 provider——把父代理和子任务的轮次按被消费的顺序写进同一个 `responses` 列表即可：

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

这里唯一的深层 import 是 `SPAWN_SUBAGENT_TOOL`：控制工具的 wire 名属于运行时词汇，不是配方 surface 的一部分，所以只有伪造模型工具调用的脚本才需要它。

可运行的单 spawn 版本见 `examples/spawn_subtask.py`。

## 另请参阅

- [任务模型](../concepts/task-model.md) — 父子任务关系
- [唤醒与恢复](../concepts/wake-resume.md) — `SubtaskCompleted` 如何唤醒父代理
- [SDK 参考](../reference/sdk.md) — `AgentDefinition`、`Options.agents`
