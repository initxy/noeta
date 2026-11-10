# 权限与 hooks

模型不问你就能做什么，怎样用模式、规则和 hook 改变它，规划模式怎样让任务在你批准计划之前保持只读，以及怎样用回退把文件改回去。格式都是 Claude Code 的，文件是 noeta 自己的（`~/.noeta/settings.json`、`.noeta/settings.json`、`.noeta/settings.local.json`）。

## 权限模式

每个任务都处在某个权限模式下，模式记在它的日志里。默认是 `auto`，和 Claude Code 一样。

| 模式 | 界面上的名字 | 哪些要问 |
|---|---|---|
| `default` | Default (ask before changes) | 改文件、非只读命令、抓网页、大多数其他工具 |
| `acceptEdits` | Accept edits | 同 `default`，但工作目录内的改文件不再问 |
| `plan` | Plan (explore, change nothing) | 任何会改动东西的操作（见[规划模式](#规划模式)） |
| `auto` | Auto (a classifier screens what would ask) | 本来要问的交给一个模型分类器回答；强制的问仍然到你这里 |
| `dontAsk` | Don't ask (refuse what would ask) | 不问：本来要问的直接拒绝 |
| `bypassPermissions` | Bypass permissions (ask for nothing) | 只剩强制的问和写受保护路径 |

起始模式可以用 `--permission-mode MODE`（`manual` 视同 `default`）、设置文件里的 `permissions.defaultMode`、代理文件的 `permissionMode`，或任务配置来设。任务运行中切换：终端界面里按 `Shift+Tab`，网页版或编辑器里用模式选择器，SDK 的 `set_permission_mode`，审批卡片上"Yes, and switch to …"那个选项，或者通过客户端接口发 `permission_mode` 信号。子代理一开始和调用者处在同一模式。

`bypassPermissions` 可以整个关掉：在设置文件的 `permissions` 里写 `"disableBypassPermissionsMode": "disable"`。`noeta serve` 和 `noeta worker` 启动时不加 `--allow-bypass-permissions`，不管请求怎么说都拒绝这个模式。

## 规则

规则就是 Claude Code 的 `Tool` 或 `Tool(内容)` 字符串，分三个列表，写在任意设置文件里：

```json
{
  "permissions": {
    "allow": ["Bash(npm run test *)", "Bash(git status)", "Edit(src/**)", "WebFetch(domain:docs.python.org)"],
    "ask":   ["Bash(git push *)"],
    "deny":  ["Read(./.env)", "Read(~/.ssh/**)", "Bash(rm -rf *)", "mcp__github__delete_repo"],
    "additionalDirectories": ["../shared-lib"],
    "defaultMode": "acceptEdits"
  }
}
```

设置文件按优先级从低到高：`~/.noeta/settings.json`；已信任项目的 `.noeta/settings.json` 和 `.noeta/settings.local.json`；机器统一管理的 `managed-settings.json`（`/etc/noeta`，macOS 上是 `/Library/Application Support/Noeta`）。规则列表和 `additionalDirectories` 在各文件之间累加；`defaultMode` 以最后一个文件为准。无界面运行时 `--allowedTools` 和 `--disallowedTools` 也加进列表（见 [-p 的参数](./terminal.md#p-只问一句-不开界面)）。Claude Code 自己的 `~/.claude/settings.json` 从来不读。

语法（`noeta/toolkit/permission_rules.py`）：

| 规则 | 匹配 |
|---|---|
| `Bash` | 这个工具的每次调用（任何工具都可以只写名字） |
| `Bash(npm run test)` | 恰好这条命令 |
| `Bash(npm run *)`、`Bash(npm run:*)` | 按前缀匹配；`*` 在任何位置都匹配任意文字，末尾的 ` *` 也匹配不带参数的前缀本身 |
| `Read(path)`、`Edit(path)` | gitignore 风格的路径：`//abs` 是绝对路径，`~/p` 在主目录下，`/p` 和 `p` / `./p` 相对项目根目录（不含 `/` 的模式在任何深度都匹配）；`*` 不跨目录，`**` 跨目录 |
| `WebFetch(domain:example.com)` | 这个主机；`*.example.com` 是它的子域名 |
| `mcp__server`、`mcp__server__*` | 某个 MCP 服务器的所有工具；`mcp__server__tool` 是其中一个 |
| `Agent(Explore)` | 启动这种子代理 |
| `Skill(name)` | 这个技能 |

`Edit(...)` 规则覆盖所有写文件的工具（`Edit`、`Write`、`NotebookEdit`），`Read(...)` 规则覆盖所有读文件的工具。复合命令（`a && b | c`）逐段判断：拒绝或询问规则只要命中任何一段就生效；放行规则必须覆盖每一段，而且永远不覆盖命令替换和重定向到文件（`npm test $(id)`）。只是"包一层再执行"的程序（`timeout`、`time`、`nice`、`nohup`、`stdbuf`）会被看穿。

拒绝规则压过一切，其次是询问规则（任何模式都跳不过，`bypassPermissions` 也一样），再次是放行规则。针对路径的拒绝规则也管 shell：一条 `Bash` 命令要是写了 `Edit` 拒绝规则覆盖的路径、读了 `Read` 拒绝规则覆盖的路径，或删掉了包含这种路径的目录，在任何模式下都拒绝。

你在卡片上选"Yes, and don't ask again for …"时，卡片上那条规则会记在这个任务里，它启动的子代理也继承；在已信任的项目里还会追加到 `.noeta/settings.local.json`（这个文件会通过 `.git/info/exclude` 排除在 git 之外）。

## 一次调用怎样判定

任何工具调用执行之前，代理的每个插件都会被问一遍（`before_tool`），然后由内核把答案合起来（`noeta/core/runner/calls.py`）。权限插件只是其中之一；hook、技能的 `allowed-tools`、规划模式也都是。

<Diagram name="guide-gates" caption="所有插件对一次调用的判断合在一起：有拒绝就拒绝，改写过的重新判断，要问的就挂起，除非有人先回答" />

1. **任何一个拒绝都算数。** 插件在检查时自己出错也算拒绝（`permission check failed: …`），所以检查坏掉绝不会把调用放过去。
2. **改写**（hook 的 `updatedInput`、客户端改过参数的批准）替换掉原来的调用，从头重新判断，最多三次。
3. **要问。** 没人要问，调用就执行。一个表示批准的放行（PreToolUse hook 的 `allow`）可以免掉非强制的问；强制的问（询问规则、hook 的 `ask`）一定要有人回答。
4. **在到你之前先回答**（`resolve_ask`）：PermissionRequest hook、auto 模式的分类器、无界面客户端的 `can_use_tool`，或者一个 MCP 权限提示工具。
5. **否则就挂起。** 任务记下 `Waiting`，让出工人，给你看卡片。你的回答进入它的收件箱，调用继续往下走，接手的是哪个工人都行。

一次回复里的多个调用按顺序判断，遇到第一个要问的就停：后面的调用先不判断，等它被回答。

权限插件内部按 Claude Code 的顺序判断（`noeta/plugins/permissions/rule_check.py`）：

<Diagram name="guide-permission-decision" caption="权限插件按顺序检查什么，以及模式怎样处理一个要问的调用" />

- 被拿掉的工具（`--tools`、`--disallowedTools` 里的工具名、代理文件的 `disallowedTools`）直接拒绝，而且压根不提供给模型。
- 先看拒绝规则，再做 shell 的路径检查，然后看询问规则。
- 再看这次调用碰了什么：读工作目录内的文件（以及计划文件、记忆目录、保存下来的工具输出）放行；在 `default` 下改工作目录内的文件要问；改工作目录外的文件，除了 `bypassPermissions` 以外的模式都要问；写受保护路径在 `acceptEdits` 和 `bypassPermissions` 下也要问（`auto` 下交给分类器判断）。shell 命令如果每一段都是只读的（`ls`、`git log`、`grep` 等）、并且只读工作目录内的东西，放行；其他的要问，除非放行规则覆盖了每一段。`WebFetch` 按主机问。只读或只跟你说话的工具（`AskUserQuestion`、`Skill`、`ToolSearch`、任务清单工具等）放行，定时任务工具和 `EnterWorktree` / `ExitWorktree` 也放行（见[工具](../reference/tools.md#一览)）。
- 最后看模式：`dontAsk` 把要问变成拒绝，`bypassPermissions` 变成放行（强制的问和受保护路径除外），`auto` 交给分类器，`plan` 照问不误。

受保护的路径（`noeta/toolkit/protected_paths.py`）包括这些目录：`.git`、`.vscode`、`.idea`、`.claude`、`.husky`、`.cargo`、`.devcontainer`、`.yarn`、`.mvn`、`.noeta`（但不含其中的 `skills`、`commands`、`worktrees`）和 `.config/git`；以及配置 shell、git、包管理器或 hook 工具的文件（`.bashrc`、`.zshrc`、`.gitconfig`、`.npmrc`、`.envrc`、`.mcp.json`、`.pre-commit-config.yaml` 等）。

### auto 模式

在 `auto` 模式下，另一个模型（分类器）会看每个本来要问的调用，结合你的项目说明文件（仅限已信任的项目）和对话内容，决定放行还是拒绝。拒绝时模型会知道原因，这一轮接着走；强制的问还是到你这里。分类器用的模型由 `classifier_model` 指定（见[插件在旁边用的模型](./models.md#插件在旁边用的模型)）。给它的指引是 Claude Code 的 `autoMode` 对象，作为权限插件的 `auto_mode` 参数：

```toml
[plugins.permissions.auto_mode]
allow = ["$defaults", "Running the project's test suite"]
soft_deny = ["$defaults", "Pushing to any remote"]
```

每个列表替换对应那一节的内置文字；写一个 `"$defaults"` 就在那个位置放回内置文字。会让一整类调用绕过分类器的放行规则（比如光秃秃一个 `Bash`）在分类器判断期间不起作用。

## Hooks

hook 就是 Claude Code 的 hook：你配置的命令、HTTP 地址、模型检查或 MCP 工具，在任务的固定节点上运行。它们可以给模型补充上下文，改变或拦下将要发生的事，或替你回答权限请求。和规则写在同样的设置文件里，放在 `hooks` 下，也可以来自 Claude Code 插件的 `hooks/hooks.json`：

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "~/.noeta/hooks/check-bash.sh", "timeout": 30}]},
      {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "ruff format --quiet \"$NOETA_PROJECT_DIR\"", "if": "Edit(**/*.py)"}]}
    ],
    "SessionStart": [
      {"matcher": "startup", "hooks": [{"type": "command", "command": "git log --oneline -5"}]}
    ]
  }
}
```

终端界面里的 `/hooks` 列出当前配置了哪些。

### 事件

| 事件 | 匹配对象 | hook 能做什么 |
|---|---|---|
| `PreToolUse` | 工具名 | `permissionDecision`：`deny`、`ask` 或 `allow`；`updatedInput` 改写调用（改写后重新判断） |
| `PermissionRequest` | 工具名 | 调用要等你时，替你放行（可顺带改写）或拒绝 |
| `PermissionDenied` | 工具名 | 分类器拒绝了某个调用之后；`retry` 告诉模型可以再试 |
| `PostToolUse`、`PostToolUseFailure` | 工具名 | 在结果后面补充上下文、拦下，或替换结果（`updatedToolOutput`） |
| `PostToolBatch` | — | 一次回复的所有调用都有结果后：补充上下文，或结束这一轮 |
| `UserPromptSubmit` | — | 补充上下文，或拦下：这一轮失败，你的消息被抹掉 |
| `Stop`、`SubagentStop` | — / 代理类型 | 拦下就让模型继续，理由作为它的下一条消息 |
| `SessionStart` | `startup`、`resume`、`compact` | 给模型补充上下文 |
| `SubagentStart` | 代理类型 | 给子代理补充上下文 |
| `PreCompact`、`PostCompact` | `manual`、`auto` | 补充摘要要求，或跳过这次压缩；压缩后的输出给你看 |
| `PostModelSwitch` | 新模型 | 前端切换模型后补充上下文 |
| `Notification` | 通知类型 | 只做附带动作（等审批或等回答时） |
| `SessionEnd`、`StopFailure`、`CwdChanged`、`InstructionsLoaded` | 原因、错误、—、加载原因 | 只做附带动作 |

Claude Code 的其他事件（`Setup`、`PreModelSwitch`、`FileChanged`、`Elicitation` 等）会跳过并给出警告；它们各自属于 noeta 没有的某个 Claude Code 功能。

### matcher 和 `if`

matcher 拿事件的匹配对象（上表中间一列）来比。单纯的名字列表（`Edit|Write`、`Edit, Write`）逐个名字比较；其他写法都当正则表达式，在匹配对象里搜索；空或 `*` 匹配一切。hook 的 `if` 是一条权限规则（`Bash(git *)`、`Edit(**/*.py)`），工具调用也必须匹配它；没有工具调用的事件上，带 `if` 的 hook 永远不跑。一模一样的 hook 每个事件只跑一次（`noeta/plugins/hooks/config.py`）。

### hook 类型

| `type` | 怎么跑 | 字段 |
|---|---|---|
| `command` | 在工人上用 `sh -c` 执行，工作目录是任务目录，事件以 JSON 从 stdin 传入 | `command`（或 `args`，不经 shell）、`timeout`（默认 600 秒）、`async`、`asyncRewake`、`onFailure`、`once` |
| `http` | 把事件以 JSON POST 过去；响应体当作 stdout 读 | `url`、`headers`（`$VAR` 只替换 `allowedEnvVars` 里列出的名字） |
| `prompt` | 任务的模型（或 `model`）判断条件是否成立；`$ARGUMENTS` 是事件 JSON | `prompt`、`model`，默认超时 30 秒 |
| `agent` | 同 `prompt`，但可以先用只读工具查一查（最多 50 次调用） | `prompt`、`model`，默认超时 60 秒 |
| `mcp_tool` | 调用任务已连接的某个 MCP 服务器的工具 | `server`、`tool`、`input`（`${tool_input.file_path}` 这类从事件里填） |

command hook 的环境变量是 noeta 的（去掉它自己的凭据），再加 `NOETA_PROJECT_DIR`（以及给 Claude Code 写的脚本用的 `CLAUDE_PROJECT_DIR`）。`async` 的 hook 在后台跑，它补充的内容随模型的下一次调用送到；`asyncRewake` 的 hook 以退出码 2 结束时，会带着它的输出唤醒模型。两者都经由任务的收件箱回来，所以在哪个工人上都能用。

### hook 能返回什么

- **退出码 0**：成功。stdout 是纯文本（SessionStart 和 UserPromptSubmit 时作为上下文），或一个 JSON 对象：`continue` / `stopReason`、`decision` / `reason`、`systemMessage`、`hookSpecificOutput`（`permissionDecision`、`permissionDecisionReason`、`updatedInput`、`additionalContext` 等）。
- **退出码 2**：拦下，stderr 作为理由。
- **其他**：报告一下然后忽略，除非写了 `"onFailure": "block"`。

超过 10,000 字符的文字会另存成文件（放在任务保存长输出的目录里），只发一段预览。每个事件的输入都带 Claude Code 的字段（`session_id`，即根任务 id；`cwd`；`permission_mode`；`hook_event_name`；`transcript_path`，为空），外加 noeta 的 `task_id`，在子代理里还有 `agent_id` / `agent_type`。

### 拒绝优先

多个 PreToolUse hook 之间，第一个 `deny`（或拦下）说了算；否则任何一个 hook 的 `ask` 压过 `allow`。插件之间按内核的规则：hook 的拒绝压过权限插件的放行；hook 的放行只能在没有强制的问时免掉询问，所以拒绝规则和询问规则仍然压过它。注册了 `callback` hook 的客户端如果连不上，工人会拒绝那些本该由它判断的调用。

## 规划模式

在规划模式下，模型只做调查，把计划写进一个文件，其他什么都不改，直到你批准这份计划（`noeta/plugins/plan_mode/`）。

- 进入方式：`/plan [需求]`，用 `Shift+Tab` 切到 `plan`，或者模型调用 `EnterPlanMode`（会先问你，`auto` 模式下不问）。
- 规划期间，只有读文件、只读命令和写计划文件不用问（计划文件在 `~/.noeta/plans/`）。在 `auto` 模式下开始的规划，shell 命令仍交给分类器判断（Claude Code 的 `useAutoModeDuringPlan`，默认开；在设置文件里写 `false` 关掉）；改文件总是问你。
- 计划写好后模型调用 `ExitPlanMode`。你看到计划，然后选择："Yes, and use auto mode"（或 "Yes, auto-accept edits"）、"Yes, manually approve edits"，或者 "No, keep planning" 并说明要改什么。
- 正在规划的任务启动的子代理也处于规划状态；它们没有 `ExitPlanMode`，在最后一条消息里给出结论。

即使没有权限插件，规划模式自己也会让任务保持只读：任何不是只读的工具调用都会问。

## 改动快照和回退

`Edit`、`Write` 或 `NotebookEdit` 改文件之前，`checkpoints` 插件会把文件当前内容记下来，每句提问里每个文件记一次（`noeta/plugins/checkpoints.py`）。内容按哈希存，同一个任务里相同内容只存一份。`Bash` 命令做的改动不跟踪，和 Claude Code 一样。

`/rewind`（或在空输入框上连按两次 `Esc`）列出你说过的每句提问，以及从那以后改过的文件，然后按你的选择恢复：

| 选项 | 效果 |
|---|---|
| Restore code and conversation | 下面两项都做 |
| Restore conversation | 显示到那句提问之前为止的对话，那句提问放回输入框，你可以改了再发；接着这段对话的新任务在你发出时才建 |
| Restore code | 文件恢复到那句提问之前的内容；当时不存在的文件会被删掉 |

对话从不原地截断：旧任务留在存储里，新任务从它的历史开始（`history_from`），所以回退不会丢任何东西。文件通过任务自己的执行环境写回，不管任务在哪儿跑；只写日志为那个路径记录过的内容，其他的都报告为未恢复（`noeta/host/rewind.py`）。所有前端用的是同一套辅助函数：终端界面和 ACP 里的 `/rewind`、网页版的回退对话框、无界面协议里的 `rewind_files`，以及 HTTP 上的 `GET` / `POST /tasks/{id}/rewind`。
