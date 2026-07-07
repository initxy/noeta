# 内置工具

Noeta 提供一组内置工具，由文件系统包、Web 包、App 包以及（条件性的）内存和 MCP 工具组装而成。工具名称是 provider 安全的 `snake_case`，是模型调用的确切字符串。

## 文件系统工具

由 `noeta.tools.fs` 中的 `build_fs_tools()` 构建。每个工具携带一个 `risk_level`，供 `PermissionGuard` 使用。

| 工具 | 风险 | 用途 | 来源 |
| --- | --- | --- | --- |
| `read` | low | 读取工作区文件（utf-8），可选按行 `offset` / `limit` 切片。 | `noeta/tools/fs/read.py` |
| `glob` | low | 匹配工作区相对 glob 模式并返回匹配路径。 | `noeta/tools/fs/read.py` |
| `grep` | low | 跨工作区的正则（`re` 模块）内容搜索。 | `noeta/tools/fs/read.py` |
| `edit` | high | 替换现有文件中精确、唯一的 `old` 子串。默认 dry-run。 | `noeta/tools/fs/edit.py` |
| `write` | high | 写入文件（创建，或覆盖先前读取过的文件）。默认 dry-run。 | `noeta/tools/fs/edit.py` |
| `apply_patch` | high | 原子性地应用一小批编辑——全部成功或全部失败。默认 dry-run。 | `noeta/tools/fs/patch.py` |
| `shell_run` | high | 在工作区中运行 shell 命令。受模式门控：默认 `ALLOWLIST`，`OFF` 完全移除该工具。 | `noeta/tools/fs/shell.py` |
| `shell_poll` | low | 检查后台 shell 作业的状态 / 输出。 | `noeta/tools/fs/shell.py` |
| `shell_kill` | high | 停止你启动的后台 shell 作业（SIGTERM → SIGKILL）。 | `noeta/tools/fs/shell.py` |
| `run_skill_script` | high | 通过允许列表中的解释器运行活动技能的捆绑脚本。 | `noeta/tools/fs/skill_script.py` |

### Shell 允许列表（默认）

当 `shell_mode = ALLOWLIST` 时，只有这些 argv 模式可以通过：

- `git status` / `git diff`
- `pytest` / `uv run pytest`
- `npm test` / `pnpm test`

Shell 元字符（`|`、`;`、`&&`、`>` 等）在分词之前被拒绝。这是**路径包含 + 允许列表，而非进程沙箱**——`shell_run` 在受信任的工作区中生成外部程序。

## Web 工具

| 工具 | 风险 | 用途 | 来源 |
| --- | --- | --- | --- |
| `webfetch` | low | 通过 HTTP(S) 获取公共网页并渲染为 Markdown。始终可用。 | `noeta/tools/web/fetch.py` |
| `web_search` | low | 运行 Web 搜索并以 Markdown 返回排名结果。**仅在设置 `NOETA_WEB_SEARCH_API_KEY` 时挂载。** | `noeta/tools/web/search.py` |

## App 工具

| 工具 | 风险 | 用途 | 来源 |
| --- | --- | --- | --- |
| `open_app` | low | 通过单端口预览网关在 Web "App" 面板中渲染工作区 HTML 应用。 | `noeta/tools/app/open_app.py` |

## 内存工具

仅在启用 `Capabilities.memory` 时挂载（只有 `main` 预设开启它）。

| 工具 | 风险 | 用途 | 来源 |
| --- | --- | --- | --- |
| `memory_write` | low | 将 markdown 内存文件写入内存存储。 | `noeta/tools/memory.py` |
| `memory_read` | low | 按需读取已存储内存的完整文本。 | `noeta/tools/memory.py` |

## MCP 工具

当 MCP server 被注册并在每个会话中启用时，远程 MCP 工具动态显示为 `mcp__<alias>__<tool>`。见[ADR：MCP 连接器](https://github.com/initxy/noeta/blob/main/docs/adr/mcp-connectors.md)。

## 工具风险等级

| 等级 | 含义 |
| --- | --- |
| `low` | 代理自身状态之外无副作用。始终允许。 |
| `high` | 修改文件系统或生成外部进程。须经 `PermissionGuard` 批准。 |

## 备注

- 没有单独的 `read_file` / `write_file` / `replace_text` / `list_dir` / `git_status` / `git_diff` 工具。那些旧名称已被重命名（`read` / `write` / `edit`）或移除（`list_dir`）。`git status` / `git diff` 是 `shell_run` 内部的允许列表规则。
- `write` 工具在构建时接受一个可选的 `allowed_path_globs` 工作区相对白名单（空 = 无限制）。`edit` 和 `apply_patch` 忽略白名单。
