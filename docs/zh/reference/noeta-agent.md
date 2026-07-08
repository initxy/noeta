# Noeta 编码代理（`python -m noeta.agent`）

一个工作区级别的编码代理。它读取、编辑、运行 shell 命令，并在一个目录上保持多轮会话，将每一步记录在持久化的 EventLog 中，以便通过 fold 该日志离线重新推导运行状态。将它指向一个目录，启动服务器，通过附带的 web UI 或 HTTP 接口驱动它。

## 启动服务器

**唯一**入口是 `python -m noeta.agent`——零位置参数，所有配置通过环境变量或 JSON 配置文件完成。它启动一个 HTTP/SSE 聊天服务器以及附带的 web SPA（位于 `<url>/chat`），并阻塞直到收到 SIGINT/SIGTERM。没有 `noeta` 控制台脚本，也没有运维 CLI。

```bash
NOETA_AGENT_WORKSPACE=./my-project \
NOETA_AGENT_PROVIDER=openai \
NOETA_AGENT_MODEL=gpt-5.5 \
NOETA_AGENT_BASE_URL=https://api.openai.com/v1 \
NOETA_AGENT_API_KEY=sk-… \
NOETA_AGENT_STORAGE=./session.sqlite \
python -m noeta.agent
# → noeta.agent serving at http://127.0.0.1:<port>/ ; chat at <url>/chat
```

启动器和环境变量解析位于 `apps/noeta-agent/noeta/agent/__main__.py` 和 `apps/noeta-agent/noeta/agent/host/runner_cli.py` 中的 `RunnerConfig.from_env`——`NOETA_AGENT_*` 旋钮的权威列表。

对于库使用（不启动服务器），SDK 从 `noeta.sdk` 导出 `Options`、`query`、`Client`、`compile_options`（`packages/noeta-sdk/noeta/sdk/__init__.py`）；官方四代理配方是 `noeta.presets.main_options()` / `official_specs()`（`packages/noeta-runtime/noeta/presets/__init__.py`）。

## 环境配置

| 变量 | 默认值 | 控制内容 |
| --- | --- | --- |
| `NOETA_AGENT_WORKSPACE` | `.` | 代理操作的目录 |
| `NOETA_AGENT_PROVIDER` | `stub` | LLM 适配器：`stub`、`openai`、`anthropic`、`openai-responses` |
| `NOETA_AGENT_MODEL` | provider 默认值 | 传递给 provider 的模型名称 |
| `NOETA_AGENT_API_KEY` | — | Provider API key |
| `NOETA_AGENT_BASE_URL` | provider 默认值 | 覆盖 base URL（例如用于兼容 OpenAI 的端点） |
| `NOETA_AGENT_STORAGE` | `:memory:` | 持久化 SQLite 文件路径；`:memory:` 仅用于开发 / 测试 |
| `NOETA_AGENT_WRITE_MODE` | `dry_run` | `dry_run`（仅提议 diff）或 `apply`（执行真实写入） |
| `NOETA_AGENT_SHELL_MODE` | `allowlist` | `allowlist`（argv 结构化白名单）或 `off` |
| `NOETA_AGENT_HOST` | `127.0.0.1` | 绑定地址 |
| `NOETA_AGENT_PORT` | `0`（OS 分配） | 监听端口 |
| `NOETA_AGENT_CONFIG` | — | JSON 配置文件路径（替代单独的环境变量） |

`NOETA_AGENT_PROVIDER=stub`（默认值）是一个完全离线、确定性的 LLM 替身——不需要 API key 或网络。用它来在全新检出时验证安装、存储和接线。

## 内置工具

工具名称是 provider 安全的 `snake_case`，即模型调用时使用的精确字符串。事实来源：`noeta.tools.fs.build_fs_tools()` 加上 `packages/noeta-sdk/noeta/tools/` 中的 `app/` 和 `web/` 包。

| 工具 | 风险 | 功能 |
| --- | --- | --- |
| `read` | 低 | 读取工作区文件（UTF-8），可选按 `offset` / `limit` 切片 |
| `glob` | 低 | 匹配工作区相对 glob，返回匹配的路径 |
| `grep` | 低 | 在工作区中进行正则（`re`）内容搜索 |
| `edit` | 高 | 替换现有文件中精确、唯一的 `old` 子串 |
| `write` | 高 | 写入文件（创建，或覆盖之前已读取的文件） |
| `apply_patch` | 高 | 原子地应用一批编辑——全部成功或全部不执行 |
| `shell_run` | 高 | 在工作区中运行 shell 命令（受模式门控） |
| `shell_poll` | 低 | 检查后台 shell 作业的状态 / 输出 |
| `shell_kill` | 高 | 停止你启动的后台 shell 作业 |
| `run_skill_script` | 高 | 通过白名单解释器运行活跃 skill 的附带脚本 |
| `open_app` | 低 | 在 web "App" 面板中渲染工作区 HTML 应用 |
| `webfetch` | 低 | 获取公开网页，渲染为 Markdown |
| `web_search` | 低 | 执行 web 搜索，返回排名结果（需 key：`NOETA_WEB_SEARCH_API_KEY`） |

没有单独的 `read_file` / `write_file` / `replace_text` / `list_dir` / `git_status` / `git_diff` 工具——那些旧名称已被重命名（`read` / `write` / `edit`）或移除；`git status` / `git diff` 现在是 `shell_run` 内的白名单规则。

远程 MCP 工具动态显示为 `mcp__<alias>__<tool>`（`noeta/tools/mcp/tool.py`）。

## 代理预设

`noeta.presets` 交付官方四件套，与 Claude Code 的阵容对齐（`packages/noeta-runtime/noeta/presets/__init__.py`）。代理是**按任务**在 `POST /tasks` 请求体中选择的（`{"goal": …, "agent": …}`），而非在进程启动时；自定义代理通过扁平的 `Options.agents` 字典配置。

| 代理 | 角色 |
| --- | --- |
| `main` | 默认编码代理：完整内置工具面，可派生三个子代理，具备所有能力 |
| `general-purpose` | 自包含编码 worker：完整的读 / 写 / 编辑 / shell 集，不委派 |
| `explore` | 只读侦察兵：glob / grep / read + 只读 shell，扇出报告事实，从不编辑 |
| `plan` | 只读架构师：读取代码并返回有序的实现计划，从不写入 |

运行器在引擎看到工具包**之前**按代理的 `allowed_tools` 过滤工具包，且 `PermissionGuard` 使用相同的白名单，因此被禁止的工具可证明地不可达。

## Skills

一个 skill 包是 `<workspace>/.noeta/skills/<name>/SKILL.md`（加上全局 `~/.noeta/skills` 层）——YAML frontmatter（`name`、`description`、可选的 `version` / `priority`）+ Markdown 正文，任何同级文件作为按需资源附带。

激活是**两阶段**且模型驱动的：

1. 启动时，索引将菜单（名称 + 一行描述）渲染到 `skill` 控制工具的 schema 中。
2. 当模型调用 `skill: <name>` 时，正文加上绝对基目录行被 fold 进下一轮的半稳定上下文，模型按需 `read` 附带资源（不急于内联）。

```text
# 模型从 skill 菜单中选择，然后调用控制工具：
skill: pdf-extract
# → 下一轮携带 SKILL.md 正文 + "Base directory: <abs path>"
# → 模型通过 `read` 读取资源。
```

索引器代码：`packages/noeta-sdk/noeta/context/skills/`。

## 写入与 shell 安全

写入**默认是 dry-run**。`NOETA_AGENT_WRITE_MODE`（`dry_run` vs `apply`）决定 `edit` / `write` / `apply_patch` 是改变字节还是仅发出提议的 unified-diff 产物。这是主机配置，不是请求字段——客户端无法升级到 `apply`。`apply_patch` 是全有或全无路径（验证每个编辑，然后写入；应用出错时进程内回滚）；顺序的 `edit` / `write` 调用是非原子的。

`shell_run` 受 `NOETA_AGENT_SHELL_MODE` 门控：`allowlist`（默认——`git status` / `git diff` / `pytest` / `uv run pytest` / `npm test` / `pnpm test` 的仅 argv 结构化白名单；shell 元字符在分词前被拒绝）或 `off`。

每个路径都经过 `WorkspaceRoot`（realpath + 包含检查，符号链接安全；在任何 IO 之前检查），因此绝对路径 / `..` / 树外符号链接逃逸在读取或写入之前就会失败。审批和写入 / shell 门控被表达为中立的控制机制——参见 [Guard vs Observer](../concepts/guard-observer.md)。

## MCP 与 hooks

**MCP**——远程或 stdio 连接器注册在 `~/.noeta/mcp_servers.json` 中（别名 → 传输 / url / 凭据；凭据永远不在请求体中传递），并通过 `enabled_mcp` 字段按会话启用。它们的工具显示为 `mcp__<alias>__<tool>`。

**Hooks**——仅有的扩展角色是 **Guard**（在 `before_tool_call` / `before_spawn_subtask` / `before_finish` 否决或变更）和 **Observer**（对已提交事件的只读订阅者）。没有 Mutator 角色——参见 [Guard vs Observer](../concepts/guard-observer.md)。

## 子代理扇出

`main` 可以并行派生三个子代理（`general-purpose`、`explore`、`plan`）。每个派生子任务是一个独立的事件溯源任务，拥有自己的 EventLog；结果通过 `SubtaskCompleted` 唤醒记录到父任务的日志中，因此整棵树可以 fold 回状态。参见 [唤醒与恢复](../concepts/wake-resume.md)。

## 另见

- [HTTP 接口参考](http-api.md)——后端服务的每条路由
- [SDK 参考](sdk.md)——编程等价物
- [操作指南：使用编码代理](../how-to/use-the-coding-agent.md)
- [配置 provider](../how-to/configure-provider.md)
