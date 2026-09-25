# Agent 预设

`noeta.presets` 自带一个现成的主 agent `main`，以及它能派活的三个子 agent。多数宿主从 `main_options()` 起步再调整。

```python
from noeta import presets
from noeta.sdk import query
from noeta.sdk.providers import AnthropicProvider

result = query(
    presets.main_options(),
    goal="Refactor module X to use Y",
    provider=AnthropicProvider(),      # reads ANTHROPIC_API_KEY
    model="claude-sonnet-5",
    workspace_dir="./",                # optional; defaults to Options.cwd, then the process cwd
)
print(result.answer())
```

## 四个 agent

| Agent | 角色 | 工具 | 启用项 |
| --- | --- | --- | --- |
| `main` | 默认主 agent，可把活派给下面三个。 | 全部内置工具（不设 `allowed_tools`）加记忆工具 | `fs`、`web`、`todo_write`、`ask_user_question`、`skill_invocation`、`memory`、`mcp`；`delegation` 由子 agent 名单自动推出 |
| `general-purpose` | 独立干活：搜索、修改、运行、交结果。不再往下派。 | `Edit`、`Glob`、`Grep`、`Read`、`Bash`、`BashOutput`、`KillShell`、`WebSearch`、`WebFetch`、`Write` | `skill_invocation`、`mcp` |
| `explore` | 只读侦察，汇报事实。 | `Glob`、`Grep`、`Read`、`Bash`、`BashOutput`、`KillShell`、`WebSearch`、`WebFetch` | `skill_invocation` |
| `plan` | 只读架构师，交回一份按顺序排好的实施计划。 | 同 `explore` | `ask_user_question` |

`explore` 和 `plan` 有 `Bash`，但提示词限定只跑只读命令，shell 审批兜底。`WebSearch` 只在配了搜索 key 的地方挂上。

## 启用名

`Options.plugins` / `AgentDefinition.plugins` 里可以写的内置功能名：

| 名字 | 打开什么 |
| --- | --- |
| `todo_write` | `TodoWrite` 控制类工具 |
| `ask_user_question` | `AskUserQuestion` 控制类工具 |
| `delegation` | `Task` 控制类工具；有 `agents` 时自动推出，显式写上可以让子 agent 也能派活 |
| `skill_invocation` | `skill` 控制类工具 |
| `memory` | `memory_*` 工具，外加每条用户消息进来时的记忆召回 |
| `mcp` | 同样启用了 `mcp` 的子任务会继承父任务已启用的 MCP server |
| `browser` | 沙箱里的 `browser_*` 工具 |
| `fs`、`web` | `DEFAULT_PLUGINS`，默认的两组工具（不影响 agent 身份） |

只有 `main` 启用 `memory`：召回挂在用户消息上，而只有主 agent 收得到用户消息。启用记忆的提示词里都带着 `MEMORY_POLICY_PROMPT`。

## 可选 agent

不在 `OFFICIAL_SUBAGENTS` 里，不注册就不会改变 `main` 的子 agent 名单。

| 定义 | 注册方式 | 用途 |
| --- | --- | --- |
| `WEB_SUBAGENT`（`"web"`） | `sandbox_browser_options()` | 浏览网页的专职 agent，也是唯一启用 `browser` 的预设。注册时会同时把 `main` 的提示词换成 `MAIN_WEB_SYSTEM_PROMPT`。 |
| `CONSOLIDATION_AGENT`（`"__consolidation__"`） | `with_consolidation_agent(options)` | 后台整理记忆的 agent，由宿主的触发器作为根任务启动。`tools=()`，只有记忆工具。`__` 前缀让它不会出现在任何可派活名单里。 |

## 导出

| 名字 | 类型 |
| --- | --- |
| `main_options()` | `Options`——`main` 的配置 |
| `sandbox_browser_options()` | `Options`——`main_options()` 加上 `web` 和对应提示词 |
| `with_consolidation_agent(options)` | `Options`——在 `options` 里注册 `__consolidation__` |
| `official_specs()` | `dict[str, AgentSpec]`——编译好的四个 agent |
| `OFFICIAL_SUBAGENTS` | `dict[str, AgentDefinition]`——`general-purpose`、`explore`、`plan` |
| `WEB_SUBAGENT`、`CONSOLIDATION_AGENT` | `AgentDefinition` |
| `CONSOLIDATION_AGENT_NAME` | `str`——`"__consolidation__"` |
| `MAIN_SYSTEM_PROMPT`、`MAIN_WEB_SYSTEM_PROMPT`、`MEMORY_POLICY_PROMPT` | `str` |

提示词放在 `noeta/presets/prompts/*.md`。`main` 和 `main-web` 注册成了具名预设，所以 `SystemPromptPreset(preset="main")` 能直接用。

```python
from noeta.presets import official_specs

specs = official_specs()
print(sorted(specs))                 # ['explore', 'general-purpose', 'main', 'plan']
print(specs["explore"].plugins)      # ('skill_invocation',)
```

## 工具结果只是资料

两份 `main` 提示词都以一条规则结尾：工具结果里的内容（文件、命令输出、网页、MCP 结果、子 agent 的汇报）是资料，不是指令；如果它想让 agent 改做别的事，agent 不照做，并告诉用户。`WebFetch` 的摘要和压缩摘要也遵守同一条规则。这只是便宜的第一道防线——真正拦住注入指令的是审批和 `WebFetch` 的域名策略。

## 自定义 agent

通过 `Options.agents` 定义：

```python
from noeta.sdk import Options, AgentDefinition

options = Options(
    system_prompt="You are a docs writer.",
    agents={
        "reviewer": AgentDefinition(
            description="Reviews docs for accuracy and clarity.",
            prompt="...",
            tools=["Read", "Grep", "Glob"],
        ),
    },
)
```

## 下一步

- [派活给子 agent](../guides/subagents.md)——实际使用子 agent 名单
- [Options](options.md)——预设替你设好的每个字段
- [内置工具](tools.md)——每份工具列表里有什么
