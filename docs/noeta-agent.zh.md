# Noeta 编码代理（`python -m noeta.agent`） { #noeta-coding-agent-python--m-noetaagent }

一个工作区范围的编码代理：它读取、编辑、运行 shell 命令，并在一个目录上持有多轮会话，将每一步记录在持久化 EventLog 中，以便运行状态可以通过 fold 该日志离线重新推导。本文档是代理的**精简地图**——它命名当前接口并指向权威来源（代码或 `docs/adr/`）。它**不**复制 schema 或散文；如有疑问，请阅读引用的文件。

## 入口点 { #entry-point }

**唯一**入口是 `python -m noeta.agent`（零位置参数；所有配置通过 `NOETA_AGENT_*` 环境变量或 `NOETA_AGENT_CONFIG` JSON 文件）。它启动 HTTP/SSE 聊天服务器 + 捆绑的 Web SPA，位于 `<url>/chat`，并阻塞直到 SIGINT/SIGTERM。没有 `noeta` 控制台脚本，也没有操作员 CLI。

```bash
NOETA_AGENT_WORKSPACE=./my-project \
NOETA_AGENT_PROVIDER=openai NOETA_AGENT_MODEL=gpt-5.5 NOETA_AGENT_API_KEY=… NOETA_AGENT_BASE_URL=… \
python -m noeta.agent
# → noeta.agent serving at http://127.0.0.1:<port>/ ; chat composer at <url>/chat
```

* 启动器 + 环境变量解析：`apps/noeta-agent/noeta/agent/__main__.py` 和 `apps/noeta-agent/noeta/agent/backend/lifecycle.py` 中的 `BackendConfig.from_env`（`NOETA_AGENT_*` 旋钮的权威列表——workspace、port、host、provider/model/key/base_url、write mode、shell mode、MCP/workspace/session registries 等）。`NOETA_AGENT_PROVIDER` 默认为离线 `stub` 替身；`openai` / `anthropic` / `openai-responses` 是真实 adapters。
* 库使用（无服务器）：`noeta.sdk` 导出 `Options`、`query`、`Client`、`compile_options`（`packages/noeta-sdk/noeta/sdk/__init__.py`）；官方四代理配方是 `noeta.presets.main_options()` / `official_specs()`（`packages/noeta-runtime/noeta/presets/__init__.py`）。

## 工具接口 { #tool-surface }

内置工具在 `packages/noeta-runtime/noeta/tools/` 中组装。名称是 provider 安全的 snake_case，是模型调用的字符串。真相来源：`noeta/tools/fs/__init__.py`（`build_fs_tools`）加上 `app/` 和 `web/` 包。

| 工具 | 风险 | 它做什么 | 来源 |
| --- | --- | --- | --- |
| `read` | low | 读取工作区文件（utf-8），可按行 `offset`/`limit` 切片。 | `noeta/tools/fs` |
| `glob` | low | 匹配工作区相对 glob 并返回匹配路径。 | `noeta/tools/fs` |
| `grep` | low | 跨工作区的正则（Python `re`）内容搜索。 | `noeta/tools/fs` |
| `edit` | high | 替换现有文件中精确、唯一的 `old` 子串。 | `noeta/tools/fs` |
| `write` | high | 写入文件（创建，或覆盖你已读取的文件）。 | `noeta/tools/fs` |
| `apply_patch` | high | 原子性地应用一小批编辑——全部成功或全部不执行。 | `noeta/tools/fs` |
| `shell_run` | high | 在工作区中运行 shell 命令（受模式门控，见下文）。 | `noeta/tools/fs` |
| `shell_poll` | low | 检查后台 shell 作业的状态/输出。 | `noeta/tools/fs` |
| `shell_kill` | high | 停止你启动的后台 shell 作业。 | `noeta/tools/fs` |
| `run_skill_script` | high | 通过白名单解释器运行活跃技能的捆绑脚本。 | `noeta/tools/fs` |
| `open_app` | low | 在 Web "App" 面板中渲染工作区 HTML 应用（单端口预览网关）。 | `noeta/tools/app` |
| `webfetch` | low | 通过 HTTP(S) 获取公共网页，渲染为 Markdown。 | `noeta/tools/web` |
| `web_search` | low | 运行 Web 搜索并返回排名结果为 Markdown（key 门控：仅在设置了 `NOETA_WEB_SEARCH_API_KEY` 时存在）。 | `noeta/tools/web` |

没有单独的 `read_file` / `write_file` / `replace_text` / `list_dir` / `git_status` / `git_diff` 工具——那些旧名称已被重命名（`read`/`write`/`edit`）或移除（`list_dir`）；`git status`/`git diff` 现在只是 `shell_run` 内的白名单规则。远程 MCP 工具动态显示为 `mcp__<alias>__<tool>`（`noeta/tools/mcp/tool.py`）。

## 代理预设 { #agent-presets }

`noeta.presets` 提供与 Claude Code 名册对齐的官方四人组（`packages/noeta-runtime/noeta/presets/__init__.py`）。代理在 `POST /tasks` 请求体中**按任务**选择（`{"goal": …, "agent": …}`），而不是在进程启动时；自定义代理通过扁平的 `Options.agents` dict 传入。

| 代理 | 角色 |
| --- | --- |
| `main` | 默认编码代理：完整内置工具集 + 可以生成三个子代理 + 所有能力。 |
| `general-purpose` | 自包含编码 worker：完整读/写/编辑/shell 集，无委派。 |
| `explore` | 只读侦察兵：glob/grep/read + 只读 shell，扇出以报告事实，从不编辑。 |
| `plan` | 只读架构师：读取代码并返回有序的实现计划，从不写入。 |

运行器在 Engine 看到工具包**之前**按代理的 `allowed_tools` 过滤它，并且 `PermissionGuard` 使用相同的白名单，因此被禁止的工具可证明不可达。见[ADR：Library-SDK 架构](adr/library-sdk-architecture.md)（Options 创建接口）和[ADR：工具和代理目录](adr/tool-and-agent-catalog.md)。

## 技能 { #skills }

技能包是 `<workspace>/.noeta/skills/<name>/SKILL.md`（加上全局 `~/.noeta/skills` 层）——YAML frontmatter（`name`、`description`、可选的 `version`/`priority`）+ Markdown 主体，任何同级文件被捆绑为按需资源。激活是**两阶段**且模型驱动的：在启动时索引将菜单（名称 + 一行描述）渲染到 `skill` 控制工具的 schema 中；当模型调用 `skill: <name>` 时，主体加上绝对 base-directory 行被 fold 到下一轮的半稳定上下文中，模型按需 `read` 捆绑资源（无急切内联）。

```text
# model picks from the skill menu, then calls the control tool:
skill: pdf-extract
# → next turn carries SKILL.md body + "Base directory: <abs path>"; model reads resources via `read`.
```

权威来源：[ADR：模型驱动的技能调用](adr/model-driven-skill-invocation.md) 和[ADR：技能资源按需加载](adr/skill-resource-on-demand.md)；索引器代码在 `packages/noeta-runtime/noeta/context/skills/`。

## 写入和 shell 安全 { #write--shell-safety }

写入**默认是 dry-run**。`NOETA_AGENT_WRITE_MODE` 主机策略（`dry_run`（默认）vs `apply`）决定 `edit`/`write`/`apply_patch` 是更改字节还是仅发出提议的 unified-diff artifact——它是主机配置，不是请求字段。`apply_patch` 是全有或全无路径（验证每个编辑，然后写入；应用错误时进程内回滚）；序列化的 `edit`/`write` 调用是非原子的。

`shell_run` 受 `NOETA_AGENT_SHELL_MODE` 门控：`allowlist`（默认——`git status`/`git diff`/`pytest`/`uv run pytest`/`npm test`/`pnpm test` 的仅 argv 结构性白名单，shell 元字符在分词前被拒绝）或 `off`。这是路径包含 + 白名单，**不是**进程沙箱——`shell_run` 在受信任的工作区中生成外部程序。

每个路径都通过 `WorkspaceRoot`（realpath + 包含检查，符号链接安全；在任何 IO 之前检查），因此绝对路径 / `..` / 树外符号链接转义在读取或写入之前失败。审批和写入/shell 门控被表达为中立控制机制——见[ADR：控制工具中立机制](adr/control-tools-neutral-mechanism.md) 和[ADR：Shell 权限和后台](adr/shell-permission-and-background.md)。

## HTTP 接口 { #http-surface }

`python -m noeta.agent` 为捆绑的 Web UI 提供 HTTP/SSE 后端。这是**本地 UI 的验收接口，不是稳定的版本化公共 API**：请求体从不接受 provider / base_url / 凭据（主机端的 `NOETA_AGENT_*` 配置是权威的）。

完整路由表见[参考 › HTTP API](reference/http-api.md)。路由在 `apps/noeta-agent/noeta/agent/backend/` 下的这些模块中注册：

- `task_protocol.py` — SSE 流（`GET /stream?task=<id>`）+ 任务命令
- `resource_services.py` — 内容 / 文件 / 文件（数据平面）
- `read_views.py` — 能力 + 会话列表
- `mcp_service.py` — MCP 连接器管理（`/mcp/servers/*`）
- `workspace_service.py` — 工作区（项目）管理
- `app.py` — 路由根、静态资产、预览网关、`/health`

配置解析：`backend/lifecycle.py` → `BackendConfig.from_env`。另见：[配置](reference/configuration.md)。

## MCP 和钩子 { #mcp--hooks }

* **MCP** — 远程/stdio 连接器注册在 `~/.noeta/mcp_servers.json`（alias → transport/url/credentials；凭据永远不会在请求体中传递）并按会话启用；它们的工具显示为 `mcp__<alias>__<tool>`。使用 `NOETA_AGENT_MCP_FILE` 覆盖注册表路径。见[ADR：MCP 连接器](adr/mcp-connectors.md)。
* **钩子** — 唯一的扩展角色是 **Guard**（在 `before_tool_call` / `before_spawn_subtask` / `before_finish` 否决）和 **Observer**（只读）。没有 Mutator 角色。见[ADR：Guard-observer 钩子](adr/guard-observer-hooks.md)。

## 子代理扇出 { #sub-agent-fan-out }

`main` 可以并行生成子代理；结果是子代理的返回值，记录到 EventLog 中，以便整个树 fold 回状态——见[ADR：子任务扇出和持久唤醒](adr/subtask-fanout-and-durable-wake.md)。
