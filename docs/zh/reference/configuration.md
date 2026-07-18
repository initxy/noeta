# 配置

平台（`python -m noeta.agent`）通过 **`apps/noeta-agent/.env`** 加环境变量配置 —— 环境变量优先于文件，文件优先于内置默认值。没有任何 CLI 参数。事实来源：`apps/noeta-agent/noeta/agent/config.py`（pydantic-settings；旧版 `.env` 中的未知键会被忽略）。`apps/noeta-agent/.env.example` 是带注释的起步副本。

**每个键都是可选的。** 全部留空时，服务器完全离线运行：确定性 mock LLM、dev-login、SQLite 存储、不开沙箱。

相对路径（`DATA_DIR`、`SHARED_DATA_DIR`、`MODELS_CONFIG`）以应用项目根 `apps/noeta-agent/` 为基准解析。

## 服务器

| 键 | 默认值 | 用途 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | 绑定地址。 |
| `PORT` | `8000` | 监听端口。 |
| `LOG_LEVEL` | `INFO` | 后端日志级别。 |
| `CORS_ORIGINS` | vite dev 的 origin | 逗号分隔的允许 origin 列表（仅当前端 dev server 单独启动时需要）。 |

## 路径与存储

| 键 | 默认值 | 用途 |
| --- | --- | --- |
| `DATA_DIR` | `data` | 可写数据根目录（见下）。 |
| `SHARED_DATA_DIR` | `data/shared` | 由后端写入、以**只读**方式挂载进沙箱的内容：知识、skill。在未来的多主机形态里，两侧挂载同一棵共享子树。 |

`DATA_DIR` 布局（启动时创建）：

```text
data/
├── app.db          # 应用 DB：用户、空间、会话、skill、
│                   # 模板、知识、MCP 连接器、反馈、…
├── noeta.db        # 引擎存储：EventLog + ContentStore + Dispatcher
├── workspaces/     # 每会话一个目录（沙箱模式下 bind-mount 到 /workspace；
│                   # 文件面板读取它）
├── memories/       # 每空间一个长期记忆池
└── shared/         # SHARED_DATA_DIR 的默认位置
    ├── knowledge/       # 物化后的知识源
    ├── builtin-skills/  # 管理员管理的平台 skill
    └── space-skills/    # 各空间上传的 skill
```

两个数据库都是 SQLite 文件；Postgres 是文档中写明的未来选项，v1 尚未接入（平台是单进程单实例）。

## LLM 网关

| 键 | 默认值 | 用途 |
| --- | --- | --- |
| `LLM_PROVIDER` | `auto` | `auto` \| `openai` \| `mock`。**`LLM_BASE_URL` 与 `LLM_API_KEY` 同时设置时，`auto` 解析为 `openai`；否则解析为离线 `mock`**（确定性 FakeLLM 演示脚本 —— 零凭证模式）。`openai` 而无凭证时启动即失败。 |
| `LLM_BASE_URL` | *(空)* | 主网关根地址 —— 任意 **OpenAI-Responses 兼容**端点；provider 会追加 `/responses`。认证使用 `api-key` header。 |
| `LLM_API_KEY` | *(空)* | 主网关凭证。 |
| `SECONDARY_LLM_BASE_URL` | *(空)* | 可选的第二网关（同样的 Responses 协议，`Authorization: Bearer` 认证）。 |
| `SECONDARY_LLM_API_KEY` | *(空)* | 第二网关的凭证。二者都设置才算配置完成；第二网关只在主网关生效时**叠加其上** —— 从不单独存在。 |
| `MODELS_CONFIG` | `models.json` | 模型菜单文件的路径（见下）。 |
| `LLM_REQUEST_TIMEOUT` | `300.0` | 单次请求超时（秒）。 |
| `LLM_MAX_TOKENS` | `8192` | 输出 token 上限。 |
| `TITLE_MODEL` | `gpt-5.4-2026-03-05` | 异步生成会话标题所用的模型（推理关闭；mock provider 下不生成标题）。 |

### `models.json`

定义用户挑选模型的菜单（`GET /api/v1/models`）。每个条目包含：`id`、`label`、`default`（仅一个条目设置）、`efforts`（推理力度档位）、`default_effort`，外加仅后端使用的字段：`gateway`（`"openai"` = 主网关，`"secondary"` = 经 `RoutingProvider` 路由到第二网关）以及 `context_window` / `max_output_tokens`（为 SDK 目录不认识的模型登记规格，让上下文 compaction 正常工作）。文件缺失或解析失败时降级为单个回退模型并给出警告 —— 后端绝不会因为模型配置而崩溃。

## 认证与管理员

| 键 | 默认值 | 用途 |
| --- | --- | --- |
| `DEV_LOGIN_ENABLED` | `true` | dev-login 参考 provider：任意用户名、签名 cookie。同时也是一个**动态配置**键 —— 可在管理员控制台热切换（DB 覆盖优先于这里的静态值）。 |
| `SESSION_SECRET` | 开发占位值 | 为会话 cookie 签名。**任何真实部署都必须改掉它。** |
| `SESSION_COOKIE_NAME` | `noeta_session` | Cookie 名。 |
| `SESSION_COOKIE_SECURE` | `false` | 部署在 HTTPS 之后时设为 `true`。 |
| `ADMIN_USERS` | *(空)* | 逗号分隔的用户名列表，这些用户获得 `is_admin` 与管理员控制台。留空 = 无人是管理员；管理员端点对所有人返回 404。dev-login 下任何人都能以白名单里的名字登录 —— 真实部署应在 `AuthProvider` 缝（`noeta/agent/auth/provider.py`）接入身份提供方。 |

## 沙箱

每个会话一个 Docker 容器；标准 fs/shell 工具的副作用经由 ExecEnv 缝路由进容器。关闭时 = **纯对话模式**：没有容器、shell 执行关闭、没有文件面。

| 键 | 默认值 | 用途 |
| --- | --- | --- |
| `SANDBOX_ENABLED` | `false` | 总开关。需要本地 Docker daemon。 |
| `SANDBOX_IMAGE` | `ghcr.io/agent-infra/sandbox:latest` | 现成的 AIO Sandbox 镜像；需要更多沙箱内工具时，可在它之上构建自己的镜像。 |
| `SANDBOX_MEMORY` | `2g` | 单容器内存上限。 |
| `SANDBOX_CPUS` | `2` | 单容器 CPU 上限。 |
| `SANDBOX_API_KEY_ENV` | `SANDBOX_API_KEY` | 存放容器 API key 的环境变量的**名字** —— 供给容器时读取，注入容器与 ExecEnv 认证，从不记录。该变量未设置 = 容器不带认证运行（仅限本地开发）。 |
| `SANDBOX_PREVIEW_PORT` | `0` | 实时 Browser/Terminal/Code 面板专用的反向代理端口。它被有意设计成与主端口**不同的 origin**（面板 iframe 以 `allow-same-origin` 运行；容器内容绝不能与 cookie/API 的 origin 同源）。`0` = 临时端口（经 `GET /sessions/{id}/preview` 发现）；防火墙需要固定端口时可将其钉死。 |
| `SANDBOX_IDLE_STOP_HOURS` | `1.0` | 空闲第 1 级：`docker stop` —— 内存 / CPU 归还宿主机；容器及其磁盘保留，恢复会话时几秒内即可重新挂上。 |
| `SANDBOX_IDLE_REMOVE_HOURS` | `24.0` | 空闲第 2 级：`docker rm` —— 磁盘也一并回收；此后只能开全新会话。这个值应远长于 stop。`0`/负数禁用对应级别；两级都禁用 = 没有回收器。 |
| `SANDBOX_IDLE_CHECK_INTERVAL_HOURS` | `0.1` | 回收器的轮询间隔（下限一分钟）。 |

## Agent 工具开关

agent 工具面的全局开关（按空间的开关落地之前的临时方案）；全部默认**关闭**：

| 键 | 默认值 | 用途 |
| --- | --- | --- |
| `MEMORY_TOOLS_ENABLED` | `false` | `memory_write/read/search/archive` + 自动召回 + 记忆整理。 |
| `COLLAB_TOOLS_ENABLED` | `false` | 频道 / 看板预览面背后的协作工具（`channel_read_*`、`board_*`）。 |
| `SUBAGENT_ENABLED` | `false` | `spawn_subagent` 委派（explorer / web specialist）。 |

## 记忆整理

仅在 `MEMORY_TOOLS_ENABLED` 打开时生效。

| 键 | 默认值 | 用途 |
| --- | --- | --- |
| `MEMORY_CONSOLIDATION` | `true` | 轮次边界上的后台整理，按空间防抖；整理 agent 只拿到记忆工具面，只能归档，不能删除。 |
| `MEMORY_CONSOLIDATION_DEBOUNCE_HOURS` | `24.0` | 两次整理之间的最小间隔小时数（标记文件放在空间的记忆目录里）。 |

## 可观测性

| 键 | 默认值 | 用途 |
| --- | --- | --- |
| `OTLP_ENDPOINT` | *(空)* | OTLP trace 导出：**完整的** OTLP/HTTP traces URL（例如 `http://localhost:4318/v1/traces`）。留空 = 关闭。导出**只能通过这个键显式开启** —— 环境中 OTel 标准的 `OTEL_EXPORTER_OTLP_ENDPOINT` 被有意**排除**在开启开关之外（运维者为别的应用注入它时，noeta 不能悄悄开始导出）。 |
| `OTLP_HEADERS` | *(空)* | 附加到每个导出请求上的额外 header（用于托管 collector 的认证），OTel 形式 `k=v,k2=v2`，值需百分号编码。未设置时回退到环境中的 `OTEL_EXPORTER_OTLP_HEADERS`。header **只在** `OTLP_ENDPOINT` 已设置时生效 —— 它们自身从不开启任何功能。 |

## Worker 池

| 键 | 默认值 | 用途 |
| --- | --- | --- |
| `AGENT_NUM_WORKERS` | `4` | 内嵌 noeta Client 里的常驻 `WorkerLoop` 线程数：N 个 worker 并发驱动不同会话的轮次（同一会话内的轮次由 dispatcher lease 保持串行）。设为 `1` 退化为单 worker。 |

## 动态配置

一小组白名单设置可在运行时通过管理员控制台热更新（`GET/PUT /api/v1/admin/config`）：DB 覆盖优先于静态 `.env` 值，且只有每次使用都会重新读取的设置才有资格进入。目前已注册：`dev_login_enabled`。来源：`apps/noeta-agent/noeta/agent/config_registry.py`。
