# 预设代理

你不必从零设计一个 agent。`noeta.presets` 提供四个现成的：一个对话式的根 `main`，以及它委派的三个子代理。大多数宿主都从 `main` 出发再做调整。

它们是一个 **SDK 层**的接口——你通过构建某个 preset 的 `Options`（`presets.main_options()`）来选它，再把它交给 `Client` 或 `query`。自定义 agent 则走扁平的 `Options.agents` dict。

## 四元组

| 代理 | 角色 | 工具 | 激活 |
| --- | --- | --- | --- |
| `main` | 默认的编码代理：完整的内置工具面，派生那三个子代理。 | 全部内置工具集（不设 `allowed_tools`），外加它的 `memory` 激活所打开的记忆工具 | `fs`、`web`、`todo_write`、`ask_user_question`、`skill_invocation`、`memory`、`mcp`；`delegation` 由它的 `agents` 名册推导而来 |
| `general-purpose` | 自给自足的编码工人：完整的读 / 写 / 编辑 / shell 集合，不做委派。 | `apply_patch`、`edit`、`glob`、`grep`、`read`、`shell_kill`、`shell_poll`、`shell_run`、`web_search`、`webfetch`、`write` | `skill_invocation`、`mcp` |
| `explore` | 只读侦察兵：glob/grep/read 加只读 shell，扇出去汇报事实，从不编辑。 | `glob`、`grep`、`read`、`shell_kill`、`shell_poll`、`shell_run`、`webfetch` | `skill_invocation` |
| `plan` | 只读架构师：读代码，返回一份具体、有序的实施计划，从不写入。 | `glob`、`grep`、`read`、`shell_kill`、`shell_poll`、`shell_run`、`webfetch` | `ask_user_question` |

`explore` 和 `plan` 列出了 `shell_run`，但它们的 prompt 把它限制在只读命令上；高风险 shell 上的审批门是兜底。`general-purpose` 是一个叶子工人——它从不再往下派生，这就限住了扇出。

## 激活名

| 名字 | 它启用什么 |
| --- | --- |
| `todo_write` | `todo_write` control tool（基于 state-patch 的进度跟踪）。 |
| `ask_user_question` | 模型可以通过 `ask_user_question` control tool 让出以获取人类输入。 |
| `delegation` | `spawn_subagent` control tool。任何带 `agents` 名册的 agent 都会推导出它；显式写出它则是把派生权授予一个子 agent。 |
| `skill_invocation` | 用于模型驱动的 skill 选择的 `skill` control tool。 |
| `memory` | 跨任务记忆：`memory_write` / `memory_read` / `memory_search` / `memory_archive` 四个工具，外加用户消息接缝上的自动召回。 |
| `mcp` | MCP 工具继承：自身 spec 也打开了 `mcp` 的子任务，会继承父任务已启用的 MCP 服务器。 |
| `browser` | 由 sandbox 支撑的 `browser_*` 工具包。只有 `web` 专家会打开它。 |
| `fs` / `web` | `DEFAULT_PLUGINS`——默认的工具包。身份惰性。 |

只有 `main` 激活 `memory`：召回挂在用户消息摄入接缝上，而只有顶层的对话式 agent 才会收到用户消息。每个启用了记忆的 preset，其 prompt 都携带那段记忆策略片段（以 `MEMORY_POLICY_PROMPT` 导出），它告诉模型该存什么、不该存什么，以及写入卫生。

## 可选代理

除了这个四元组，还随包提供两个 `AgentDefinition`。两者都不在 `OFFICIAL_SUBAGENTS` 里，因此除非某个产品去注册它，它们都不会改变 `main` 的可派生名册。

| 定义 | 由谁注册 | 用途 |
| --- | --- | --- |
| `WEB_SUBAGENT`（`"web"`） | `sandbox_browser_options()` | 浏览专家——唯一激活 `browser` 的身份。注册它会同步把 `main` 的 prompt 换成 `MAIN_WEB_SYSTEM_PROMPT`，与名册保持步调一致，因此 prompt 绝不会提到一个不可派生的子代理。`main` 自己保持无浏览器，把每一次页面交互都委派出去。 |
| `CONSOLIDATION_AGENT`（`"__consolidation__"`） | `with_consolidation_agent(options)` | 后台的记忆策展员，由宿主的一个触发点作为普通根任务驱动。`tools=()` 清空白名单，因此它的整个接口面就是那个受能力门控的记忆工具包。它以 `__` 保留的名字使它不会进入任何父任务的可派生集合。 |

## 子代理扇出

`main` 可以并行派生这三个子代理；结果就是子代理的返回值，被记录进 EventLog，因此整棵树都能 fold 回状态。见 [ADR: Subtask fan-out and durable wake](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-fanout-and-durable-wake.md) 和 [ADR: Subtask parallel execution](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-parallel-execution.md)。

## 导出的接口

| 名字 | 形状 |
| --- | --- |
| `main_options()` | `Options` —— 官方的 `main` 配方 |
| `sandbox_browser_options()` | `Options` —— `main_options()` 加上 `web` 子代理和那份感知 web 的 prompt |
| `with_consolidation_agent(options)` | `Options` —— 注册了 `__consolidation__` 的 `options` |
| `official_specs()` | `dict[str, AgentSpec]` —— 编译好的四个 agent |
| `OFFICIAL_SUBAGENTS` | `dict[str, AgentDefinition]` —— `general-purpose` / `explore` / `plan` |
| `WEB_SUBAGENT` / `CONSOLIDATION_AGENT` | `AgentDefinition` |
| `CONSOLIDATION_AGENT_NAME` | `str` —— `"__consolidation__"` |
| `MAIN_SYSTEM_PROMPT` / `MAIN_WEB_SYSTEM_PROMPT` / `MEMORY_POLICY_PROMPT` | `str` |

prompt 文本住在 `noeta/presets/prompts/*.md` 里，并按字节忠实加载，因此改一段 prompt 就是一次文档形态的 diff。`main` 和 `main-web` 也被注册为具名 preset，因此 `SystemPromptPreset(preset="main")` 能解析出来。

## 以编程方式使用 preset

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
# → 'Replaced the three call sites in module X with Y and ran the tests.'
```

或者把四个 agent 全部编译成 spec：

```python
from noeta.presets import official_specs

specs = official_specs()
print(sorted(specs))
# → ['explore', 'general-purpose', 'main', 'plan']
print(specs["explore"].plugins)
# → ('skill_invocation',)
```

## 自定义代理

通过扁平的 `Options.agents` dict 定义自定义 agent：

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

## 源码

- Preset：`packages/noeta-sdk/noeta/presets/__init__.py`
- Prompt：`packages/noeta-sdk/noeta/presets/prompts/`
- `Options` / `AgentDefinition`：`packages/noeta-sdk/noeta/client/options.py`
- 工具目录：`packages/noeta-sdk/noeta/builtins/`
- [ADR: Tool and agent catalog](https://github.com/initxy/noeta/blob/main/docs/adr/tool-and-agent-catalog.md)

## 下一步

- [你的第一个 agent](../tutorials/first-agent.md) —— 从一个 preset 出发构建一个
- [生成子代理](../how-to/spawn-subagents.md) —— 在实践中使用这份名册
- [Options](sdk-options.md) —— 一个 preset 替你设置的每个字段
- [内置工具](tools.md) —— 每个 preset 的工具列表里都有什么
