# 子代理委派 { #sub-agent-delegation }

在 `Options.agents` 中声明子代理并开启父代理的 `delegation` 能力。SDK 暴露一个模型可见的 `spawn_subagent` 控制接口——当父模型调用它时，运行时生成一个从命名子代理*自己的*配置（自己的 prompt、工具、model）构建的子 Task，运行它到终止状态，将其结果 fold 回来，然后恢复父代理。

## 示例：main + researcher { #example-main--researcher }

```python
import tempfile
from pathlib import Path

from noeta.client import AgentDefinition, Client, Options
from noeta.policies.react import SPAWN_SUBAGENT_TOOL
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.testing.fake_llm import FakeLLMProvider

# --- 1. Declare child agents -----------------------------------------------
#
# AgentDefinition is a flat, non-recursive recipe for a child agent.
# It has no agents/subagents field — deep trees must be expressed by
# declaring every agent at the top level.

main = Options(
    system_prompt="Delegate research to your sub-agent, then summarise.",
    name="main",
    agents={
        "researcher": AgentDefinition(
            description="Read-only researcher that returns a finding.",
            prompt="You are a researcher. Investigate and report back.",
            tools=["read", "glob", "grep"],
        ),
    },
    permission_mode="bypassPermissions",
)

# When Options.agents is non-empty, the compiler automatically opens
# delegation capability and adds the child names to spawnable.
# You can also set capabilities explicitly:
#
#   from noeta.agent.spec import Capabilities
#   options = Options(
#       ...,
#       capabilities=Capabilities(
#           delegation=True,
#           spawnable=("researcher",),
#       ),
#   )

# --- 2. Scripted provider (for demo) ---------------------------------------
#
# In a real deployment, the live model decides when to delegate.
# Here we script three turns:
#   Turn 1: parent calls spawn_subagent("researcher", "find the answer")
#   Turn 2: child finishes with "the answer is 42"
#   Turn 3: parent finishes with a summary

def spawn_call(agent: str, goal: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="spawn-1",
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": agent, "goal": goal},
            )
        ],
        usage=Usage(uncached=1, output=1),
    )

def finish(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )

provider = FakeLLMProvider(
    responses=[
        spawn_call("researcher", "find the answer"),
        finish("researcher: the answer is 42"),
        finish("Done — the researcher reported 42."),
    ]
)

# --- 3. Run with Client (multi-turn) ---------------------------------------
#
# We use Client (not query) because we need the parent's task id to
# inspect the spawned child's stream.

with tempfile.TemporaryDirectory(prefix="noeta-spawn-") as tmp:
    client = Client(
        main,
        provider=provider,
        workspace_dir=Path(tmp),
        model="stub-model",
        multi_turn=False,
    )
    try:
        outcome = client.start(goal="Find the answer and tell me.")
        parent_id = outcome.task_id

        # Inspect the parent's event stream for the spawn.
        parent_events = client.events(parent_id)
        spawned = [e for e in parent_events if e.type == "SubtaskSpawned"]
        child_id = spawned[0].payload.subtask_id if spawned else None

        print(f"parent task: {parent_id}")
        print(f"spawned child task: {child_id}")
        # → parent task: <uuid>
        # → spawned child task: <uuid>
    finally:
        client.shutdown()
```

## 使用官方预设 { #using-the-official-presets }

Noeta 提供四个与 Claude Code 阵容对齐的官方代理。`main` 代理可以委派给 `general-purpose`、`explore` 和 `plan`：

```python
from noeta import presets
from noeta.sdk import Client

# Build the main agent with all three sub-agents available.
options = presets.main_options()

client = Client(
    options,
    provider=my_provider,
    workspace_dir=Path("./my-project"),
    model="gpt-5.5",
)
```

完整四元组见[代理预设](../reference/presets.md)。

## 带委派的自定义代理 { #custom-agents-with-delegation }

```python
from noeta.sdk import Options, AgentDefinition

options = Options(
    system_prompt="You are a team lead. Delegate to specialists.",
    name="lead",
    agents={
        "coder": AgentDefinition(
            description="Writes and edits code.",
            prompt="You are a senior engineer. Write clean, tested code.",
            tools=["read", "edit", "write", "apply_patch", "shell_run"],
        ),
        "reviewer": AgentDefinition(
            description="Reviews code for bugs and style.",
            prompt="You are a code reviewer. Flag issues clearly.",
            tools=["read", "grep", "glob"],
        ),
    },
    permission_mode="bypassPermissions",
)
```

## 要点 { #key-points }

- **`AgentDefinition` 是扁平的、非递归的。** 子代理不能声明自己的子代理。深层树需要在顶层声明每个代理，并用 `Capabilities.spawnable` 连接委派路径。
- **`description` 是必需的。** 它在 `spawn_subagent` 工具 schema 中显示给父模型，以便模型知道选择哪个代理。
- **子代理在自己的 Task 中运行。** 它们获得自己的 EventLog、自己的 View 和自己的工具集。父代理仅在子代理达到终止状态后恢复。
- **结果自动 fold 回来。** 子代理的返回值作为 `SubtaskCompleted` 事件记录在父代理的 EventLog 中，父模型在下一个 View 中看到它。

## 来源 { #source }

- `examples/spawn_subtask.py` —— 完整可运行演示
- `Options.agents` / `AgentDefinition` —— `packages/noeta-sdk/noeta/client/options.py`
- `SPAWN_SUBAGENT_TOOL` —— `packages/noeta-runtime/noeta/policies/react.py`
- 官方预设：`packages/noeta-runtime/noeta/presets/__init__.py`
- 另见：[代理预设](../reference/presets.md)、[ADR：子任务扇出与持久唤醒](../adr/subtask-fanout-and-durable-wake.md)、[ADR：子任务并行执行](../adr/subtask-parallel-execution.md)
