# 代理预设

`noeta.presets` 提供四个官方代理：一个对话式的根 `main`，以及它委派的三个子代理。

它们是一个 **SDK 层**的接口：你通过构建某个预设的 `Options`（`presets.main_options()`）来选它，再把它交给 `Client` / `query`。自定义代理通过扁平的 `Options.agents` dict 定义。

## 四元组

| 代理 | 角色 | 工具 | 激活 |
| --- | --- | --- | --- |
| `main` | 默认编码代理：完整的内置工具面，生成三个子代理。 | 完整内置集（`allowed_tools` 未设置），加上其 `memory` 激活开启的记忆工具 | `fs`、`web`、`todo_write`、`ask_user_question`、`skill_invocation`、`memory`、`mcp`；`delegation` 由它的 `agents` 阵容推导得出 |
| `general-purpose` | 自包含的编码 Worker：完整的读/写/编辑/shell 集，无委派。 | `apply_patch`、`edit`、`glob`、`grep`、`read`、`shell_kill`、`shell_poll`、`shell_run`、`web_search`、`webfetch`、`write` | `skill_invocation`、`mcp` |
| `explore` | 只读侦察：glob/grep/read + 只读 shell，扇出以报告事实，从不编辑。 | `glob`、`grep`、`read`、`shell_kill`、`shell_poll`、`shell_run`、`webfetch` | `skill_invocation` |
| `plan` | 只读架构师：读取代码并返回一份具体、有序的实现计划，从不写入。 | `glob`、`grep`、`read`、`shell_kill`、`shell_poll`、`shell_run`、`webfetch` | `ask_user_question` |

`explore` 和 `plan` 列出了 `shell_run`，但它们的提示将其限制在只读命令上；高风险 shell 上的批准门是最后的兜底。`general-purpose` 是一个叶子 Worker——它从不进一步生成，从而限住扇出。

## 激活名

| 名字 | 启用的功能 |
| --- | --- |
| `todo_write` | `todo_write` 控制工具（基于 state-patch 的进度跟踪）。 |
| `ask_user_question` | 模型可以通过 `ask_user_question` 控制工具为人类输入让路。 |
| `delegation` | `spawn_subagent` 控制工具。为任何拥有 `agents` 阵容的代理推导得出；显式命名它可以授予子代理生成的权限。 |
| `skill_invocation` | 用于模型驱动 skill 选择的 `skill` 控制工具。 |
| `memory` | 跨任务记忆：`memory_write` / `memory_read` / `memory_search` / `memory_archive` 工具，加上在用户消息接缝处的自动召回。 |
| `mcp` | MCP 工具继承：自身规范也开启 `mcp` 的子任务，继承父级已启用的 MCP servers。 |
| `browser` | 沙箱支撑的 `browser_*` 工具包。只有 `web` 专家开启它。 |
| `fs` / `web` | `DEFAULT_PLUGINS` —— 默认工具包。身份中性。 |

只有 `main` 激活 `memory`：召回挂到用户消息摄入接缝上，而只有顶层的对话式代理才接收用户消息。每个启用了记忆的预设，其提示都携带记忆政策片段（导出为 `MEMORY_POLICY_PROMPT`），它告诉模型什么值得记、什么不记，以及写入卫生规则。

## 可选代理

除了四元组，还随附另外两个 `AgentDefinition`。二者都不在 `OFFICIAL_SUBAGENTS` 里，因此除非某个产品注册它，否则都不会改变 `main` 的可生成阵容。

| 定义 | 由谁注册 | 用途 |
| --- | --- | --- |
| `WEB_SUBAGENT`（`"web"`） | `sandbox_browser_options()` | 浏览专家——唯一激活 `browser` 的身份。注册它会与阵容同步地把 `main` 的提示切换到 `MAIN_WEB_SYSTEM_PROMPT`，于是提示绝不会点名一个不可生成的子代理。`main` 本身保持无浏览器，并把每一次页面交互都委派出去。 |
| `CONSOLIDATION_AGENT`（`"__consolidation__"`） | `with_consolidation_agent(options)` | 后台记忆策展器，作为一个普通的根任务由主机触发器驱动。`tools=()` 清空白名单，于是它的整个工作面就是能力门控的记忆包。它的 `__` 保留名让它排除在任何父级的可生成并集之外。 |

## 子代理扇出

`main` 可以并行生成这三个子代理；结果是子代理的返回值，记录到 EventLog 中，以便整棵树 fold 回状态。见 [ADR：子任务扇出与持久唤醒](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-fanout-and-durable-wake.md)和 [ADR：子任务并行执行](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-parallel-execution.md)。

## 导出的接口

| 名字 | 形态 |
| --- | --- |
| `main_options()` | `Options` —— 官方的 `main` 配方 |
| `sandbox_browser_options()` | `Options` —— `main_options()` 加上 `web` 子代理和 web 感知提示 |
| `with_consolidation_agent(options)` | `Options` —— 注册了 `__consolidation__` 的 `options` |
| `official_specs()` | `dict[str, AgentSpec]` —— 四个代理，已编译 |
| `OFFICIAL_SUBAGENTS` | `dict[str, AgentDefinition]` —— `general-purpose` / `explore` / `plan` |
| `WEB_SUBAGENT` / `CONSOLIDATION_AGENT` | `AgentDefinition` |
| `CONSOLIDATION_AGENT_NAME` | `str` —— `"__consolidation__"` |
| `MAIN_SYSTEM_PROMPT` / `MAIN_WEB_SYSTEM_PROMPT` / `MEMORY_POLICY_PROMPT` | `str` |

提示文本存放在 `noeta/presets/prompts/*.md`，逐字节忠实地加载，因此编辑一个提示就是一处 docs 形态的 diff。`main` 和 `main-web` 也注册为命名预设，因此 `SystemPromptPreset(preset="main")` 可解析。

## 以编程方式使用预设

```python
from noeta import presets
from noeta.sdk import query
from noeta.sdk.providers import AnthropicProvider

options = presets.main_options()

# `provider` and `workspace_dir` are required — without them the Client
# raises ValueError before any turn.
result = query(
    options,
    goal="Refactor module X to use Y",
    provider=AnthropicProvider(api_key="sk-ant-…"),
    workspace_dir="./",
    model="claude-sonnet-4-5-20250929",
)
print(result.answer())
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

- 预设：`packages/noeta-sdk/noeta/presets/__init__.py`
- 提示：`packages/noeta-sdk/noeta/presets/prompts/`
- Options / AgentDefinition：`packages/noeta-sdk/noeta/client/options.py`
- 工具目录：`packages/noeta-sdk/noeta/builtins/`
- 另见：[ADR：工具与代理目录](https://github.com/initxy/noeta/blob/main/docs/adr/tool-and-agent-catalog.md)、[ADR：库-SDK 架构](https://github.com/initxy/noeta/blob/main/docs/adr/library-sdk-architecture.md)
