# 内置工具

noeta 里模型能调用的每个工具：由哪个插件提供、有哪些参数、权限系统怎么对待它。工具名和参数格式照搬 Claude Code，因为模型对这些名字最熟。每个工具写给模型看的完整说明，就是插件代码里它的 `description`（大多在 `noeta/plugins/<插件>/descriptions/` 下）。

表格的读法：

- **参数**：必填的加粗。
- **加载**：*总是*表示每次请求都带上；*延迟*表示只列出名字，模型要先用 `ToolSearch` 把它的参数格式加载进来（见 [ToolSearch](#toolsearch)）。
- **只读**：同一次回复里的只读调用可以并排执行（最多十个）；其他调用一个一个按顺序跑。
- **权限**：在 `default` 模式下、没有任何规则时，[权限插件](../guides/permissions.md#一次调用怎样判定)怎么说；`acceptEdits`、`auto`、`plan` 的不同之处另行注明。拒绝和询问规则、hook 以及其他模式都在此之上起作用。"auto 下交分类器"指 `auto` 模式不直接放行，而是交给分类器判断。

## 一览

| 工具 | 插件 | 加载 | 只读 | 权限（`default` 模式） |
|---|---|---|---|---|
| [`Read`](#read) | `fs` | 总是 | 是 | 工作目录内放行；目录外要问 |
| [`Write`](#write) | `fs` | 总是 | | 要问；`acceptEdits` 和 `auto` 下工作目录内放行 |
| [`Edit`](#edit) | `fs` | 总是 | | 同 `Write` |
| [`NotebookEdit`](#notebookedit) | `fs` | 延迟 | | 同 `Write` |
| [`Bash`](#bash) | `shell` | 总是 | | 只读命令放行；其他要问 |
| [`Monitor`](#monitor) | `shell` | 延迟 | | 同 `Bash`；连 `ws` 的要问 |
| [`TaskStop`](#taskstop) | `shell` | 延迟 | | 放行 |
| [`EnterWorktree`](#enterworktree-exitworktree) | `shell` | 延迟 | | 放行 |
| [`ExitWorktree`](#enterworktree-exitworktree) | `shell` | 延迟 | | 放行 |
| [`Agent`](#agent) | `subtasks` | 总是 | | 放行；auto 下交分类器 |
| [`SendMessage`](#sendmessage) | `subtasks` | 延迟 | | 放行；auto 下交分类器 |
| [`ListAgents`](#listagents) | `subtasks` | 总是 | 是 | 放行 |
| [`SubagentHandback`](#subagenthandback) | `subtasks` | 仅子代理 | | 放行；auto 下交分类器 |
| [`Skill`](#skill) | `skills` | 总是 | | 放行 |
| [`AskUserQuestion`](#askuserquestion) | `ask_user` | 总是 | | 放行（它本身就是在问你） |
| [`WebFetch`](#webfetch) | `web` | 延迟 | 是 | 按主机问 |
| [`WebSearch`](#websearch) | `web` | 延迟 | 是 | 要问 |
| [`EnterPlanMode`](#enterplanmode) | `plan_mode` | 延迟 | 是 | 要问（强制）；`auto` 和 `plan` 下放行 |
| [`ExitPlanMode`](#exitplanmode) | `plan_mode` | 延迟 | | 放行（它本身会问你） |
| [`TaskCreate`、`TaskGet`、`TaskUpdate`、`TaskList`](#任务清单工具) | `todos` | 延迟 | `TaskGet`、`TaskList` | 放行 |
| [`ScheduleWakeup`](#schedulewakeup) | `schedule` | 总是 | | 放行 |
| [`CronCreate`、`CronList`、`CronDelete`](#定时任务工具) | `schedule` | 延迟 | `CronList` | 放行；`CronCreate` 在 auto 下交分类器 |
| [`Workflow`](#workflow) | `workflow` | 总是 | | 放行；auto 下交分类器 |
| [`ReportFindings`](#reportfindings) | `findings` | 总是 | 是 | 放行 |
| [`StructuredOutput`](#structuredoutput) | `structured_output` | 有答案格式时 | 是 | 放行 |
| [`ToolSearch`](#toolsearch) | `tool_search` | 有延迟工具时 | 是 | 放行 |
| [`ListMcpResourcesTool`、`ReadMcpResourceTool`、`ReadMcpResourceDirTool`](#mcp-工具) | `mcp` | 延迟 | 是 | 放行 |
| [`mcp__<server>__<tool>`](#mcp-工具) | `mcp` | 延迟 | | 要问 |
| [`run_skill_script`](#run-skill-script) | `skills` | 只在打开 `scripts` 时 | | 要问 |

`main` 代理带着上面所有插件；子代理带得少一些（见[代理文件](../guides/skills-and-agents.md#代理文件)）。关掉某个插件（`[plugins.web] enabled = false`）就拿掉了它的工具；`--tools`、`--disallowedTools`、代理文件的 `tools` / `disallowedTools`、权限插件的 `allowed_tools` / `disallowed_tools` 按名字筛选工具。拿掉 `Bash` 会顺带拿掉 `Monitor` 和 `TaskStop`。

noeta 不提供 `Glob`、`Grep`、`LS` 这几个工具：模型用 `Bash`（`rg`、`find`）来搜，只读命令不用问。`Read(...)` 权限规则仍然覆盖这几个名字。

## 文件：`fs` 插件

`noeta/plugins/fs/`。这个插件还会记住模型读过哪些文件：没读过的文件、或读过之后在磁盘上被改过（格式化工具、别的程序）的文件，`Edit` 和 `Write` 会拒绝，直到重新读一遍；读过的文件变了，会告诉模型一次。`Bash` 命令显示过的文件（`cat`、`head`、`sed -n`）也算读过。

### Read

读一个文件：文本（带行号）、图片、PDF 或 Jupyter notebook。崩溃后会重试，而不是回答"结果未知"。

| 参数 | 类型 | 含义 |
|---|---|---|
| **`file_path`** | string | 绝对路径 |
| `offset` | integer | 从第几行开始（第一行是 1） |
| `limit` | integer | 读几行 |
| `pages` | string | PDF 页码，如 `"1-5"`、`"3"`；每次最多 20 页 |
| `allow_large` | boolean | 突破通常的大小限制，读到上下文装得下为止 |

权限：工作目录内放行（计划文件、记忆目录和保存下来的工具输出也一样）；目录外要问，并提供 `Read(//<目录>/**)` 作为"以后不再问"的规则。

### Write

写整个文件，已存在就覆盖。

| 参数 | 类型 | 含义 |
|---|---|---|
| **`file_path`** | string | 绝对路径 |
| **`content`** | string | 完整内容 |

权限：`default` 下要问；`acceptEdits`、`auto`、`bypassPermissions` 下工作目录内放行；工作目录外，除了 `bypassPermissions` 以外的模式都要问（`auto` 下由分类器回答）；受保护路径（`.git/`、`.bashrc` 等）在任何模式下都要问；`plan` 下除了计划文件都要问。记忆目录里的 markdown 文件不用问就能写。写之前会先记下文件的快照（见[回退](../guides/permissions.md#改动快照和回退)）。

### Edit

把文件里一段确切的文字替换掉。

| 参数 | 类型 | 含义 |
|---|---|---|
| **`file_path`** | string | 绝对路径 |
| **`old_string`** | string | 要替换的文字 |
| **`new_string`** | string | 替换成什么（必须和原文不同） |
| `replace_all` | boolean | 替换所有出现的地方（默认 false） |

权限：同 `Write`。

### NotebookEdit

改 `.ipynb` 里的一个单元格：替换内容、插入新单元格或删除。

| 参数 | 类型 | 含义 |
|---|---|---|
| **`notebook_path`** | string | 绝对路径 |
| **`new_source`** | string | 单元格的新内容 |
| `cell_id` | string | 哪个单元格；`insert` 时新单元格插在它后面（不给就插在最前） |
| `cell_type` | string | `code` 或 `markdown`；`insert` 时必填 |
| `edit_mode` | string | `replace`（默认）、`insert` 或 `delete` |

权限：同 `Write`。

## shell：`shell` 插件

`noeta/plugins/shell/`。命令在任务的执行环境里、任务的工作目录下运行（见[命令在哪里执行](../guides/sandbox.md)）。

### Bash

执行一条 bash 命令，返回它的输出。shell 在两次调用之间保持当前目录。结果超过 30,000 个字符时存成文件，模型只看到开头一段。

| 参数 | 类型 | 含义 |
|---|---|---|
| **`command`** | string | 命令行 |
| `timeout` | number | 毫秒；默认 120,000（两分钟），前台运行最多 600,000 |
| `description` | string | 用大白话说明它做什么，给人看的 |
| `run_in_background` | boolean | 放到后台跑；模型马上拿到一个 id，跑完时会收到通知 |
| `dangerouslyDisableSandbox` | boolean | 申请在沙箱外运行 |

权限：每一段都是只读命令（`ls`、`cat`、`git log`、`rg` 等）、并且只读工作目录内东西的命令，放行；`acceptEdits` 下工作目录内的文件操作命令（`touch`、`mkdir`、`mv` 等）也放行；其他要问，并按命令的各段给出可记住的规则。`auto` 下由分类器回答。命令里提到 `.noeta` 的，除了 `bypassPermissions` 以外都要问；写受保护路径的在任何模式下都要问，`bypassPermissions` 也不例外；要离开沙箱的，不管允许规则怎么写都要问（`auto` 下由分类器判断，`bypassPermissions` 下直接跑）。打开沙箱且 `autoAllowBashIfSandboxed` 为默认的开时，在沙箱里跑的命令在 `plan` 以外的模式下放行。`Bash(...)` 规则逐段匹配（见[规则](../guides/permissions.md#规则)）。后台任务运行期间，所属任务会一直被跑它的那个工人认领着。

### Monitor

在后台运行一个脚本（或连一个 WebSocket），它每输出一行，就给模型发一条通知；模型这期间照常干活。

| 参数 | 类型 | 含义 |
|---|---|---|
| `command` | string | 脚本；stdout 每一行是一个事件，脚本退出就结束监视 |
| `ws` | object | 改为连接这个 WebSocket |
| **`description`** | string | 简短的标签，每条通知都带着 |
| **`timeout_ms`** | number | 多久后停止（默认 300,000，最多 1,800,000） |

权限：`command` 按 `Bash` 的规矩判（只读命令放行，其他要问；`Bash(...)` 规则覆盖它）；连 `ws` 的要问。

### TaskStop

按 id 停掉一个后台任务：`Bash` 后台命令、`Monitor`、子代理，或一次 `Workflow` 运行。

| 参数 | 类型 | 含义 |
|---|---|---|
| `task_id` | string | id |
| `shell_id` | string | `task_id` 的旧名字 |

权限：放行。

### EnterWorktree、ExitWorktree

`EnterWorktree` 新建一个 git worktree（在它自己的分支上），或进入已有的一个，并把任务的 shell 移进去；`ExitWorktree` 把 shell 移回原处。

| 工具 | 参数 | 类型 | 含义 |
|---|---|---|---|
| `EnterWorktree` | `name` | string | 新 worktree 的名字：字母、数字、`.`、`_`、`-`，可用 `/` 分段，最多 64 个字符（不给就随机起）；分支名是 `worktree-<name>` |
| | `path` | string | 改为进入本仓库里这个已有的 worktree（不能和 `name` 同时给） |
| `ExitWorktree` | **`action`** | string | `keep` 留在磁盘上；`remove` 连同分支一起删掉 |
| | `discard_changes` | boolean | 有未提交的文件或未合并的提交也照删 |

权限：放行，`auto` 下也不交分类器。Claude Code 进 `.claude/worktrees/` 以外的工作树前会问，因为它在那里会把会话的写权限一起挪过去；noeta 的 `EnterWorktree` 只挪 shell，不动权限范围（见 `parity/deviations.md`）。

## 子代理：`subtasks` 插件

`noeta/plugins/subtasks/`。子代理是在后台运行的子任务（见[子代理](../guides/subagents.md)）。

### Agent

启动一个子代理。`subagent_type: "fork"` 启动一个调用者的副本，继承整段对话；其他类型，或不写（`general-purpose`），从头开始。

| 参数 | 类型 | 含义 |
|---|---|---|
| **`description`** | string | 3–5 个词的标签 |
| **`prompt`** | string | 交给它的任务 |
| `subagent_type` | string | 哪种代理（对话里有列表） |
| `model` | string | `sonnet`、`opus`、`haiku` 或 `fable`，优先于它定义里的模型（fork 不理会） |
| `effort` | string | 推理强度：`low`、`medium`、`high`、`xhigh` 或 `max`（fork 不理会） |
| `isolation` | string | `worktree`：给它一个临时 git worktree；`remote`：在别处运行（有条件时） |

权限：放行；`auto` 下交分类器判断。`Agent(Explore)` 这样的规则可以点名类型。

### SendMessage

给它启动的子代理、给调用者（后台子代理写 `main`），或给 `ListAgents` 列出的其他任务发消息。

| 参数 | 类型 | 含义 |
|---|---|---|
| **`to`** | string | 代理 id 或名字、`main`，或任务 id |
| `message` | string | 纯文本；第一行应当能单独看懂 |
| `summary` | string | 在发送方对话记录里显示的 5–10 个词的标签 |
| `notify_when_idle` | boolean | 请另一个开着的任务在这一轮结束时回报一次 |

权限：放行；auto 下交分类器。

### ListAgents

列出 `SendMessage` 能联系到的代理：这个任务启动的子代理，以及存储里其他开着的任务，每个都标明类别和状态。只提供给顶层任务。参数 `channel` 和 `q` 是为了和 Claude Code 的格式一致，不起作用。权限：放行。

### SubagentHandback

只提供给在 `auto` 模式下启动的子代理：把最终报告交给调用者，结束这次运行。

| 参数 | 类型 | 含义 |
|---|---|---|
| **`message`** | string | 完整的报告 |

权限：放行；`auto` 下由分类器审一遍。`SubagentStop` hook 在它上面运行。

## 技能：`skills` 插件

### Skill

把一个技能的说明加载进对话（见[技能](../guides/skills-and-agents.md#技能)）。

| 参数 | 类型 | 含义 |
|---|---|---|
| **`skill`** | string | 清单里给出的名字，不带开头的斜杠 |
| `args` | string | 传给技能的文字（`$ARGUMENTS`） |

权限：放行；`Skill(name)` 规则可以点名某个技能。技能生效期间，它的 `allowed-tools` 列出的工具事先批准。

### run_skill_script

运行某个已生效技能目录里带的脚本，不经过 shell。只在技能插件的 `scripts` 参数打开时提供。

| 参数 | 类型 | 含义 |
|---|---|---|
| **`skill`** | string | 带这个脚本的已生效技能 |
| **`relpath`** | string | 脚本在该技能目录里的路径（如 `scripts/check.sh`） |
| `args` | array | 脚本参数，每个一个字符串，不能有 shell 特殊字符 |

权限：要问。

## 问人：`ask_user` 插件

### AskUserQuestion

问人一到四个选择题，等回答。等的时候任务是挂起的，不占工人。

| 参数 | 类型 | 含义 |
|---|---|---|
| **`questions`** | array | 1–4 个问题；每个有 **`question`**、**`header`**（最多 12 个字符）、**`options`**（2–4 个，各有 **`label`**、**`description`**，可选 `preview`）和 **`multiSelect`**；"其他"选项总会自动加上 |
| `answers`、`annotations` | object | 由前端填写 |
| `metadata` | object | 只做统计用 |

权限：放行。没人实时跟着的任务（`-p`、`interactive: false`）不提供，除非有客户端负责回答。

## 网络：`web` 插件

`noeta/plugins/web/`。这两个工具都从工人上联网；除了 MCP 服务器和你自己的命令，内置工具里只有它们会联网。

### WebFetch

抓一个网址，转成 Markdown，再用一个模型（`webfetch_model`，默认任务的模型）就 `prompt` 回答问题：模型拿到的是答案，不是整页内容。网页缓存 15 分钟；HTTP 自动升级为 HTTPS（本机回环地址除外）；跳转到别的主机时不跟过去，而是把跳转报告回来。

| 参数 | 类型 | 含义 |
|---|---|---|
| **`url`** | string | 网址 |
| **`prompt`** | string | 想从页面里知道什么 |
| `offset` | integer | 页面太长一次读不完时，从哪个字符位置接着读 |

权限：按主机问，并提供 `WebFetch(domain:<主机>)` 作为"以后不再问"的规则。

### WebSearch

搜索网页，返回标题、网址和摘要。只在有搜索密钥时提供（`NOETA_WEB_SEARCH_API_KEY`，或插件的 `search_api_key`；用的是 Tavily）。

| 参数 | 类型 | 含义 |
|---|---|---|
| **`query`** | string | 搜什么（至少 2 个字符） |
| `allowed_domains`、`blocked_domains` | array | 只要这些域名的结果，或排除这些域名 |
| `mode` | string | `standard`（快）或 `extended`（彻底，更贵；默认） |

权限：要问。

## 规划模式：`plan_mode` 插件

只提供给顶层任务（见[规划模式](../guides/permissions.md#规划模式)）。

### EnterPlanMode

进入规划模式，没有参数。权限：`default` 和 `acceptEdits` 下要问你（强制）；`auto` 和 `plan` 下放行。

### ExitPlanMode

把计划文件拿给你批准；你同意后，按你选的模式退出规划模式。`allowedPrompts` 参数接受但不使用。权限：放行；工具本身会挂起等你回答。

## 任务清单工具

`TaskCreate`、`TaskGet`、`TaskUpdate`、`TaskList`（`todos` 插件）把 Claude Code 的任务清单记在任务日志里。默认只提供给比 Claude Code 精简提示词更早的 Claude 模型（以及 Bedrock 的应用推理配置），而且只给顶层任务（`[plugins.todos] all_models = true` 让所有模型都有；见[任务清单](../guides/memory-and-instructions.md#任务清单)）。

| 工具 | 参数 |
|---|---|
| `TaskCreate` | **`subject`**、**`description`**、`activeForm`、`metadata` |
| `TaskGet` | **`taskId`** |
| `TaskUpdate` | **`taskId`**、`subject`、`description`、`activeForm`、`status`、`addBlocks`、`addBlockedBy`、`owner`、`metadata` |
| `TaskList` | 无 |

权限：放行。四个都会在崩溃后重试。

## 定时：`schedule` 插件

定时器是内核状态，所以排好的唤醒不怕哪个工人挂掉。

### ScheduleWakeup

设定自己掌握节奏的 `/loop` 下一次什么时候跑。

| 参数 | 类型 | 含义 |
|---|---|---|
| `delaySeconds` | number | 多少秒后，限制在 60–3,600 之间 |
| `reason` | string | 一句话说明原因，给人看 |
| `prompt` | string | 唤醒时触发的 `/loop` 输入 |
| `stop` | boolean | 改为结束循环 |
| `noop` | boolean | 这一次什么都没变 |

除非 `stop` 为 true，前三个和 `noop` 必填。结果里的唤醒时间按任务配置的 `timezone` 显示（没有就用机器的时区）。权限：放行。

### 定时任务工具

| 工具 | 参数 | 作用 |
|---|---|---|
| `CronCreate` | **`cron`**（5 个字段，按本地时间：任务配置里有 `timezone` 就用它，否则用跑这个任务的机器的时区）、**`prompt`**、`recurring`（默认 true；循环任务 7 天后失效）、`durable`（接受；所有任务本来就持久保存） | 按计划排一句提问，或只排一次 |
| `CronList` | 无 | 这个任务的定时任务 |
| `CronDelete` | **`id`** | 取消一个 |

结果里的下次运行时间也按这个时区显示。一个任务同时最多 50 个定时任务。权限：三个都放行；`auto` 下 `CronCreate` 交分类器，另外两个不交。

## 工作流：`workflow` 插件

### Workflow

运行一段 JavaScript 工作流脚本，用固定的方式驱动许多子代理（`agent()`、`parallel()`、`pipeline()`、`phase()`），在后台运行；跑完时会收到通知。

| 参数 | 类型 | 含义 |
|---|---|---|
| `script` | string | 整段脚本，以 `export const meta = {…}` 开头 |
| `name` | string | `.noeta/workflows/` 或 `~/.noeta/workflows/` 里保存的工作流 |
| `scriptPath` | string | 要运行的脚本文件（优先于 `script` 和 `name`） |
| `args` | any | 脚本里的全局变量 `args` |
| `resumeFromRunId` | string | 复用一次已结束运行里没变的 `agent()` 结果 |
| `description`、`title` | string | 忽略：它们写在脚本的 `meta` 里 |

权限：放行；auto 下交分类器。

## 审查结果：`findings` 插件

### ReportFindings

代码审查的说明要求时，把审查发现作为结构化列表交给前端显示。

| 参数 | 类型 | 含义 |
|---|---|---|
| **`findings`** | array | 核实过的发现，最严重的在前，最多 32 条；每条有 **`file`**、**`summary`**、**`failure_scenario`**，可选 `line`、`short_summary`、`category`、`verdict`、`outcome` |
| `level` | string | 这次审查用的推理强度 |

权限：放行。

## StructuredOutput

任务指定了答案格式时提供（`--json-schema`，或任务配置的 `output_schema`）：模型通过它交出最终答案，调用的参数就是答案。它的参数格式就是你给的那个 schema。不管代理的工具列表怎么写都会提供。权限：放行。

## ToolSearch

任务里有延迟加载的工具时，由 `tool_search` 插件加上。延迟工具在对话里只列名字；没加载就调用会失败，并提示怎么加载。`ToolSearch` 负责加载：

| 参数 | 类型 | 含义 |
|---|---|---|
| **`query`** | string | `select:WebFetch,NotebookEdit` 加载这几个；普通词语在名字和描述里搜；`+mcp resource` 只要名字里含 `mcp` 的 |
| **`max_results`** | number | 按词语搜索时最多加载几个（默认 5） |

加载过的工具在任务剩下的时间里一直可用，压缩后也是。权限：放行。

## MCP 工具

`mcp` 插件（见 [MCP 服务器](../guides/mcp.md)）提供以下工具，全部延迟加载：

| 工具 | 作用 | 权限 |
|---|---|---|
| `mcp__<server>__<tool>` | 已连接服务器的某个工具 | 要问；`mcp__<server>` 规则覆盖整个服务器 |
| `mcp__<server>__authenticate`、`mcp__<server>__complete_authentication` | 服务器需要登录时，代替它的工具出现 | 要问 |
| `ListMcpResourcesTool` | 列出服务器的资源（`server` 可缩小范围） | 放行 |
| `ReadMcpResourceTool` | 读一个资源（**`server`**、**`uri`**） | 放行 |
| `ReadMcpResourceDirTool` | 列出一个目录资源下一层的内容（**`server`**、**`uri`**） | 放行 |

三个资源工具只提供给顶层任务，只要配置了服务器就有（服务器挂了也有）。服务器的工具一律不算只读，不管服务器怎么标。服务器的工具排在内置工具后面。
