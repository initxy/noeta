# SDK 参考（`noeta.sdk`）

`noeta.sdk` 是 SDK 唯一的公开导入面。以下所有内容都从它重新导出——用户永远不直接导入 `noeta.client` 或运行时内部。事实来源：`packages/noeta-sdk/noeta/sdk/__init__.py` 中的 `__all__` 列表。

```python
from noeta.sdk import query, Client, Options, tool
```

## Client 动词

### `query(options, goal, *, provider=None, workspace_dir=None, model=None, images=(), plugins=None, host_config=None) → QueryResult`

一次性查询：驱动单轮到真正的终止状态，并返回带有预 fold 投影的完整信封流（`client/client.py`）。创建一个临时的 `Client(multi_turn=False)`，并在返回前将其关闭。多轮工作请直接使用 `Client`。参数与 `Client` 构造函数一致，所以这条语法糖路径并不局限于内存存储——`host_config` 可以选用持久化存储和任意其他主机接线。

### `QueryResult` — `client/client.py`

`list[EventEnvelope]` 的子类（迭代 / 索引行为类似列表），外加：

| 成员 | 返回值 | 备注 |
| --- | --- | --- |
| `.task_id` | `str` | 被驱动的任务 |
| `.messages()` | `list[ViewItem]` | 预 fold 的人类视图；每个 `ContentRef` 已解引用 |
| `.answer()` | `Any` | 终止答案；任务失败或非终止时**抛出 `QueryFailedError`** |

投影在拆除之前针对临时 Client 的 ContentStore 物化——不要用新的存储重新投影原始信封。

### `Client` — `client/client.py`

```python
Client(options, *, provider=None, workspace_dir=None, model=None,
       multi_turn=True, host_config=None, allowed_models=None, plugins=None)
```

provider 必须来自 `provider` 关键字参数或 `Options.provider`，否则抛出 `ValueError`。工作区按 `workspace_dir` > `Options.cwd` > `Path.cwd()` 的顺序解析。存储默认为内存中；传入 `HostConfig` 以注入持久化后端。`allowed_models` 是每轮模型选择器的白名单：`None` 回落到 `DEFAULT_MODEL_ALLOWLIST`（`opus` / `sonnet` / `haiku`），而显式的**空**序列则不授权任何选择器（`model_selector=None` 仍绑定主机默认值）。`plugins` 是一个已加载的 `PluginSet`（见[插件机制](#插件机制)）：它的身份面贡献只有在 `Options.plugins` 激活了该插件的地方才会到达某个 agent，而它的 guard 与 observer 则进程级生效。加载集里不存在的激活名会导致构建失败。

`Client` 是一个上下文管理器（`with Client(...) as client:`），因此 `shutdown` 不会被遗忘。

**驱动轮次的动词。** 每个动词都在调用线程上跑完整轮并返回一个 `DriveOutcome`。当配置了 `Options.can_use_tool` 时，它们都会经它排空，所以无论是哪个动词恢复了对话，被门控的工具调用都会被自动解决。

| 方法 | 签名（`task_id` 之后为关键字参数） |
| --- | --- |
| `start` | `(*, goal, agent=None, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), workspace_dir=None, effort=None, activations=())` |
| `send_goal` | `(task_id, *, goal, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), effort=None, activations=())` |
| `approve` | `(task_id, *, call_id, reason=None, resolver="client")` |
| `deny` | `(task_id, *, call_id, reason=None, resolver="client")` |
| `answer` | `(task_id, *, question_id, answers, answered_by="client")` |
| `deliver_event` | `(task_id, *, event_kind, payload=None)` — 唤醒 `wait_external` 挂起；按 `event_kind` 精确匹配，可选 `payload` 作为 `origin="system"` 消息记录在恢复轮上 |

`start` 时的 `workspace_dir` 被一次性焊入持久化的 `TaskHostBound`；后续轮次靠 fold 解析它。`permission_mode` / `enabled_mcp` / `effort` / `activations` 是每轮的、非持久化的主机旋钮。`activations` 在进入循环前钉住内置 skill——这正是 `/skill-name` 斜杠命令所依附的通道。

**Seed / drive 拆分**（异步传输）。`seed_*` 在请求线程上完成每一个持久化、已校验的步骤——因此类型化的拒绝（`ModelSelectorError`、`NotResumableError`）仍然作为同步的 4xx 浮现——并返回一个 `SeededTurn`，交给后台线程上的 `drive_seeded`。

| 方法 | 签名 |
| --- | --- |
| `seed_start` | 同 `start` |
| `seed_send_goal` | 同 `send_goal` |
| `seed_approve` / `seed_deny` | 同 `approve` / `deny` |
| `seed_answer` | 同 `answer` |
| `seed_deliver_event` | 同 `deliver_event` |
| `drive_seeded` | `(seeded)` — 把 seeded 轮跑到它的下一个边界 |

**常驻 Worker 池。** 当 Worker 在运行时，后台驱动路径会把 seed 的租约交还给就绪队列，而不是派生一次性线程，从而在多个对话之间实现真正的并发。

| 方法 | 签名 |
| --- | --- |
| `start_workers` | `(num_workers=1, *, poll_interval=0.1, heartbeat_interval=30.0, stale_sweep_interval=10.0, timer_poll_interval=1.0, lease_seconds=600.0, shutdown_grace_s=10.0)`；调用两次抛出 `RuntimeError` |
| `stop_workers` | `(timeout=None)` → `bool` — 当某个 Worker 未能及时退出时返回 `False`，且该池保持被跟踪，以便重试完成收尾 |

**对话生命周期。**

| 方法 | 签名 |
| --- | --- |
| `cancel` | `(task_id, *, reason="cancelled", cascade=False)` — 杀掉对话 |
| `interrupt` | `(task_id, *, reason=None, interrupted_by="user")` — 在下一个边界停下进行中的轮次，让任务停留在其 next-goal 挂起上，于是 `send_goal` 直接续上；对正在被驱动的轮次线程安全 |
| `close` | `(task_id, *, closed_by="user", reason=None)` — 归档它 |
| `reopen` | `(task_id, *, reopened_by="user", reason=None)` |
| `rewind` | `(task_id, *, message_seq)` — 重新基线到 `message_seq` 处用户消息之前：该消息、它的输出以及之后的每一轮都成为死历史（append-only 保持不变），被撤销区间编辑过的工作区文件被恢复 |
| `fork` | `(task_id, *, message_seq)` — 相同锚点，相反的保留策略：铸造一个**新**任务，继承到该边界为止的历史，源任务保持不动。返回的 `DriveOutcome.task_id` 是这个 fork 的。仅限根任务；两条分支共享同一个工作区 |

**审视与存储。**

| 方法 | 签名 |
| --- | --- |
| `events` | `(task_id)` → `list[EventEnvelope]` |
| `messages` | `(task_id)` → `list[ViewItem]` |
| `events_after` | `(task_id, after_seq=None)` → 严格在游标之后的流 |
| `task_streams` | `()` → 每任务 `(task_id, last_seq)` 摘要 |
| `delete_task` | `(task_id)` → `{"ok", "reason"?, "task_id", "deleted": [...]}`；以 `reason="running"` / `"not_found"` 拒绝 |
| `get_content` | `(content_hash)` → `bytes \| None` |
| `put_content` | `(body, *, media_type)` → `ContentRef` |
| `memory_root` | `(task_id=None)` → `Path` — 该任务在多租户链下解析到的存储 |
| `subscribe` | `(callback)` → 取消订阅可调用对象；提交后信封，所有任务 |
| `add_sandbox_lifecycle_listener` | `(on_allocate, on_release)` — 用于容器跟踪副作用的产品接线；无沙箱时为 no-op |
| `shutdown` | `()` — 幂等：停止 Worker，拆除 observer 与 trace sink，释放沙箱 |

属性：`registry`（编译后的 `AgentRegistry`）、`main_agent_name`、`workers_running`。

## 配方：`Options`

### `Options` — `client/options.py`

编译为 `AgentSpec` 的冻结数据类。字段分为**身份**（进入记录）和**接线**（仅挂载点，被 `compile_options` 忽略，且不计入 `Options` 相等性）：

| 字段 | 类型 / 默认值 | 类别 |
| --- | --- | --- |
| `system_prompt` | `str \| SystemPromptPreset` — 必填 | 身份 |
| `name` | `str = "main"` | 身份 |
| `skills` | `tuple[str, ...] = ()` | 身份 |
| `budget` | `BudgetSpec \| None` — `None` ⇒ 默认 `max_subtask_depth=3` | 身份 |
| `plugins` | `tuple[str, ...] = DEFAULT_PLUGINS`（`("fs", "web")`）——按 agent 的插件**激活**：内置特性 bundle 名（`memory` / `skill_invocation` / `browser` / `todo_write` / `ask_user_question` / `mcp` / `delegation`），以及交给 `Client` 的 `PluginSet` 中已加载插件的名字 | 身份 |
| `agents` | `Mapping[str, AgentDefinition] = {}` — 扁平、非递归 | 身份 |
| `allowed_tools` | `tuple \| None` — `None` ⇒ **全部 11 个内置**；条目是名称字符串或暴露 `.ref` 的对象 | 身份 |
| `disallowed_tools` | `tuple[str, ...] = ()` — 从白名单中减去 | 身份 |
| `permission_mode` | `"default"` \| `"acceptEdits"` \| `"bypassPermissions"` | 身份 |
| `max_turns` | `int \| None` — `budget.max_iterations` 的语法糖；同时设置两者抛出 `ValueError` | 身份 |
| `policy` | 可调用 `(llm) → Policy`，带 `.ref` — `None` ⇒ 内置 ReAct | 身份 |
| `mcp_servers` | `tuple[SdkMcpServer, ...] = ()` — 它们的工具进入身份 | 身份 |
| `model` | `str \| None` — 路由提示 | 排除在身份之外 |
| `metadata` | `Mapping[str, str] = {}` — 观察性标签 | 排除在身份之外 |
| `provider` | `LLMProvider \| None` | 接线 |
| `cwd` | `str \| Path \| None` | 接线 |
| `can_use_tool` | `(tool_name, arguments) → bool` — 自动解决门控调用；以 `resolver="can_use_tool"` 记录 | 接线 |
| `output_schema` | `Mapping \| None` — 最终答案的 JSON Schema | 接线 |
| `thinking` | `"adaptive"` \| `"disabled"` \| `None` | 接线 |
| `effort` | `"low"` \| `"medium"` \| `"high"` \| `"xhigh"` \| `"max"` \| `None` | 接线 |
| `guards` | `tuple[Guard, ...] = ()` | 接线 |
| `observers` | `tuple[Observer, ...] = ()` | 接线 |
| `content_channels` | `tuple[ContentKindSpec, ...] = ()` — 唯一的组合器 seam | 接线 |

无效的 `thinking` / `effort` 值在构造时抛出 `ValueError`；无效的 `permission_mode` 在编译时抛出。

### `AgentDefinition` — `client/options.py`

扁平的子 agent 配方：`description`（必填，非空）、`prompt`（必填）、`tools`（`None` ⇒ 全部内置）、`model`、`plugins`（按 agent 的激活，默认 `()`——没有 `fs`/`web`；`plugins=("delegation",)` 是给子 agent 授予 spawn 权限的方式）、`metadata`。不能嵌套——子项是叶子；深树在顶层扁平声明，由编译后的 `AgentSpec.spawnable` 接线。

### `SystemPromptPreset` — `client/options.py`

`preset: str = "main"`，`append: str | None = None` — 解析已注册的预设提示，可选追加后缀。

### `compile_options(options, *, plugins=None, preset_prompts=None) → (AgentSpec, tuple[AgentSpec, ...])`

将配方纯编译为 `(main_spec, descendant_specs)`——引用透明，因此相等的 `Options` 产生相等的 `AgentSpec`。`plugins` 是一个 `Mapping[str, PluginActivation]`；`Client` 从 `PluginSet` 构建它。

### `register_preset_prompt(name, prompt) → None`

为 `SystemPromptPreset` 注册一个命名预设（后写者胜出）。

### `BudgetSpec` — `noeta/agent/spec.py`

`Options.budget` 携带的上限：`max_iterations`、`max_tool_calls`、`max_cost_usd`、`max_spawned_subtasks`、`max_subtask_depth`。

## 创作

### `@tool` — `noeta/tools/decorator.py`

```python
from noeta.sdk import tool

@tool(name="word_count", version="1", risk_level="low",
      input_schema={"type": "object", "properties": {}}, description="...")
def word_count(arguments, ctx): ...
```

将 `fn(arguments, ctx) → ToolResult` 包装为 `DecoratedTool`。`name` 和 `input_schema` 是必填关键字；`version` 同样是**必填的**——省略它抛出 `TypeError`，因为 version 供给身份指纹。`risk_level` 默认为 `"low"`。`input_schema` 是面向 LLM 的元数据（不在运行时验证）；`description` 是模型工具语义的唯一来源。也可以直接调用：`tool(fn, name=..., version=..., input_schema=...)`。

### `create_sdk_mcp_server(name, version="1.0.0", tools=()) → SdkMcpServer`

将 `@tool` 函数打包为一个进程内（`"sdk"` 传输）MCP server，用于 `Options.mcp_servers`（`client/mcp_server.py`，经 `sdk/authoring.py` 重新导出）。空 `name` 抛出 `ValueError`；非 `DecoratedTool` 条目抛出 `TypeError`。`SdkMcpServer` 是冻结的：`name`、`version`、`tools`。它的工具保留其裸的 `@tool` 名字——`mcp__{alias}__{tool}` 前缀只适用于远程 server。

## 消息投影与线路

### `as_messages(envelopes, content_store) → list[ViewItem]` — `client/messages.py`

将信封流纯投影为人类可读视图。`content_store` 必须是与该流**配对的**那个。`ViewItem` 是以下类型的联合：

| 类型 | 字段 |
| --- | --- |
| `AssistantMessage` | `text` |
| `UserMessage` | `text` |
| `ToolUse` | `call_id`、`tool_name`、`arguments` |
| `ToolResultView` | `call_id`、`tool_name`、`success`、`output: str \| None` |
| `Result` | `answer`、`status` — 在 `"failed"` 时，`answer` 持有失败原因 |

### `envelope_to_dict(env) → dict` — `client/wire.py`

`EventEnvelope` 的规范 JSON 就绪字典形式（SSE 流消费的线路形态）。

### 内容块

`ImageBlock`（`noeta/protocols/messages.py`）——`start` / `send_goal` / `query(images=…)` 的图像输入块。`ContentRef`（`noeta/protocols/values.py`）——对 ContentStore 的 `hash + size + media_type` 引用。

## 主机级接线

### `HostConfig` — `client/host_config.py`

作为 `Client(..., host_config=…)` 传入的冻结数据类；永远不是 agent 身份的一部分，因此两个仅在此处不同的 client 会编译出逐字节相同的 `AgentSpec`。每个字段都默认为“缺省”，所以一个裸的 `HostConfig()` 重现内存、无预览、无 MCP 的行为。

**存储。** `storage_triple()` 返回已解析的三元组或 `None`。

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `storage_path` | `None` | 单个字符串——一个 sqlite 文件路径、一个 `postgresql://` DSN，或 `":memory:"`——经 `noeta.sdk.storage.open_storage_stack` 解析，它按 event log 所要求的顺序构建三元组 |
| `event_log` / `content_store` / `dispatcher` | `None` | 显式三元组，全有或全无 |

同时提供两种形式抛出 `ValueError`，部分显式三元组也一样。全部 `None` ⇒ 内存中。

**运行时注入**

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `app_gateway` | `None` | `AppPreviewGateway`；`None` ⇒ 没有 `open_app` 工具 |
| `write_roots` | `None` | `(task_id) → Sequence[str]` 额外写根 |
| `mcp_server_resolver` | `None` | `(alias) → McpAnyServerSpec \| None`，每轮解析 |
| `mcp_http_post` | `None` | 用于远程 MCP 的可注入 HTTP 传输（`HttpPostFn`） |
| `delta_sink` | `None` | `(StepContext, call_id, StreamDelta) → None` — 来自支持流式的 provider 的临时 token delta；从不持久化 |
| `otlp_traces` / `otlp_http_post` | `None` | `OtlpTraceConfig` 导出配置 + 传输 |
| `provider_headers` | `None` | `(StepContext) → Mapping[str, str]` 每请求头部 |

**沙箱 / 执行环境**

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `exec_env` | `None` | `SandboxExecEnvConfig` — **附着**一个共享容器 |
| `sandbox_provider` | `None` | `SandboxProvider` — 每会话预置一个全新容器；优先于 `exec_env` |
| `sandbox_spec` | `None` | 每会话 `SandboxSpec` 中部署固定的那一半（image、resources、base mounts） |
| `sandbox_exec_preamble` | `None` | `(exec_env_ref, argv) → prefix`，每条容器命令重新调用 |
| `sandbox_backend_factory` / `sandbox_browser_factory` | `None` | 在不触碰 seam 的情况下替换沙箱线路 |
| `sandbox_policy` | `None` | `(root_task_id, workspace_dir) → bool` 每会话退出开关 |

**记忆** — 优先级 `memory_root_resolver` > `memory_dir` > `global_memory_dir` > `~/.noeta/memories`。见[多租户记忆](../how-to/multi-tenant-memory.md)。

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `memory_dir` / `global_memory_dir` | `None` | 主机级存储根 |
| `memory_root_resolver` | `None` | `(task_id) → Path \| None` 每任务根 |

**Kill-switch 与 policy**

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `workflow_allowed` | `False` | 暴露 `run_workflow`（同样需要 delegation） |
| `max_background_jobs_per_root_task` | `8` | 超过上限的后台 `shell_run` 被拒绝，而非排队 |
| `max_background_subagents_per_root_task` | `8` | 对 `spawn_subagent(background=True)` 同理 |
| `instructions_enabled` | `False` | 加载工作区根的 `NOETA.md` → `AGENTS.md` |
| `instructions_file` | `None` | 只读取此路径，而不做搜索 |
| `instructions_discovery` | `False` | 由 `read` 触发的对子目录指令文件的发现（[组合器与缓存](../concepts/composer-and-cache.md)） |
| `write_mode` | `"dry_run"` | `"apply"` 执行真实写入 |

### 沙箱接口 — `client/sandbox_provider.py`、`client/sandbox.py`

| 符号 | 角色 |
| --- | --- |
| `SandboxProvider` | 产品实现的 `allocate` / `release` / `attach` Protocol |
| `SandboxSpec` | `allocate` 的输入：`image`、`mounts`、`resources`、`env` |
| `MountSpec` | 一个挂载：`source`、`target`、`mode`、`kind`（`local-path` / `nas` / `volume` / `pvc`） |
| `SandboxHandle` | 一个活绑定：`base_url`、`sandbox_id`、`auth`、`workdir` |
| `SandboxAuth` / `StaticApiKeyAuth` | `connect_headers()` Protocol 及其环境变量实现；从不序列化 |
| `encode_exec_env_ref(base_url, sandbox_id)` / `decode_exec_env_ref(ref)` | 扁平持久化 `exec_env_ref` 编解码器 |
| `SandboxExecEnvConfig` | 附着模式配置（`base_url`、`api_key_env`、`workdir`）——`client/host_config.py` |
| `ExecEnv` | 容器执行 Protocol（`noeta/runtime/exec_env.py`） |
| `BrowserBackend` | 容器的浏览器线路 Protocol（`noeta.builtins.browser.impl`，惰性重新导出） |
| `BackendFactory` / `BrowserBackendFactory` / `BoundPreamble` | `HostConfig.sandbox_backend_factory` / `sandbox_browser_factory` 所依据的可调用别名 |

`AppPreviewGateway` / `AppMount`（`noeta.builtins.app.impl`）是 `open_app` 的 Protocol，同样惰性重新导出。内核词汇模块 `noeta.runtime.mcp` 提供 `McpServerSpec`（stdio）、`McpHttpServerSpec`、`McpAnyServerSpec`（它们的联合）、`McpError`、`McpConfigError`、`HttpPostFn`。

### `path_within(resolved, root) → bool` — `noeta/runtime/workspace.py`

fs 写围栏所用的包含判定谓词，发布出来是为了让决定在 `HostConfig.write_roots` 里放什么的主机，能用围栏回答问题的完全相同方式来提问（按路径分量比对，绝不做字符串前缀匹配）。

### `NEXT_GOAL_WAKE_HANDLE` — `noeta/protocols/wake.py`

对话在两轮之间所停靠的 wake handle。产品的 session-stop seam 通过这个常量识别尾随的 next-goal 挂起。

## 记忆整合

用于策展长期记忆存储的主机可调用入口（`client/consolidation.py`）：

- `run_consolidation(client, *, memory_root, now=None, debounce=True, debounce_hours=24.0, max_root_tasks=10, max_chars_per_root_task=16000, include_task=None, on_seeded=None) → bool` —
  入队一次后台运行；当且仅当入队了一次时返回 `True`。防抖未到期以及无内容可消化时返回 `False`，不抛异常。
- `consolidation_due(memory_root, *, now, debounce_hours=24.0) → bool` — 单独的防抖那一半。
- `build_consolidation_digest(client, *, since=None, max_root_tasks=10, max_chars_per_root_task=16000, include_task=None) → str | None` —
  单独的摘要那一半，供自行编排运行的主机使用。

## 错误（类型化 / 编码）

边界代码结构化地匹配错误——`isinstance(exc, CodedError)` + `exc.code`——从不通过消息文本匹配。`CodedError` 是基类（`noeta/protocols/errors.py`）。

| 错误 | `code` | 来源 |
| --- | --- | --- |
| `QueryFailedError` — 携带 `task_id`、`status`、`reason`、`retryable` | `query_failed` | `client/client.py` |
| `ModelSelectorError` | `model_selector_rejected` | `noeta/execution/driver.py` |
| `ProviderSelectorError` | `provider_selector_rejected` | `driver.py` |
| `NotResumableError` | `not_resumable` | `driver.py` |
| `TaskAlreadyTerminalError` | `task_already_terminal` | `driver.py` |
| `UnsupportedSubtaskSuspend` | `unsupported_subtask_suspend` | `noeta/execution/subtask_drain.py` |

## 能力投影

`client/capabilities.py` 中的三个函数：

- `permission_modes() → tuple[str, ...]` — 合法的 `permission_mode` 值。
- `effort_modes() → tuple[str, ...]` — 合法的 `effort` 值。
- `model_capabilities(models) → dict[str, dict[str, bool]]` — 每模型的能力标志，例如视觉门控。

## 扩展接口

实现其中之一并通过匹配的 `Options` 字段挂载：

| 接口 | 挂载方式 | 来源 |
| --- | --- | --- |
| `Tool`（协议：元数据 + `invoke(arguments, ctx) → ToolResult`） | `allowed_tools` | `noeta/protocols/tool.py` |
| `ToolContext` / `ToolResult`（`success`、`output`、`summary`、`artifacts`、`images`、`side_effects`、`output_ref`、`file_changes`） | 工具调用输入 / 输出 | `noeta/protocols/tool.py` |
| `LLMProvider` | `provider` | `noeta/protocols/messages.py` |
| `StreamingProvider` / `StreamDelta`（可选能力：`complete_streaming(request, on_delta, request_headers=None)` 仍返回完整的 `LLMResponse`；delta 是临时副作用） | 在 `provider` 上与 `LLMProvider` 一起实现；经 `HostConfig.delta_sink` 消费 | `noeta/protocols/messages.py` |
| `LLMRequest` / `LLMResponse` / `Message` / `TextBlock` / `ToolUseBlock` / `ToolResultBlock` / `Usage` | `LLMProvider` 实现所消费和产出的材料 | `noeta/protocols/messages.py` |
| `Policy` | `policy` | `noeta/protocols/policy.py` |
| `Guard` / `GuardContext` / `VerdictResult` | `guards` | `noeta/protocols/hooks.py` |
| `ProposedAction` 及其成员 `ProposedToolCall` / `ProposedSpawnSubtask` / `ProposedFinish`（guard 按类型分派） | 传入 `Guard.check` | `noeta/protocols/hooks.py` |
| `Observer`（= `Subscriber`，一个 `Callable[[EventEnvelope], None]`） | `observers` | `noeta/protocols/event_log.py` |
| `ContentKindSpec` | `content_channels` | `noeta/context/content_channel.py` |
| `Decision`（Policy 决策类型的联合） | 由自定义 `Policy` 返回 | `noeta/protocols/decisions.py` |
| `StepContext` / `View` | 传递给自定义 `Policy` | `noeta/protocols/step_context.py` / `noeta/protocols/view.py` |

`MemoryStore`（`noeta.builtins.memory.impl`，惰性重新导出）是记忆工具背后的“每条记忆一个文件”的存储。管理记忆池的主机打开 agent 写入的同一个存储，于是两边在 slug 与 frontmatter 上保持一致。

## 插件机制

清单声明的贡献包，架在一个 surface 注册表之上，采用主机级**加载** / agent 级**激活**的拆分。完整契约见[插件参考](plugins.md)；`noeta.sdk` 上的面：

| 符号 | 角色 | 来源 |
| --- | --- | --- |
| `PluginManifest` / `ManifestContribution` | 静态清单（`name`、`requires_noeta`、`config_schema`、`contributions`）以及其中的一个条目（`surface`、`name`、`ref`、`path`、`params`） | `client/plugin_manifest.py` |
| `PluginBuilder` | *本身就是*一份清单的单文件装饰器语法糖 | `client/plugin_manifest.py` |
| `SurfaceSpec` / `SurfaceRegistry` / `standard_registry()` | surface 注册表——每个 spec 携带 `plane`、`activation_scope`、`validator`、`collision_key`、`ordering`、`activation_binding` | `client/surfaces.py` |
| `load_plugins(*, builtins=True, disabled_builtins=(), entry_points=False, modules=(), user_dirs=(), workspace_dirs=(), enabled=None, trust_store=None, registry=None, entry_point_group="noeta.plugins") → PluginSet` | 五源加载器 | `client/plugin_set.py` |
| `PluginSet` | 加载后的集合——**无需执行插件代码**即可列举 / 检查冲突 | `client/plugin_set.py` |
| `PluginActivation` | 一个外部插件的身份面贡献（由 `Client` 构建） | `client/options.py` |
| `DEFAULT_PLUGINS` | `("fs", "web")` — `Options.plugins` 的默认值；它们命名默认工具包，且不改变编译后的工具集 | `client/options.py` |
| `grant_trust(path, store=None)` / `is_trusted(path, store=None)` | 工作区目录信任存储 | `client/plugins.py` |
| `PluginError` / `UntrustedPluginDirWarning` | 高调的加载故障 / 唯一不抛异常的跳过 | `client/plugins.py` |

通过 `Options.plugins` / `AgentDefinition.plugins` 按 agent 激活已加载的插件，并把 `PluginSet` 交给 `Client(options, plugins=…)` 或 `query(…, plugins=…)`。治理面（`guard` / `observer`）一旦加载即进程级生效；其他每个面都遵循激活。

## 官方预设

`presets` —— 模块重新导出（`noeta.presets`）。关键条目：

- `main_options()` → 官方主 agent 的 `Options`。
- `official_specs()` → 四个官方 agent（`main`、`general-purpose`、`explore`、`plan`）的 `dict[str, AgentSpec]`。
- `sandbox_browser_options()` → 注册了 `web` 浏览子 agent 的 `main_options()`；沙箱部署的显式选用。
- `with_consolidation_agent(options)` → 注册内部的 `__consolidation__` 策展器，让 `run_consolidation` 可以 seed 它。
- `MAIN_SYSTEM_PROMPT`、`MAIN_WEB_SYSTEM_PROMPT`、`MEMORY_POLICY_PROMPT`、`OFFICIAL_SUBAGENTS`、`WEB_SUBAGENT`、`CONSOLIDATION_AGENT`、`CONSOLIDATION_AGENT_NAME` —— 提示与阵容材料。

## 存储适配器

`noeta.sdk.storage` 是持久化后端模块。`open_storage_stack(path)` 从一个字符串构建整个 `(event_log, content_store, dispatcher)` 三元组；`build_storage_stack`、`is_memory_path` 和 `is_postgres_url` 是更细粒度的入口。sqlite 与 postgres 适配器（`SqliteEventLog` / `SqliteContentStore` / `SqliteDispatcher`，`PostgresEventLog` / `PostgresContentStore` / `PostgresDispatcher`，它们的只读变体以及 schema 版本错误）都从同一个模块导出。

## 另见

- [你的第一个 agent](../tutorials/first-agent.md) — 引导式 SDK 演练
- [架构概览](../architecture/overview.md) — 身份 vs 接线，上下文中的扩展 seam
- [WorkerLoop](worker-loop.md) — 常驻排空原语
