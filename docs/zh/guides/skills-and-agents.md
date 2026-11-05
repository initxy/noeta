# 技能、代理与命令

有三种 markdown 文件，不写一行代码就能改变 noeta 的行为：技能（模型在需要时才加载的做事说明）、代理文件（一个代理的系统提示词、工具和插件），以及斜杠命令。三种都用 Claude Code 的格式，只是放在 noeta 自己的位置。Claude Code 的插件目录可以把三样一起带进来。

## 技能

一个技能就是一个装着 `SKILL.md` 的目录：先是 YAML frontmatter，后面是说明。模型在技能清单里看到每个技能的名字和描述，需要时用 `Skill` 工具加载整个文件；你也可以直接输入 `/<名字> [参数]`。

```markdown
---
description: Cut a release: bump the version, update the changelog, tag. Use when asked to release or ship a version.
arguments: [version]
allowed-tools: Bash(git *), Edit
---
Release version $version of this project.

The last tag: !`git describe --tags --abbrev=0`

1. …
```

存成 `~/.noeta/skills/release/SKILL.md`，这个技能就叫 `release`：技能以所在目录命名，frontmatter 里的 `name` 只是显示用的名字（`noeta/plugins/skills/registry.py`）。

### 去哪里找技能

<Diagram name="guide-skills-lookup" caption="技能插件查找的位置，优先级从高到低；同名的技能先找到的算数" />

| 位置 | 什么时候读 |
|---|---|
| 机器统一管理的目录里的 `.noeta/skills`（`/etc/noeta`，macOS 上是 `/Library/Application Support/Noeta`） | 总是 |
| `~/.noeta/skills/<名字>/SKILL.md` | 总是 |
| `~/.agents/skills/<名字>/SKILL.md`，各家编程助手共用的技能目录（在你的主目录下，`$NOETA_HOME` 改不了它的位置） | 总是 |
| 工作目录及其上层各级目录（直到仓库根目录）里的 `.noeta/skills/` 和 `.agents/skills/`（近的优先；同一目录里 `.noeta` 先于 `.agents`） | 已信任的项目 |
| 每个额外目录（`--add-dir`、`additionalDirectories`）里的同样位置 | 已信任的项目 |
| 统一管理目录的 `.noeta/commands/`、`~/.noeta/commands/`，再是项目和额外目录里的 `.noeta/commands/`（已信任的项目） | 其中的 `*.md` 当作技能读（见下文） |
| Claude Code 插件的 `skills/` | 命名为 `<插件>:<技能>` |
| 工作目录下层的 `.noeta/skills` 和 `.agents/skills`，在文件工具碰到那里的文件后才发现 | 已信任的项目；不会顶掉同名技能：名字被占了的，命名为 `<目录>:<技能>`（如 `apps/web:deploy`） |

同一个名字，先找到的技能算数，和 Claude Code 一样；查找顺序是确定的，同样的目录树总得出同样的清单。比技能目录深一层以上的 `SKILL.md` 不算技能。Claude Code 的 `.claude/skills` 从来不读。没被信任的项目（见 [`noeta trust`](./terminal.md#安装和启动)）会跳过它的技能目录，并提示一句。每次有新输入时会重新查找技能，模型每次调用前如果某个 `SKILL.md` 变了也会重新查找，所以任务中途新加的技能能马上被发现。

### frontmatter

| 键 | 作用 |
|---|---|
| `description` | 清单里显示的描述；没写就取正文第一行（最多 100 个字符） |
| `when_to_use` | 在清单里接在描述后面，用短横线隔开 |
| `name` | 显示名，列在技能名旁边 |
| `arguments` | 给 `/<名字>` 后面的参数起名：`$version` 就是第一个，依此类推 |
| `allowed-tools` | 技能生效期间，这些工具不用问就能用（事先批准） |
| `disable-model-invocation: true` | 不进清单：只能由你输入 `/<名字>` 来运行 |
| `user-invocable: false` | 不提供 `/<名字>`；模型仍可以加载 |
| `paths` | 通配路径：文件工具碰到匹配的文件后，这个技能才出现在清单里 |
| `context: fork` | 不在当前对话里跑，而是交给一个子代理（`agent` 指定，默认 `general-purpose`）；`disallowed-tools` 从那个代理身上拿掉工具 |
| `model`、`effort` | 加载这个技能的那一轮，剩下的部分用这个模型和推理强度 |
| `version` | 作为元数据保留 |

正文里，`$ARGUMENTS` 是整个参数串，`$0`、`$1`（或 `$ARGUMENTS[0]`）是按 shell 规则拆开的各个词，`$<名字>` 是起了名的参数，`\$` 表示字面的美元符号；没被任何占位符用掉的参数，会以一行 `ARGUMENTS:` 接在正文后面（`noeta/toolkit/arguments.py`）。`${NOETA_SKILL_DIR}`（或 `${CLAUDE_SKILL_DIR}`）是技能所在目录，加载出来的文字第一行是 "Base directory for this skill"，方便模型读取旁边的文件。正文里的 `` !`命令` ``（在行首或空格之后），或以 ```` ```! ```` 开头的代码块，会在技能加载时执行，输出替换掉命令本身。每条命令都按 `Bash` 调用走任务的权限检查；会被拒绝、或需要你回答的命令，会让这个技能加载失败，因为加载的那一刻没人可问（`noeta/toolkit/inline_commands.py`）。

### 技能清单和它的预算

`Skill` 工具自己的描述从来不变；技能是以一份清单的形式交给模型的，每个任务记录一次，和 Claude Code 一样（`noeta/plugins/skills/listing.py`）。之后新加的技能、或描述变了的技能，会在同一个标题下再列一次；删掉的不另行通知。

清单的预算是模型上下文窗口的 1%（按字符算：窗口大小乘以这个模型每个 token 的字符数）。每条描述最多 1,536 个字符。超出预算时，先给每个技能一个短描述（第一句话，最多 24 个 token），预算还有富余再逐个换成完整描述，顺序是：在整个存储的任务里用得多的在前，其次看所在位置，再按名字。名字永远不会被丢掉。要调预算：

| 设置 | 在哪儿 |
|---|---|
| `skillListingBudgetFraction`（0–1）、`skillListingMaxDescChars` | 设置文件（Claude Code 的键） |
| `NOETA_SLASH_COMMAND_TOOL_CHAR_BUDGET` | 环境变量，按字符给出预算，对所有任务生效 |

## 代理文件

一个代理就是一个 markdown 文件：YAML frontmatter 说明它是什么，正文是它的系统提示词（`noeta/agents/agent_files.py`）。格式是 Claude Code 的，外加 noeta 自己的两个字段 `plugins` 和 `one_shot`。

```markdown
---
name: reviewer
description: Reviews a diff for bugs. Use after making changes, before committing.
model: sonnet                 # inherit、别名或模型 id
tools: Read, Bash             # 只提供这些（"*" 表示全部）
disallowedTools: Edit, Write
permissionMode: default
maxTurns: 40
effort: high
---
You review code changes. Read the diff with `git diff`, …
```

| 字段 | 作用 |
|---|---|
| `name`、`description` | 名字，以及模型挑子代理时读的说明 |
| `model` | `inherit`（用调用者的模型）、别名（`sonnet`、`opus`、`haiku`、`fable`）或模型 id |
| `tools`、`disallowedTools` | 只提供这些工具；永远不提供这些（`Bash(npm:*)` 这样的规则按它的工具名算） |
| `permissionMode` | 它运行时的权限模式 |
| `maxTurns` | 每轮最多调用几次模型 |
| `effort` | 推理强度 |
| `omitClaudeMd: true` | 只给它看机器统一管理的那几个说明文件 |
| `identity` | 系统提示词开头那一行身份说明 |
| `plugins` | 它的插件，按顺序：写名字，或 `名字: {参数}`（noeta 自己的字段） |
| `one_shot: true` | 第一轮回答完就结束（noeta 自己的字段） |
| `skills` | 启动时就加载的技能 |
| `mcpServers` | 给它用的 MCP 服务器：直接写定义，或写别处配置好的名字 |
| `hooks` | 它运行期间生效的 hook（作为子代理运行时，它的 `Stop` hook 变成 `SubagentStop`） |
| `memory: user\|project\|local` | 它自己的记忆目录（见[代理记忆](./memory-and-instructions.md#代理记忆)） |
| `isolation: worktree` | 作为子代理时，在自己单独的 git worktree 里跑 |
| `initialPrompt`、`autoCompactWindow`、`criticalSystemReminder_EXPERIMENTAL` | 同 Claude Code |

noeta 不处理的字段（`cacheTtl`、`observer` 等）会记一次日志。没写 `plugins` 的文件（也就是普通的 Claude Code 代理文件）用默认那一套：权限、规划模式、预算、hook、shell、网页、MCP、文件、技能、项目说明、环境、子代理、压缩等等（`noeta/agents/__init__.py` 里的 `DEFAULT_PLUGINS`）。每个代理还都带着 `tool_search` 和 `structured_output`。

### 去哪里找代理文件

| 位置 | 名字 |
|---|---|
| 内置的 `noeta/agents/files/*.md` | `main`、`Explore`、`general-purpose`、`Plan` |
| Claude Code 插件的 `agents/**/*.md` | `<插件>:<代理>` |
| `~/.noeta/agents/*.md` | 按 `name` |
| 已信任项目的 `.noeta/agents/*.md` | 按 `name` |
| `--agents '{"name": {"description": …, "prompt": …}}'`（无界面模式） | 按键名 |

按这个顺序，后面的文件替换前面的同名文件，所以你的 `~/.noeta/agents/Explore.md` 会替换内置的 Explore。终端界面里 `/agents` 列出它们，`/reload-plugins` 重新读取。

内置代理：

| 代理 | 是什么 |
|---|---|
| `main` | 和你对话的那个：所有插件、主系统提示词 |
| `general-purpose` | 做调研和多步骤工作的子代理，有文件和 shell 工具 |
| `Explore` | 只读的搜索子代理（文件工具里只有 `Read`；不能改文件，也不能再开子代理） |
| `Plan` | 只读的子代理，负责设计实现方案 |

模型还可以用 `subagent_type: "fork"` 启动一个自己的副本，它继承整段对话。子代理在后台怎么跑等内容见[子代理](./subagents.md)。

想让终端界面或 `-p` 用 `main` 以外的代理开始：`noeta --agent reviewer`，或在设置文件里写 `"agent": "reviewer"`。

### 不复制文件也能加插件

`config.toml` 可以按名字给代理追加插件，排在文件自己的插件后面；`"*"` 表示所有代理：

```toml
[agents.main]
add_plugins = ["repetition_guard", {my_plugin = {level = 2}}]

[agents."*"]
add_plugins = ["run_tests"]

[plugins.web]
enabled = false          # 对所有代理关掉
```

内置代理列出的插件里，`memory`、`mcp` 和 `repetition_guard` 要等宿主程序设置后才开；`noeta` 命令会打开记忆和 MCP。怎么写插件见[写一个插件](./plugins.md)。

## 斜杠命令

斜杠命令就是你输入的 `/<名字> [参数]`。除了前端自己的命令（见[终端界面](./terminal.md#斜杠命令)），其余都来自代理的插件，所以同一批命令在终端界面、网页版、编辑器和无界面模式里都能用。

**markdown 命令。** `~/.noeta/commands/<名字>.md` 和已信任项目的 `.noeta/commands/<名字>.md` 是 Claude Code 的命令文件。noeta 把它们当技能读（Claude Code 现在也是这样）：你可以输入 `/<名字>`，模型也可以用 `Skill` 加载。子目录里的文件以路径命名（`.noeta/commands/git/fixup.md` 就是 `/git:fixup`）。

```markdown
---
description: Make a fixup commit for the last change to a file
argument-hint: <path>
allowed-tools: Bash(git add *), Bash(git commit *)
---
Stage $ARGUMENTS and make a `git commit --fixup` for the last commit that touched it.
```

**Claude Code 插件里的命令**（插件目录里的 `commands/**/*.md`）通过 `markdown_commands` 插件输入（`noeta/plugins/markdown_commands.py`），命名为 `<插件>:<名字>`；模型也可以加载它们。输入命令后，任务把命令正文加进来：填好参数，`` !`…` `` 行走权限检查后执行，`allowed-tools` 列出的工具从此事先批准。

这个插件自己还带两个提示词命令：`/init`（给这个项目写一份 `NOETA.md`，已有 `NOETA.md` 或 `AGENTS.md` 就改进它）和 `/review [PR | 分支 | 路径]`（审查改动里的缺陷，不改任何文件）。

其他插件的命令：`/compact`、`/plan`、`/loop`、`/mcp`、`/hooks`，以及 MCP 服务器的 prompt（`/mcp__<server>__<prompt>`）。插件用 `commands()` 和 `run_command()` 添加自己的命令（见 [Extension 接口](../reference/extension.md)）。

## Claude Code 插件目录

把一个 Claude Code 插件目录（`.claude-plugin/plugin.json`、`skills/`、`agents/`、`commands/`、`hooks/hooks.json`、`.mcp.json`）放进 `~/.noeta/plugins/` 或已信任项目的 `.noeta/plugins/`，它的各部分会交给本来就读这些格式的插件去加载（`noeta/cli/claude_plugins.py`）：

| 部分 | 加载成 |
|---|---|
| `skills/` | 技能，命名为 `<插件>:<技能>` |
| `agents/**/*.md` | 代理文件，命名为 `<插件>:<代理>` |
| `commands/**/*.md` | 可输入的命令兼技能，命名为 `<插件>:<命令>` |
| `hooks/hooks.json` | hook |
| `.mcp.json` | MCP 服务器（项目里的插件除外：会提示你把它们抄进 `.noeta/mcp.json`） |
| `settings.json` 里的 `agent` | 前端默认用的代理，优先级低于你自己的设置 |

`${CLAUDE_PLUGIN_ROOT}` 和 `${CLAUDE_PLUGIN_DATA}`（`~/.noeta/plugin-data/<名字>`）在各处都会替换，清单里 `userConfig` 的值也按 Claude Code 的方式填进去。Monitor、`bin/`、LSP 服务器、主题和输出样式不加载。安装时固定到某个提交：

```bash
noeta plugin add https://github.com/you/your-plugin@<完整提交号：40 位十六进制，SHA-256 仓库是 64 位>
noeta plugin add --marketplace ./team-marketplace formatter     # 从 marketplace.json 装
noeta plugins                                                    # 加载了哪些、各自从哪来
```

安装过程不执行插件里的任何东西。安装和配置的更多内容见[写一个插件](./plugins.md)。
