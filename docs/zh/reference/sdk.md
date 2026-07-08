# SDK 参考（`noeta.sdk`）

`noeta.sdk` 是 SDK 唯一的公开导入面。以下所有内容都从它重新导出——用户永远不直接导入 `noeta.client` 或运行时内部。事实来源：`packages/noeta-sdk/noeta/sdk/__init__.py:108-174` 中的 `__all__` 列表。

```python
from noeta.sdk import query, Client, Options, tool
```

## Client 动词

### `query(options, goal, *, provider=None, workspace_dir=None, model=None, images=()) → QueryResult`

一次性查询：驱动单轮到真正的终止状态，并返回带有预 fold 投影的完整信封流（`packages/noeta-sdk/noeta/client/client.py:984`）。创建一个临时的 `Client(multi_turn=False)` 并在返回前关闭它。多轮工作请直接使用 `Client`。

### `QueryResult` — `client/client.py:881`

`list[EventEnvelope]` 的子类（迭代 / 索引行为类似列表），外加：

| 成员 | 返回值 | 备注 |
| --- | --- | --- |
| `.task_id` | `str` | 被驱动的任务 |
| `.messages()` | `list[ViewItem]` | 预 fold 的人类视图；每个 `ContentRef` 已解引用 |
| `.answer()` | `Any` | 终止答案；任务失败或非终止时**抛出 `QueryFailedError`** |

投影在拆除之前针对临时 Client 的 ContentStore 物化——不要用新的存储重新投影原始信封。

### `Client` — `client/client.py:122`

```python
Client(options, *, provider=None, workspace_dir=None, model=None,
       multi_turn=True, host_config=None, allowed_models=None)
```

（`client/client.py:147`）provider 必须来自 `provider` 关键字参数或 `Options.provider`，工作区来自 `workspace_dir` 或 `Options.cwd`——否则抛出 `ValueError`。存储默认为内存中；传递 `HostConfig` 以注入持久化三元组。

| 方法 | 签名（`task_id` 之后为关键字参数） | 源码 |
| --- | --- | --- |
| `start` | `(*, goal, agent=None, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), workspace_dir=None, effort=None)` → outcome | `client.py:392` |
| `send_goal` | `(task_id, *, goal, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), effort=None)` → outcome | `client.py:440` |
| `approve` | `(task_id, *, call_id, reason=None, resolver="client")` | `client.py:475` |
| `deny` | `(task_id, *, call_id, reason=None, resolver="client")` | `client.py:488` |
| `answer` | `(task_id, *, question_id, answers, answered_by="client")` | `client.py:501` |
| `deliver_event` | `(task_id, *, event_kind, payload=None)` — 唤醒 `wait_external` 挂起；按 `event_kind` 精确匹配，可选 `payload` 作为 `origin="system"` 消息记录在恢复轮上 | `client.py:517` |
| `cancel` | `(task_id, *, reason="cancelled", cascade=False)` | `client.py:657` |
| `close` | `(task_id, *, closed_by="user", reason=None)` | `client.py:669` |
| `reopen` | `(task_id, *, reopened_by="user", reason=None)` | `client.py:681` |
| `events` | `(task_id)` → `list[EventEnvelope]` | `client.py:705` |
| `messages` | `(task_id)` → `list[ViewItem]` | `client.py:709` |
| `events_after` | `(task_id, after_seq=None)` → `list[EventEnvelope]` — 严格在游标之后的流 | `client.py:719` |
| `task_streams` | `()` → 每任务 `(task_id, last_seq)` 摘要 | `client.py:729` |
| `delete_task` | `(task_id)` → `{"ok", "reason"?, "task_id", "deleted": [...]}`；以 `reason="running"` / `"not_found"` 拒绝 | `client.py:738` |
| `subscribe` | `(callback)` → 取消订阅可调用对象；提交后信封，所有任务 | `client.py:846` |
| `shutdown` | `()` — 幂等的 Observer 拆除 | `client.py:856` |

属性：`registry`（编译后的 `AgentRegistry`，`client.py:661`）和 `main_agent_name`（`client.py:666`）。`start` 时的 `workspace_dir` 被一次性焊入持久化的 `TaskHostBound`；后续轮次 fold 解析它。`permission_mode` / `enabled_mcp` / `effort` 是每轮的、非持久化的主机旋钮。

## 配方：`Options`

### `Options` — `client/options.py:197`

编译为 `AgentSpec` 的冻结数据类。字段分为**身份**（进入记录）和**接线**（仅挂载点，被 `compile_options` 忽略）：

| 字段 | 类型 / 默认值 | 类别 |
| --- | --- | --- |
| `system_prompt` | `str \| SystemPromptPreset` — 必填 | 身份 |
| `name` | `str = "main"` | 身份 |
| `skills` | `tuple[str, ...] = ()` | 身份 |
| `budget` | `BudgetSpec \| None` — `None` ⇒ 默认 `max_subtask_depth=3` | 身份 |
| `capabilities` | `Capabilities \| None` — `None` ⇒ 从子项派生 | 身份 |
| `agents` | `Mapping[str, AgentDefinition] = {}` — 扁平、非递归 | 身份 |
| `allowed_tools` | `tuple \| None` — `None` ⇒ **全部 11 个内置**；条目是名称字符串或 `DecoratedTool` | 身份 |
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

无效的 `thinking` / `effort` 值在构造时抛出 `ValueError`；无效的 `permission_mode` 在编译时抛出（`options.py:541`）。

### `AgentDefinition` — `client/options.py:121`

扁平面子代理配方：`description`（必填，非空）、`prompt`（必填）、`tools`（`None` ⇒ 全部内置）、`model`、`capabilities`、`metadata`。不能嵌套——子项是叶子。

### `SystemPromptPreset` — `client/options.py:101`

`preset: str = "main"`，`append: str | None = None` — 解析已注册的预设提示，可选追加后缀。

### `compile_options(options) → (AgentSpec, tuple[AgentSpec, ...])` — `client/options.py:514`

将配方纯编译为 `(main_spec, descendant_specs)`。引用透明：相等的 `Options` 产生相等的 `AgentSpec`。

### `register_preset_prompt(name, prompt) → None` — `client/options.py:84`

为 `SystemPromptPreset` 注册一个命名预设（后写者胜出）。

## 创作

### `@tool` — `packages/noeta-runtime/noeta/tools/decorator.py:99`

```python
@tool(name="word_count", version="1", risk_level="low",
      input_schema={...}, description="...")
def word_count(arguments: dict, ctx: ToolContext) -> ToolResult: ...
```

将 `fn(arguments, ctx) → ToolResult` 包装为 `DecoratedTool`（`decorator.py:43`）。`version` 是**必填的**——省略它抛出 `TypeError`（version 供给身份指纹）。`risk_level` 默认为 `"low"`。`input_schema` 是面向 LLM 的元数据（不在运行时验证）；`description` 是模型工具语义的唯一来源。也可以直接调用：`tool(fn, name=..., ...)`。

### `create_sdk_mcp_server(name, version="1.0.0", tools=()) → SdkMcpServer` — `sdk/authoring.py:60`

将 `@tool` 函数打包为进程内（`"sdk"` 传输）MCP server，用于 `Options.mcp_servers`。空 `name` 抛出 `ValueError`；非 `DecoratedTool` 条目抛出 `TypeError`。`SdkMcpServer`（`sdk/authoring.py:35`）是冻结的：`name`、`version`、`tools`。

## 消息投影与线路

### `as_messages(envelopes, content_store) → list[ViewItem]` — `client/messages.py:150`

将信封流纯投影为人类可读视图。`content_store` 必须是与流**配对的**那个。`ViewItem`（`messages.py:136`）是以下类型的联合：

| 类型 | 字段 | 源码 |
| --- | --- | --- |
| `AssistantMessage` | `text` | `messages.py:80` |
| `UserMessage` | `text` | `messages.py:87` |
| `ToolUse` | `call_id`、`tool_name`、`arguments` | `messages.py:94` |
| `ToolResultView` | `call_id`、`tool_name`、`success`、`output: str \| None` | `messages.py:108` |
| `Result` | `answer`、`status` — 在 `"failed"` 时，`answer` 持有失败原因 | `messages.py:123` |

### `envelope_to_dict(env) → dict` — `client/wire.py:25`

`EventEnvelope` 的规范 JSON 就绪字典形式（SSE 流和 web 前端消费的线路形态）。

### 内容块

`ImageBlock`（`noeta/protocols/messages.py:121`）——`start` / `send_goal` / `query(images=…)` 的图像输入块。`ContentRef`（`noeta/protocols/values.py:27`）——对 ContentStore 的 `hash + size + media_type` 引用。

## 主机级接线

### `HostConfig` — `client/host_config.py:38`

作为 `Client(..., host_config=…)` 传递的冻结数据类；永远不是代理身份的一部分。字段：持久化存储三元组 `event_log` / `content_store` / `dispatcher`（**全有或全无**——`host_config.py:85` 的 `storage_triple()` 在部分设置时抛出 `ValueError`；全部 `None` ⇒ 内存中）、`app_gateway`（`AppPreviewGateway` —— `None` ⇒ 没有 `open_app` 工具）、`mcp_server_resolver`（`(alias) → McpAnyServerSpec | None`）、`mcp_http_post`（可注入的 HTTP 传输，`HttpPostFn`）、`delta_sink`（`(StepContext, call_id, StreamDelta) → None` ——在支持流式传输的 provider 调用进行中接收临时 token delta；`None` ⇒ 不流式传输，provider 完全按之前的方式调用；delta 从不持久化）、`workflow_allowed: bool = False`，以及 `write_mode: str = "dry_run"`（`"apply"` 执行真实写入）。

来自 `noeta.tools.app` / `noeta.tools.mcp` 的相关重新导出：`AppPreviewGateway`、`AppMount`、`McpServerSpec`（stdio）、`McpHttpServerSpec`、`McpAnyServerSpec`（它们的联合）、`McpError`、`McpConfigError`、`HttpPostFn`。

## 错误（类型化 / 编码）

边界代码结构化地匹配错误——`isinstance(exc, CodedError)` + `exc.code`——从不通过消息文本匹配。`CodedError` 是基类（`noeta/protocols/errors.py:18`）。

| 错误 | `code` | 源码 |
| --- | --- | --- |
| `QueryFailedError` — 携带 `task_id`、`status`、`reason`、`retryable` | `query_failed` | `client/client.py:848` |
| `ModelSelectorError` | `model_selector_rejected` | `noeta/execution/driver.py:123` |
| `ProviderSelectorError` | `provider_selector_rejected` | `driver.py:144` |
| `NotResumableError` | `not_resumable` | `driver.py:171` |
| `TaskAlreadyTerminalError` | `task_already_terminal` | `driver.py:204` |
| `UnsupportedSubtaskSuspend` | `unsupported_subtask_suspend` | `noeta/execution/subtask_drain.py:110` |

## 能力投影

三个**函数**（`packages/noeta-sdk/noeta/client/capabilities.py`）：

- `permission_modes() → tuple[str, ...]` — 合法的 `permission_mode` 值（`capabilities.py:21`）。
- `effort_modes() → tuple[str, ...]` — 合法的 `effort` 值（`capabilities.py:26`）。
- `model_capabilities(models) → dict[str, dict[str, bool]]` — 每模型能力标志，例如视觉门控（`capabilities.py:31`）。

## 扩展接口

实现其中之一并通过匹配的 `Options` 字段挂载：

| 接口 | 挂载方式 | 源码 |
| --- | --- | --- |
| `Tool`（协议：元数据 + `invoke(arguments, ctx) → ToolResult`） | `allowed_tools` | `noeta/protocols/tool.py:132` |
| `ToolContext` / `ToolResult`（`success`、`output`、`artifacts`、`output_ref`） | 工具调用输入 / 输出 | `tool.py:108` / `tool.py:19` |
| `LLMProvider` | `provider` | `noeta/protocols/messages.py:286` |
| `StreamingProvider` / `StreamDelta`（可选能力：`complete_streaming(request, on_delta, request_headers=None)` 仍返回完整的 `LLMResponse`；delta 是临时副作用） | 在 `provider` 上与 `LLMProvider` 一起实现；通过 `HostConfig.delta_sink` 消费 | `messages.py` |
| `Policy` | `policy` | `noeta/protocols/policy.py:21` |
| `Guard` / `GuardContext` / `ProposedAction` / `VerdictResult` | `guards` | `noeta/protocols/hooks.py:159` / `111` /（载荷类型）/ `45` |
| `Observer`（= `Subscriber`，一个 `Callable[[EventEnvelope], None]`） | `observers` | `noeta/protocols/event_log.py:47` |
| `ContentKindSpec` | `content_channels` | `noeta/context/content_channel.py:63` |
| `Decision`（Policy 决策类型的联合） | 由自定义 `Policy` 返回 | `noeta/protocols/decisions.py:427` |
| `StepContext` / `View` | 传递给自定义 `Policy` | `noeta/protocols/step_context.py:17` / `noeta/protocols/view.py:70` |

## 官方预设

`presets` —— 模块重新导出（`noeta.presets`，`packages/noeta-runtime/noeta/presets/__init__.py`）。关键条目：`main_options()`（`presets/__init__.py:159`）返回官方主代理 `Options`；`official_specs()`（`presets/__init__.py:185`）返回编译后的四代理集（`main` / `general-purpose` / `explore` / `plan`）。

## 另见

- [你的第一个代理](../tutorials/first-agent.md) — 引导式 SDK 演练
- [架构概览](../architecture/overview.md) — 身份 vs 接线，上下文中的扩展 seam
- [WorkerLoop](worker-loop.md) — 常驻排空原语
