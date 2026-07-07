# 代理预设

Noeta 提供四个官方代理，与 Claude Code 的阵容对齐。代理在**每个任务**的 `POST /tasks` body 中选择（`{"goal": …, "agent": …}`），而非在进程启动时。自定义代理通过扁平的 `Options.agents` dict 定义。

## 四元组

| 代理 | 角色 | 工具 | 能力 |
| --- | --- | --- | --- |
| `main` | 默认编码代理：完整内置工具集 + 可生成三个子代理 + 所有能力。 | 完整内置集（所有 fs + web + app + memory 工具） | `todo_write`、`ask_user_question`、`delegation`、`skill_invocation`、`memory`、`mcp` |
| `general-purpose` | 自包含编码 Worker：完整读/写/编辑/shell 集，无委派。 | `apply_patch`、`edit`、`glob`、`grep`、`read`、`shell_kill`、`shell_poll`、`shell_run`、`web_search`、`webfetch`、`write` | `skill_invocation`、`mcp` |
| `explore` | 只读侦察：glob/grep/read + 只读 shell，扇出以报告事实，从不编辑。 | `glob`、`grep`、`read`、`shell_kill`、`shell_poll`、`shell_run`、`webfetch` | `skill_invocation` |
| `plan` | 只读架构师：读取代码并返回具体的有序实现计划，从不写入。 | `glob`、`grep`、`read`、`shell_kill`、`shell_poll`、`shell_run`、`webfetch` | `ask_user_question` |

## 能力标志

| 标志 | 启用的功能 |
| --- | --- |
| `todo_write` | `todo_write` 控制工具（基于 state-patch 的进度跟踪）。 |
| `ask_user_question` | 模型可以通过 `ask_user_question` 控制工具为人类输入让路。 |
| `delegation` | 可以通过三个子代理（`general-purpose`、`explore`、`plan`）生成子任务。 |
| `skill_invocation` | 用于模型驱动技能选择的 `skill` 控制工具。 |
| `memory` | 跨任务内存：`memory_write` / `memory_read` 工具 + 在用户消息接缝处自动召回。只有 `main` 启用此功能。 |
| `mcp` | MCP 工具继承：自身规范也开启 `mcp` 的子任务继承父级的已启用 MCP servers。 |

## 子代理扇出

`main` 可以并行生成三个子代理；结果是子代理的返回值，记录到 EventLog 中，以便整棵树 fold 回状态。见[ADR：子任务扇出与持久唤醒](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-fanout-and-durable-wake.md)和[ADR：子任务并行执行](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-parallel-execution.md)。

## 以编程方式使用预设

```python
from noeta import presets
from noeta.sdk import Client, query

# 构建 main 代理的 Options
options = presets.main_options()

# 在进程内运行代理
result = query(options, goal="Refactor module X to use Y")
```

或将所有四个代理编译为 specs：

```python
from noeta.presets import official_specs
specs = official_specs()
# → {"main": AgentSpec, "general-purpose": AgentSpec, "explore": AgentSpec, "plan": AgentSpec}
```

## 自定义代理

通过扁平的 `Options.agents` dict 定义自定义代理：

```python
from noeta.sdk import Options, AgentDefinition

options = Options(
    system_prompt="You are a docs writer.",
    agents={
        "reviewer": AgentDefinition(
            description="Reviews docs for accuracy and clarity.",
            prompt="...",
            tools=["read", "grep", "glob"],
        ),
    },
)
```

## 来源

- 预设：`packages/noeta-runtime/noeta/presets/__init__.py`
- Options / AgentDefinition：`packages/noeta-sdk/noeta/client/options.py`
- 工具目录：`packages/noeta-runtime/noeta/tools/`
- 另见：[ADR：工具与代理目录](https://github.com/initxy/noeta/blob/main/docs/adr/tool-and-agent-catalog.md)、[ADR：库-SDK 架构](https://github.com/initxy/noeta/blob/main/docs/adr/library-sdk-architecture.md)
