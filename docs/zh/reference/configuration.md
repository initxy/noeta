# 配置

Noeta Agent（`python -m noeta.agent`）通过环境变量和可选的 JSON 配置文件进行配置。环境变量优先于文件；文件优先于内置默认值。

## 配置来源

优先级（低 → 高）：

1. **Dataclass 默认值** —— 安全的离线默认值（`stub` provider、`dry_run` 写入、内存存储）。
2. **`NOETA_AGENT_CONFIG` 文件** —— 一个 JSON 对象，其键覆盖默认值（见[下文](#json-config-file-fields)）。
3. **`NOETA_AGENT_*` 环境变量** —— 最高优先级（见[下文](#environment-variables)）。

## 环境变量 { #environment-variables }

| 变量 | 类型 | 默认值 | 用途 |
| --- | --- | --- | --- |
| `NOETA_AGENT_CONFIG` | path | *(无)* | JSON 配置文件的路径。见 [JSON 配置文件字段](#json-config-file-fields)。 |
| `NOETA_AGENT_HOST` | string | `127.0.0.1` | HTTP 服务器绑定的接口。 |
| `NOETA_AGENT_PORT` | int | `8765` | HTTP 服务器监听的端口。`0` = 操作系统分配。 |
| `NOETA_AGENT_WORKSPACE` | path | `$PWD` | 默认工作区目录（代理的文件根目录）。 |
| `NOETA_AGENT_WORKSPACES_FILE` | path | `~/.noeta/workspaces.json` | 工作区（项目）注册表 JSON 存储。 |
| `NOETA_AGENT_MCP_FILE` | path | `~/.noeta/mcp_servers.json` | MCP server 连接器注册表 JSON。 |
| `NOETA_AGENT_STORAGE` | URL | *(无)* | 用于持久化 EventLog + ContentStore + Dispatcher 的存储 URL：SQLite 文件路径或 `postgresql://` DSN。未设置 = 内存（无持久化）。旧写法 `NOETA_AGENT_SQLITE` 仍被接受。 |
| `NOETA_AGENT_PROVIDER` | string | `stub` | Provider adapter：`stub`（离线）、`openai`、`openai-responses`、`anthropic`。 |
| `NOETA_AGENT_MODEL` | string | *(无)* | 已配置 provider 提供的模型标识符。 |
| `NOETA_AGENT_MODELS` | string | *(无)* | 可选模型的逗号分隔列表（启用 UI 中的每轮模型切换）。 |
| `NOETA_AGENT_API_KEY` | string | *(无)* | Provider API key。真实 provider 必需。 |
| `NOETA_AGENT_BASE_URL` | URL | *(无)* | Provider base URL。`openai` 和 `openai-responses` 必需。 |
| `NOETA_AGENT_API_VERSION` | string | *(无)* | API 版本查询参数（`openai-responses` 使用）。 |
| `NOETA_AGENT_MAX_TOKENS` | int | *(无)* | 转发给未携带 token 上限的请求的输出 token 上限。 |
| `NOETA_AGENT_WRITE_MODE` | string | `dry_run` | 文件系统写入策略：`dry_run`（暂存 diff，安全默认值）或 `apply`（执行真实写入）。 |
| `NOETA_AGENT_WORKFLOW_ENABLED` | bool | `false` | `run_workflow` 控制工具的主机终止开关。 |
| `NOETA_AGENT_BACKGROUND_DRIVE` | bool | `true` | 在后台线程上异步驱动轮次（命令端点返回 `202`）。 |
| `NOETA_AGENT_OTLP_ENDPOINT` | URL | *(无)* | OTLP trace 导出：**完整的** OTLP/HTTP traces URL（如 `http://localhost:4318/v1/traces`）。任务 / 工具 / LLM 执行会作为 span 导出到任意 OTLP collector（Jaeger、OpenTelemetry Collector 等）。不设置 = 关闭导出。同时兼容 OTel 标准变量：`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`（原样使用）、`OTEL_EXPORTER_OTLP_ENDPOINT`（自动追加 `/v1/traces`）、`OTEL_EXPORTER_OTLP_HEADERS`（`k=v,k2=v2` 形式的请求头）。 |
| `NOETA_WEB_SEARCH_API_KEY` | string | *(无)* | 启用 `web_search` 内置工具。没有此 key，该工具不会挂载。 |

### 布尔解析

`*_ENABLED` / `*_DRIVE` 布尔值接受 `1`、`true`、`yes`、`on`（不区分大小写）为真；其他一切为假。

## JSON 配置文件字段 { #json-config-file-fields }

通过 `NOETA_AGENT_CONFIG=/path/to/config.json` 传递路径。文件必须包含一个 JSON 对象。所有键都是可选的。

| 键 | 类型 | 默认值 | 用途 |
| --- | --- | --- | --- |
| `host` | string | `127.0.0.1` | 绑定接口。 |
| `port` | int | `8765` | 绑定端口。 |
| `workspace_dir` | string | `$PWD` | 默认工作区目录。 |
| `workspaces_registry_path` | string | `~/.noeta/workspaces.json` | 工作区注册表存储。 |
| `mcp_servers_registry_path` | string | `~/.noeta/mcp_servers.json` | MCP 连接器注册表。 |
| `storage_url` | string | *(无)* | 持久化存储 URL（见上文环境变量）。旧键 `sqlite_path` 仍被接受。 |
| `provider_id` | string | `stub` | Provider adapter id。 |
| `model` | string | *(无)* | 模型 id。 |
| `models` | list[string] | `[]` | 可选模型列表。 |
| `api_key` | string | *(无)* | Provider API key。 |
| `base_url` | string | *(无)* | Provider base URL。 |
| `api_version` | string | *(无)* | API 版本。 |
| `max_tokens` | int | *(无)* | 输出 token 上限。 |
| `default_headers` | object[string→string] | `{}` | Provider 请求的额外 HTTP 头（例如网关 `X-TT-LOGID`）。仅文件可用。 |
| `write_mode` | string | `dry_run` | 写入策略。 |
| `workflow_enabled` | bool | `false` | 工作流工具门控。 |
| `background_drive` | bool | `true` | 异步轮次驱动。 |
| `otlp_endpoint` | string | *(无)* | OTLP trace 导出 URL（见上方环境变量）。 |
| `otlp_headers` | object[string→string] | `{}` | 附加在每个 OTLP 导出请求上的请求头（托管 collector 的鉴权）。导出内容只含 audit 白名单投影——不含 goal、工具参数或消息正文。 |

### 示例

```json
{
  "provider_id": "openai",
  "model": "gpt-4o-mini",
  "base_url": "https://api.openai.com/v1",
  "api_key": "sk-…",
  "workspace_dir": ".",
  "storage_url": ":memory:",
  "host": "127.0.0.1",
  "port": 8765
}
```

## Provider adapters

| `provider_id` | 说明 |
| --- | --- |
| `stub` | *(默认)* 离线确定性两轮 LLM 替身。无需 API key，无需网络。用于安装和接线冒烟测试。 |
| `openai` | OpenAI 兼容的 `/chat/completions` 端点。需要 `api_key` + `base_url`。 |
| `openai-responses` | OpenAI Responses API。需要 `api_key` + `base_url`（完整 responses 端点）。通过 `image_resolver` 支持视觉。消费 `api_version` + `max_tokens`。 |
| `anthropic` | Anthropic Messages API。需要 `api_key`。可选 `base_url`、`max_tokens`、`default_headers`。支持视觉。 |

## 写入与 Shell 安全

- **写入** 默认是 `dry_run`：`edit` / `write` / `apply_patch` 发出 unified-diff artifact 而不修改字节。设置 `write_mode: apply`（或 `NOETA_AGENT_WRITE_MODE=apply`）以执行真实写入。
- **`shell_run`** 默认由 `ShellMode.ALLOWLIST` 门控：只有允许列表中的 argv 模式可以通过（`git status`、`git diff`、`pytest`、`uv run pytest`、`npm test`、`pnpm test`）。Shell 元字符在分词之前被拒绝。这是**路径包含 + 允许列表，而非进程沙箱**——`shell_run` 在受信任的工作区中生成外部程序。

## 来源

权威配置解析位于 `noeta.agent.backend.lifecycle.BackendConfig.from_env`（`apps/noeta-agent/noeta/agent/backend/lifecycle.py`）。Provider 构建在同一模块的 `build_provider()` 中。
