# HTTP API 参考（noeta-agent 平台）

`python -m noeta.agent` 对外提供的带版本 REST + SSE 接口。下文所有路由都带 **`/api/v1`** 前缀（表中省略）。所有请求 / 响应体均为 JSON。事实来源：`apps/noeta-agent/noeta/agent/api/` 下的各个 router，由 `noeta/agent/main.py` 装配。

约定：

- **认证** —— 每个端点都要求登录时设置的签名会话 cookie（`noeta_session`），例外只有三个公开端点：`GET /health`、`GET /auth/config`，以及 `POST /auth/dev-login` 本身。
- **可见性 = 成员身份。** 你无权看到的会话（session）、空间（space）资源或频道返回 **404**（隐藏其存在），而不是 403。403 只留给「你看得见但不能这么做」的情形（例如成员 vs 所有者）。
- **命令端点以 202 确认**，响应体很小；每个可见变化都经会话的 SSE 流到达。
- **凭证绝不回传。** 连接器的 header/env 值和网关 key 只存在服务端，并从每个响应中剔除。

## 认证

| 方法与路径 | 用途 |
| --- | --- |
| `GET /auth/config` | 公开的登录页配置：`dev_login_enabled` + provider 贡献的字段（`AuthProvider` 缝）。 |
| `POST /auth/dev-login` | 请求体 `{username}`。设置签名的 `noeta_session` cookie 并 upsert 用户。dev-login 被禁用时（动态配置）返回 403。 |
| `GET /auth/me` | 当前用户：`username`、`email`、`name`、`avatar`、`is_admin`。 |
| `POST /auth/logout` | 清除会话 cookie。 |

## 杂项

| 方法与路径 | 用途 |
| --- | --- |
| `GET /health` | `{"ok": true, "provider": "mock"\|"openai"}` —— 无需认证。 |
| `GET /models` | 来自 `models.json` 的模型菜单（`id`、`label`、`default`、`efforts`、`default_effort`）+ 实际生效的 provider。 |
| `GET /capabilities` | agent 能力开关的快照（memory / delegation / mcp / …）。 |
| `GET /content/{hash}` | 按 SHA-256 哈希（64 位十六进制字符）读取 ContentStore 原始字节；哈希即凭据（capability）—— 只能请求你见过的哈希。媒体类型从 magic bytes 嗅探（PNG/JPEG/GIF/WebP/PDF），否则为 `application/octet-stream`。用于把 composer 图片附件渲染回来，也供管理员 trace 视图使用。 |

## 会话

前缀 `/sessions`。会话隶属于一个空间；可见性 = 空间成员身份。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /sessions?space_id=` | 列出该空间的会话。 |
| `POST /sessions` | `201`。请求体 `{space_id, model?, template_id?, workflow_template_id?, params?}`。`template_id` 从 prompt 模板启动会话；`workflow_template_id` 启动多节点工作流会话（二者互斥）。模型默认取空间 agent-config 的默认值，其次取平台默认值。 |
| `GET /sessions/{id}` | 会话详情；工作流会话携带 `workflow` 视图（节点标签栏）。 |
| `DELETE /sessions/{id}` | 删除（仅限创建者或空间所有者；成员得到 403）。 |
| `POST /sessions/{id}/messages` | `202`。请求体 `{content?, model?, effort?, task_id?, images?}` —— 文本和 / 或 composer 图片附件（见下）。轮次运行中或有提问待回答时返回 409；未知模型或不支持的 effort 返回 422。 |
| `POST /sessions/{id}/answer` | `202`。请求体 `{question_id, answers, task_id?}`，回答一个结构化提问。每个答案值是对象 `{choice_id?, text?}`（至少填一项；仅当提问允许时才能用自由文本 `text`）。没有待回答的提问时返回 409。 |
| `POST /sessions/{id}/cancel` | 停止正在运行的轮次（`task_id` 可选，指定工作流节点）。 |
| `POST /sessions/{id}/advance/preview` | 工作流会话：生成进入下一节点的交接内容（预填参数 + 交接摘要 + 完整交接文档）。幂等；没有下一节点或上一节点仍在运行时返回 409。 |
| `POST /sessions/{id}/advance/confirm` | `202`。请求体 `{node_index, params, summary?, handoff_doc?}` —— 启动下一节点；交接文档保存在会话工作区的 `handoff/` 目录下。 |
| `GET /sessions/{id}/events` | 每会话一条的 SSE 流（见下）。查询参数：`since_seq?`、`task_id?`（工作流节点过滤）。 |
| `GET /sessions/{id}/files` | 会话工作区的文件列表（`{path, size, mtime}`）。沙箱关闭时为空（纯对话模式没有文件面）。 |
| `GET /sessions/{id}/files/content?path=` | 单个工作区文件（UTF-8，上限 200 KB，带 `truncated` 标记）。路径逃出工作区时返回 400。 |
| `GET /sessions/{id}/preview` | 沙箱实时预览发现接口：`{token, port, panels}`，供 Browser / Terminal / Code iframe 使用，由**独立的**预览 origin（`http://<host>:<port>/sandbox-preview/<token>/…`）提供。会话没有容器时返回 404。 |

### Composer 图片附件

`POST /sessions/{id}/messages` 可以携带 `images: [{media_type, data_base64}]`。约束（违反即 **400**，轮次不会被启动）：MIME 白名单为 PNG / JPEG / GIF / WebP；base64 必须有效；每张图片 ≤ 5 MB。字节写入内容寻址存储，并作为 `ImageBlock` 挂在用户轮次上；UI 事件只暴露 `{hash, media_type}`，前端经 `GET /content/{hash}` 取回并渲染 —— 图片字节从不走事件流。

### SSE 流与 `since_seq`

每个会话一条流。帧遵循 SSE 格式：持久事件携带 `id: <seq>`（根任务 EventLog 中的 envelope 序号）；合成帧不带 id。来源：`apps/noeta-agent/noeta/agent/host/translator.py` —— 一个从引擎 `EventEnvelope` 到 UI 事件的确定性纯函数，replay 与实时共用，两条路径因此不可能出现分歧。

**Replay 即重新推导。** 连接建立时，后端把会话的 EventLog 重新过一遍 translator，跳过 `seq <= since_seq` 的事件，然后发出合成的 `replay_done` 并切换到实时帧（replay 与实时的重叠部分按 seq 去重）。不存在任何存储下来的 UI 投影；EventLog 是唯一的持久事实。重连时把最后看到的 `id` 作为 `since_seq` 传入即可。

持久事件词汇表（经过翻译、携带 seq）：

| 事件 | 数据 | 含义 |
| --- | --- | --- |
| `user_message` | `{content, images?}` | 一个用户轮次（宿主注入的消息已被过滤掉）。 |
| `assistant_text` | `{text}` | 助手正文文本（从不截断）。 |
| `thinking` | `{text}` | 推理摘要（截断到 2000 字符）。 |
| `tool_call` | `{call_id, tool_name, arguments, subtask_id?}` | 工具执行开始。 |
| `tool_result` | `{call_id, success, summary, output, subtask_id?}` | 工具执行结束（output 截断到 2000 字符）。 |
| `memory_op` | `{call_id, op, name}` | 一次记忆工具调用，折叠成语义标记（`write`/`read`/`search`/`archive`）。 |
| `skill_activated` | `{skill}` | 模型激活了一个 skill。 |
| `todo_update` | `{todos: [{id, content, status}]}` | todo 列表被整体替换。 |
| `subtask_started` / `subtask_finished` | `{subtask_id, agent_name?, goal?, status, summary}` | subagent 委派的生命周期。 |
| `question` | `{question_id, reason?, questions}` | 一个结构化提问；会话等待 `POST …/answer`。 |
| `question_answered` | `{question_id}` | 答案已被记录。 |
| `compaction` | `{replaced_count}` | 较早的历史被压缩进一份摘要。 |
| `llm_retry` | `{call_id}` | 一次瞬时 LLM 失败正在重试（客户端会清空该调用的 delta 缓冲）。 |
| `turn_started` / `turn_finished` | `{}` / `{status}` | 轮次边界；`status` ∈ `awaiting_input` / `completed` / `failed` / `cancelled`。 |
| `error` | `{message}` | 失败轮次的错误信息（与 `turn_finished` 成对出现）。 |

合成帧（不带 id、从不参与 replay —— 例外是 `replay_done`，每次 replay 都以它收尾）：

- `delta` —— `{call_id, kind: "text"|"thinking", text, index}`：LLM 调用进行期间的临时 token 流式预览。从不持久化、从不 replay；持久记录永远是随后追加的消息事件。
- `replay_done` —— replay 结束标记。
- `session_meta` —— `{title}`：异步生成的会话标题。
- `workflow_update` —— 工作流视图发生了变化（节点启动 / 结束）。
- 子任务流上的 `tool_call` / `tool_result` / `subtask_finished` 帧同样是合成帧（子任务的 seq 独立于根流计数；replay 只读根流）。

原始、未经翻译的 envelope **不在**这个接口上 —— 它们位于管理员 trace 端点（见下）。

## 空间

| 方法与路径 | 用途 |
| --- | --- |
| `GET /spaces` | 你所属的空间。 |
| `POST /spaces` | `201`。创建团队空间（你成为所有者）。 |
| `GET /spaces/{id}` | 空间详情（仅限成员；否则 404）。 |
| `PATCH /spaces/{id}` | 重命名 / 编辑（所有者）。个人空间：400。 |
| `DELETE /spaces/{id}` | 删除（所有者）。个人空间：400。 |
| `POST /spaces/{id}/members` | `201`。添加成员（所有者）。 |
| `PATCH /spaces/{id}/members/{member}` | 修改成员角色（`owner` / `member`）；最后一名所有者不能被降级。 |
| `DELETE /spaces/{id}/members/{member}` | 移除成员（所有者；最后一名所有者不能被移除）。 |
| `GET /users/search?q=` | 按用户名搜索，供成员选择器使用。 |
| `GET /spaces/{id}/agent-config` | 空间的 agent 配置（成员可读）：`prompt`（人设，会写入会话工作区的 `AGENT.md`）、`memory_enabled`、`knowledge_sources`（null = 全部）、`default_model`、`default_effort`。 |
| `PUT /spaces/{id}/agent-config` | 更新它（所有者）。 |

## Skill

两个层级，同一种 `SKILL.md` 格式：

**内置 skill** —— 平台级，仅限管理员控制台（前缀 `/skills`，全部受管理员白名单门控；非管理员得到 404）：

| 方法与路径 | 用途 |
| --- | --- |
| `GET /skills` | 列出内置 skill（含启用标记）。 |
| `POST /skills` | 上传（zip 或单个 `SKILL.md`）；frontmatter 的 `name` 决定目录名；重新上传 = 重装。 |
| `PATCH /skills/{name}` | 平台级启用 / 禁用。 |
| `DELETE /skills/{name}` | 删除 skill 及其目录。 |
| `GET /skills/{name}/preview` | 只读内容预览。 |

**空间 skill** —— 按空间（前缀 `/spaces/{space_id}/skills`；成员可读，所有者可写）：

| 方法与路径 | 用途 |
| --- | --- |
| `GET /spaces/{id}/skills` | 列出空间的 skill。 |
| `POST /spaces/{id}/skills` | `201`。上传 skill 到空间。 |
| `PATCH /spaces/{id}/skills/{name}` | 在本空间启用 / 禁用。 |
| `PUT /spaces/{id}/skills/{name}/group` | 设置 skill 的展示分组。 |
| `DELETE /spaces/{id}/skills/{name}` | 删除。 |
| `GET /spaces/{id}/skills/{name}/preview` | 只读预览。 |

## 知识源

前缀 `/spaces/{space_id}/knowledge`。成员可读；所有者负责管理。源类型：`git_repo`（clone URL + 可选 token）与 `local_dir`（托管目录）。

| 方法与路径 | 用途 |
| --- | --- |
| `GET …` | 列出空间的知识源（含同步状态）。 |
| `POST …` | `201`。添加知识源。 |
| `PATCH …/{source_id}` | 编辑配置。 |
| `DELETE …/{source_id}` | 删除知识源及其物化副本。 |
| `POST …/{source_id}/sync` | `202`。触发一次同步（异步；状态经 GET 查询）。 |
| `GET …/{source_id}/sync` | 同步状态 / 最近一次错误。 |
| `POST …/resolve-paths` | 把引用脚注中的路径解析回源位置。 |

## MCP 连接器

前缀 `/spaces/{space_id}/mcp`。成员可读；所有者负责管理。传输方式为 `http`（`url` + `headers`）或 `stdio`（`command` + `args` + `env`）。发现（discovery）仅限 HTTP —— stdio 连接器的菜单接口一律返回 400（服务器不会因为管理面的一个 GET 就拉起运维者配置的子进程）；连接 / 握手失败映射为 502。启用的连接器**每轮**都会被解析进 agent 宿主；其工具以 `mcp__<alias>__<tool>` 的名字出现。

| 方法与路径 | 用途 |
| --- | --- |
| `GET …/servers` | 列出连接器（凭证已剔除）。 |
| `POST …/servers` | `201`。创建 / 整体替换连接器。 |
| `PUT …/servers/{alias}` | 合并式编辑（未提供的字段保持不变）。 |
| `PATCH …/servers/{alias}` | 启用 / 禁用。 |
| `DELETE …/servers/{alias}` | 删除。 |
| `GET …/servers/{alias}/tools` | 连接器的完整工具菜单。 |
| `PUT …/servers/{alias}/tools` | 设置启用的工具子集（`null` = 全部）。 |
| `GET …/servers/{alias}/prompts` | 连接器的 prompt。 |
| `GET …/servers/{alias}/resources` | 连接器的静态资源。 |

## 模板

前缀 `/spaces/{space_id}`。成员可读可用；所有者负责管理。结构性错误返回 422，名称冲突返回 409；占位符一致性警告随 `warnings` 一并返回，不阻塞操作。

| 方法与路径 | 用途 |
| --- | --- |
| `GET …/templates` · `POST` · `PATCH /{id}` · `DELETE /{id}` | 单节点 prompt 模板（prompt + 带类型的参数）。 |
| `GET …/workflow-templates` · `POST` · `PATCH /{id}` · `DELETE /{id}` | 多节点工作流定义（有序的模板引用）。删除被工作流引用的模板：409。 |

## 记忆

前缀 `/spaces/{space_id}/memories` —— 空间的长期 agent 记忆池（每条记忆一个 markdown 文件）。成员可读**也**可编辑 / 归档（他们的会话本来就在写记忆）；物理删除仅限所有者。

| 方法与路径 | 用途 |
| --- | --- |
| `GET …` | 列出记忆（名称、类型、摘要）。 |
| `GET …/{name}` | 全文。 |
| `PUT …/{name}` | 创建 / 更新。 |
| `POST …/{name}/archive` | 退役进 `archive/`（常规路径；可追溯）。 |
| `DELETE …/{name}` | 硬删除（仅限所有者）。 |

## 反馈

成员层面收集，所有者把关执行：

| 方法与路径 | 用途 |
| --- | --- |
| `POST /sessions/{id}/feedback` · `GET` | 为一条消息评分（赞 / 踩 + 评论）/ 列出会话的反馈。 |
| `GET /spaces/{id}/feedback` | 空间的反馈列表。 |
| `PUT/GET /spaces/{id}/feedback/{fid}/reference` | 附加 / 读取修正后的参考产物。 |
| `POST /spaces/{id}/feedback/analyze` | `202`。所有者：对收集到的反馈运行分析 agent。 |
| `GET /spaces/{id}/feedback/runs/latest` | 最近一次分析运行的状态。 |
| `GET /spaces/{id}/feedback/suggestions` | 建议列表。 |
| `POST …/suggestions/{sid}/adopt` · `/dismiss` | 所有者：采纳（写入空间记忆，或在备份后应用 skill 补丁）或驳回。 |
| `GET …/suggestions/{sid}/skill-diff` | 预览某条建议的 skill 补丁。 |
| `POST /spaces/{id}/feedback/report` · `GET …/reports` · `POST …/reports/{rid}/publish` | 把选中的建议聚合成一份 **markdown 报告**、列出报告、发布。 |

## 频道与看板（协作预览）

团队空间的频道（`GET/POST /spaces/{id}/channels`、消息、话题、`GET /channels/{id}/stream` SSE、未读水位线）和一个三列任务看板（`GET /spaces/{id}/board`、卡片 CRUD、卡片 → 会话）。个人空间返回 422。这一层是**预览面**：让它真正有用的 agent 侧协作工具（`channel_read_*`、`board_*`）**默认被特性开关关闭**（`COLLAB_TOOLS_ENABLED=false`）；是否打开协作层是一个部署决策。

## 管理员

前缀 `/admin`；每条路由都要求 `ADMIN_USERS` 白名单 —— 非管理员得到 404。除动态配置写入与内置 skill 接口外全部只读。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /admin/stats` | 平台计数：用户、空间、按状态统计的会话、知识源、skill。 |
| `GET /admin/users` · `/sessions` · `/spaces` | 跨空间列表。 |
| `GET /admin/spaces/{id}/members` · `/knowledge` · `/skills` | 逐空间下钻。 |
| `GET /admin/sessions/{id}/raw-events` | **原始 trace 面**：根任务及其完整子任务树的未翻译 `EventEnvelope`。游标 = 上一个响应回显的 `{task_id: last_seq}` JSON（每个任务流的 seq 独立计数）。这是原始 envelope 唯一会经过网络的地方。 |
| `GET /admin/config` · `PUT /admin/config/{key}` | 动态配置：已注册的可热更新键（如 `dev_login_enabled`），DB 覆盖优先于静态设置。 |

## 另见

- [平台参考](noeta-agent.md) —— 架构、启动模式、管理员控制台
- [配置](configuration.md) —— 每一个 `.env` 键
- [SDK 参考](sdk.md) —— 底下的进程内库接口
