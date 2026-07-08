# 使用 coding agent

**目标：** 将 `python -m noeta.agent` 用于真实的编码任务——配置工作区、使用代理预设、管理会话以及操作技能。

**开始之前：** 你已安装 Noeta 并配置了真实 provider（参见[配置 provider](configure-provider.md)）。

## 用你的工作区启动代理

```bash
NOETA_AGENT_WORKSPACE=./my-project \
NOETA_AGENT_PROVIDER=anthropic \
NOETA_AGENT_MODEL=claude-sonnet-4-5-20250929 \
NOETA_AGENT_API_KEY=sk-ant-… \
NOETA_AGENT_STORAGE=./my-project/session.sqlite \
NOETA_AGENT_WRITE_MODE=apply \
python -m noeta.agent
```

关键变量：

| 变量 | 为什么设置它 |
| --- | --- |
| `NOETA_AGENT_WORKSPACE` | 代理读取和编辑的目录。默认为 `.`。 |
| `NOETA_AGENT_STORAGE` | EventLog 的持久化存储。没有它，会话会随进程消亡。 |
| `NOETA_AGENT_WRITE_MODE` | `apply` 让代理实际写入文件。默认 `dry_run` 仅提议 diff。 |

在浏览器中打开打印出的 URL，导航到 `/chat`。

## 选择代理预设

代理是**按任务**选择的——创建新对话时，你可以选择使用哪个代理。内置的四个预设：

| 预设 | 适用场景 |
| --- | --- |
| `main` | 默认。完整工具面，可派生子代理。最适合通用编码工作。 |
| `general-purpose` | 自包含：读取、写入、编辑、运行 shell。不委派。 |
| `explore` | 只读侦察。用它来理解代码库而无编辑风险。 |
| `plan` | 只读架构师。返回有序的实现计划。 |

当你希望代理理解新代码库而不做任何修改时，选择 `explore`；当你希望它做出更改时，选择 `main`。

## 发送消息并观察 trace

在聊天输入框中输入你的请求——例如：

```
查找所有导入 `pydantic` 的 Python 文件，并列出它们使用了其中的哪些内容。
```

随着代理的工作，trace 视图会填充事件：LLM 轮次、每次工具调用（`grep`、`read`、`glob`）以及工具结果。你可以检查每轮的 token 用量和 cache 命中率。

如果代理提议编辑且 `NOETA_AGENT_WRITE_MODE=apply`，文件会立即更改。如果 `write_mode=dry_run`（默认），你会看到一个统一 diff 工件——便于安全评估。

## 管理会话

左侧边栏显示会话列表（仅根对话；子任务挂载在父级的流上）。每行显示状态、标题（来自第一条消息）和代理名称。

- **创建** — 点击"New session"或从空状态发送消息。
- **恢复** — 点击会话以继续。代理会 fold EventLog 以恢复状态，因此即使你重启了服务器，对话也会从上次中断的地方继续。
- **关闭 / 重新打开** — 右键点击或使用会话菜单。关闭会归档会话；重新打开会使其再次激活。
- **取消** — 在轮次中途停止正在运行的会话。部分状态保留在日志中。
- **删除** — 从存储中硬删除会话及其子任务树。不可逆。

## 使用技能

技能是基于 Markdown 的能力包，模型可以按需激活。将技能放入你的工作区：

```
my-project/
└── .noeta/
    └── skills/
        └── pdf-extract/
            └── SKILL.md
```

`SKILL.md` 包含 YAML frontmatter 加 Markdown 正文：

```markdown
---
name: pdf-extract
description: Extract text and tables from PDF files
version: "1"
---

# PDF Extract

Use `pdftotext` (shell) to extract text from a PDF file.
Call it with the file path.
```

当模型决定需要 PDF 提取时，它会调用 `skill: pdf-extract`，技能正文会被 fold 到下一轮的上下文中。然后模型使用捆绑的资源（通过 `read`）来执行任务。

全局技能放在 `~/.noeta/skills/` 中，对所有工作区可用。

## 批准受控工具调用

当 `NOETA_AGENT_WRITE_MODE=apply` 且 `permission_mode=default`（`main` 的默认值）时，某些工具调用在执行前需要你的批准：

- `edit`、`write`、`apply_patch` — 文件修改
- `shell_run` — shell 命令（即使在 `allowlist` 模式下，某些命令也可能需要批准）

聊天界面会显示待批准的工具调用详情。点击**Approve**让其执行，或点击**Deny**阻止它。批准或拒绝会记录在 EventLog 中。

## 另请参阅

- [Coding agent 参考](../reference/noeta-agent.md) — 所有环境变量、工具和预设
- [HTTP 接口参考](../reference/http-api.md) — UI 背后的路由
- [配置 provider](configure-provider.md) — 连接真实 LLM
- [构建自定义工具](build-custom-tools.md) — 扩展工具面
