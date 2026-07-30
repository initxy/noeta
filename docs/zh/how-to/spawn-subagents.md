# 派生子代理

**目标：** 在 `Options.agents` 中定义子代理，启用委派功能，让父代理将工作并行分发给子代理。

**开始之前：** 你已通过[你的第一个代理](../tutorials/first-agent.md)熟悉了 SDK。

## 定义子代理

子代理在 `Options.agents` 中以 `AgentDefinition` 条目声明。每个子代理是一个扁平配方——有自己的 prompt、工具和模型。子代理是叶子节点；它们不能再嵌套代理。

```python
from noeta.sdk import Options, AgentDefinition

researcher = AgentDefinition(
    description="Read-only researcher that finds and reports facts.",
    prompt="You are a researcher. Read files and report what you find. Do not edit anything.",
    tools=("read", "glob", "grep", "shell_run"),  # 只读子集
    model=None,  # 继承父代理的模型
)

options = Options(
    system_prompt="You are a lead engineer. Delegate research to the researcher sub-agent.",
    name="lead",
    agents={"researcher": researcher},
)
```

这样就够了：填充 `agents` 本身就是那个开关。`compile_options` 会自动合成父代理的 `delegation` 能力，并把子代理名并入 `spawnable`，于是 `spawn_subagent` 控制工具会带着 `researcher` 出现在模型可见的 schema 里。

## 派发如何工作

当父模型调用 `spawn_subagent(agent="researcher", goal="...")` 时，运行时：

1. 创建一个子任务（Task），拥有自己的 EventLog，根据 `researcher` 代理定义配置。
2. 运行子任务至终止状态（完成、失败或取消）。
3. 将子任务的结果作为 `SubtaskCompleted` 事件记录到父级日志中。
4. 唤醒挂起的父代理，附带子任务的结果。

子任务是一个独立的事件溯源任务——它有自己的 trace、自己的工具调用和自己的 LLM 轮次。父代理只看到最终结果。

## 并行分发

真实 LLM（非脚本化 provider）可以在同一轮中多次调用 `spawn_subagent` 来分发工作：

```python
# 模型可能在单轮中产生以下调用：
spawn_subagent(agent="researcher", goal="Analyze the auth module")
spawn_subagent(agent="researcher", goal="Analyze the billing module")
spawn_subagent(agent="researcher", goal="Analyze the API module")
```

三个子任务并发运行。父代理挂起，直到该轮中所有派发的子任务完成，然后通过 `SubtaskCompleted` 唤醒事件恢复，每个子任务的结果都可用。

## 检查子任务流

运行结束后，你可以单独检查子任务的事件流：

```python
from noeta.sdk import Client

client = Client(options, provider=my_provider, workspace_dir="./")
outcome = client.start(goal="Analyze the codebase and report findings.")

# 父级消息
parent_msgs = client.messages(outcome.task_id)

# 从信封流中查找子任务 ID：SubtaskSpawned 的 payload.subtask_id 是子任务 id，
# SubtaskCompleted 携带它的结果。
envelopes = client.events(outcome.task_id)
spawned = [e for e in envelopes if e.type == "SubtaskSpawned"]
child_ids = [e.payload.subtask_id for e in spawned]

# 子任务的事件流和任何别的任务一样读
child_msgs = client.messages(child_ids[0])
```

每个子任务在 Web 界面中都有自己的 trace——在父级会话视图中查找子任务链接。

## 使用 FakeLLMProvider 离线测试

要在没有真实 API 密钥的情况下测试派发，请脚本化父代理的轮次：

```python
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.protocols.messages import (
    LLMResponse, TextBlock, ToolUseBlock, Usage,
)
from noeta.policies.control_semantics import SPAWN_SUBAGENT_TOOL

def _finish(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


provider = FakeLLMProvider(
    responses=[
        # 1. 父代理派发
        LLMResponse(
            stop_reason="tool_use",
            content=[ToolUseBlock(
                call_id="spawn-1",
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": "researcher", "goal": "find the bug"},
            )],
            usage=Usage(uncached=1, output=1),
        ),
        # 2. **子任务**的那一轮
        _finish("researcher: the bug is in auth.py"),
        # 3. 父代理带着子任务结果恢复
        _finish("The researcher found the bug in auth.py."),
    ]
)
```

子代理继承父级的 provider——一个 host 只有一个 provider，子任务不过是它上面的一个普通 Task。所以用 `FakeLLMProvider` 时**不需要**再接第二个 provider：把父代理和子任务的轮次按被消费的顺序（父派发 → 子任务 → 父恢复）写进同一个 `responses` 列表即可，如上。

`examples/spawn_subtask.py` 就是这个流程的可运行版本。

## 另请参阅

- [任务模型](../concepts/task-model.md) — 父子任务关系
- [唤醒与恢复](../concepts/wake-resume.md) — `SubtaskCompleted` 如何唤醒父代理
- [SDK 参考](../reference/sdk.md) — `AgentDefinition`、`Options.agents`
- `examples/spawn_subtask.py` — 完整可运行示例
