# HTTP 接口

`python -m noeta.agent` 为附带的 Web UI 提供 HTTP/SSE 后端。这是**本地 UI 的验收接口，而非稳定的版本化公共 API**：请求 body 从不接受 provider / base_url / 凭据（主机端的 `NOETA_AGENT_*` 配置是权威的）。

所有路由位于 base URL 之下（默认 `http://127.0.0.1:8765/`）。

## SSE 流

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/stream?task=<id>` | 任务的多路复用 SSE 事件流。`Last-Event-ID` 头从序列号恢复。页面按任务在客户端过滤。 |

流以 JSON 形式携带规范的 `EventEnvelope` 记录，由 `taskId` 寻址。Envelope 的 `seq` 兼作 SSE id，因此 `Last-Event-ID` 可以在流中途恢复。

在流式 LLM 调用进行期间，同一条流还会携带**短暂的 token 增量帧**：具名的 `event: delta` 帧，数据为 `{"task_id", "call_id", "kind": "text"|"thinking", "text", "index"}`。delta 帧**不带 SSE id**——恢复游标不会因它移动，断线重连只补发 envelope，消费过慢时 delta 可能被丢弃。它只是实时预览：持久的真相始终是随后到达的 `MessagesAppended` envelope。通过 `EventSource.addEventListener("delta", …)` 消费；`onmessage` 只会收到 envelope。

## 任务命令

所有命令端点返回 `202 {"task_id": "<id>"}`（仅确认）；可见变化通过 SSE 流到达（唯一真相来源）。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/tasks` | 创建任务。Body：`goal`（string）、`agent`（string，可选）、`model`（string，可选每轮选择器）、`effort`（string，可选）、`permission_mode`（string，可选）、`enabled_mcp`（list，可选）、`workspace`（string，可选）、`images`（list，可选）。 |
| `POST` | `/tasks/{id}/messages` | 向现有任务追加后续目标。Body 字段与创建相同（除 `agent` 外）。 |
| `POST` | `/tasks/{id}/approve` | 批准门控工具调用。Body：`call_id`、`reason`。 |
| `POST` | `/tasks/{id}/deny` | 拒绝门控工具调用。Body：`call_id`、`reason`。 |
| `POST` | `/tasks/{id}/answer` | 回答模型提出的问题。Body：`question_id`、`answers`（dict）。 |
| `POST` | `/tasks/{id}/cancel` | 取消任务。Body：`reason`（默认 `"cancelled"`）、`cascade`（bool，取消子任务）。 |
| `POST` | `/tasks/{id}/close` | 关闭对话。Body：`reason`（可选）。 |
| `POST` | `/tasks/{id}/reopen` | 重新打开已关闭的对话。Body：`reason`（可选）。 |
| `DELETE` | `/tasks/{id}` | 硬删除会话（任务 + 子任务树）。返回 `200` 及已清除的 id，运行中返回 `409`，未知返回 `404`。 |

## 只读视图

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/tasks` | 会话列表（仅根对话，最近优先）。每行携带 `task_id`、`status`、`closed`、`title`、`agent_name`、`workspace_dir`、`workspace_name`。 |
| `GET` | `/capabilities` | Composer 的可选接口：`agents`、`models`、`model_capabilities`、`permission_modes`、`effort_modes`、`mcp_servers`、`workspaces`。 |

## 资源服务

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/content/{hash}` | 按哈希解码的 content-ref 主体。媒体类型从 magic bytes 嗅探。 |
| `GET` | `/files?task=<id>` | 任务工作区的工作区文件树（沙箱化，只读投影）。 |
| `GET` | `/file?task=<id>&path=<rel>` | 单文件预览。返回 `{path, size, truncated, content}`（utf-8，最大 1 MB）。 |

## 工作区管理

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/workspaces` | 列出工作区（项目）注册表条目。 |
| `POST` | `/workspaces` | 添加工作区。Body：`path`、`name`（可选）。 |
| `DELETE` | `/workspaces/{id}` | 按 id 移除工作区。 |

## MCP server 管理 { #mcp-server-management }

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/mcp/servers` | 列出已注册的 MCP server 连接器。 |
| `POST` | `/mcp/servers` | 注册新的 MCP server。 |
| `PUT` | `/mcp/servers/{alias}` | 更新 MCP server 的配置。 |
| `DELETE` | `/mcp/servers/{alias}` | 移除 MCP server。 |
| `GET` | `/mcp/servers/{alias}/tools` | 列出 MCP server 提供的工具。 |
| `PUT` | `/mcp/servers/{alias}/tools` | 设置 MCP server 的工具允许列表。 |
| `GET` | `/mcp/servers/{alias}/prompts` | 列出 MCP server 提供的 prompts。 |
| `GET` | `/mcp/servers/{alias}/resources` | 列出 MCP server 提供的 resources。 |

## 静态资源 & UI

这些是前缀路由（不在 API 路由器中），从附带的前端构建提供：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/` | 重定向到 `/chat`。 |
| `GET` | `/chat` | 聊天编辑器 SPA。 |
| `GET` | `/trace` | 每任务 trace 视图 SPA。 |
| `GET` | `/assets/*` | 附带的 Web 资源（JS、CSS、图片）。 |
| `GET` | `/preview/*` | 单端口 HTML 应用预览网关（沙箱化 iframe）。 |
| `GET` | `/health` | 存活探针 → `{"status": "ok", "backend": "new"}`。 |

## 错误码

Engine 错误携带一个稳定的 `code` token，映射到 HTTP 状态：

| 错误码 | HTTP 状态 | 含义 |
| --- | --- | --- |
| `model_selector_rejected` | 400 | 每轮模型选择器被拒绝（不在允许列表中）。 |
| `provider_selector_rejected` | 400 | Provider 选择器被拒绝。 |
| `not_resumable` | 409 | 任务不在可恢复状态。 |
| `unsupported_subtask_suspend` | 409 | 子任务在此配置中无法挂起。 |
| `task_already_terminal` | 409 | 任务已达到终止状态。 |
| *(意外)* | 500 | 内部错误（从不泄露堆栈跟踪）。 |

## 来源

路由注册分布在 `apps/noeta-agent/noeta/agent/backend/` 下的这些模块中：

- `task_protocol.py` —— SSE 流 + 任务命令端点
- `resource_services.py` —— content / files / file（数据平面）
- `read_views.py` —— capabilities + 会话列表
- `mcp_service.py` —— MCP 连接器管理
- `workspace_service.py` —— 工作区（项目）管理
- `app.py` —— 路由根、静态资源、预览网关、`/health`

配置解析：`lifecycle.py` → `BackendConfig.from_env`。
另见：[配置](configuration.md)。
