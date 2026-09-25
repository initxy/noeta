# SDK 参考：`query` 与 `Client`

所有公开的名字都从 `noeta.sdk` 导入；这一页讲怎么把 agent 跑起来。以 `packages/noeta-sdk/noeta/sdk/__init__.py` 里的 `__all__` 为准，不在里面的名字不算公开接口。

## 用哪个入口

| 你要做的事 | 用 |
| --- | --- |
| 一个目标、一个答案，不追问 | `query(options, goal, ...)` |
| 多轮对话：追问、审批、取消、恢复 | `Client(options, ...)` |
| 一个进程里同时跑很多对话 | `Client` + `start_workers(n)` |
| worker 单独跑在一个进程里 | [`WorkerLoop`](worker-loop.md) |

## 从哪里导入

| 模块 | 名字 | 见 |
| --- | --- | --- |
| `noeta.sdk` | `query`、`QueryResult`、`Client`、`DriveOutcome`、`SeededTurn`、`TaskStatus`、`DeleteTaskResult`、`UsageReport`、`ModelUsage`、`DEFAULT_MODEL_ALLOWLIST`、`Principal`、`LOCAL_PRINCIPAL`、`NEXT_GOAL_WAKE_HANDLE`、各类错误 | 本页 |
| `noeta.sdk` | `WorkerLoop`、`ReliabilityEvent` | [WorkerLoop](worker-loop.md) |
| `noeta.sdk` | `Options`、`AgentDefinition`、`SystemPromptPreset`、`compile_options`、`register_preset_prompt`、`BudgetSpec`、`HostConfig`、`HooksConfig`、`PreToolUseRule`、`MatchArg`、`PostToolUseRule`、`NotificationRule`、`PluginActivation`、`DEFAULT_PLUGINS`、`permission_modes`、`effort_modes`、`model_capabilities`，以及沙箱 / MCP / OTLP 的配置类型 | [Options](options.md) |
| `noeta.sdk` | `tool`、`create_sdk_mcp_server`、扩展用的 Protocol、消息和事件类型、`as_messages`、`envelope_to_dict`、`resolve_tool_call_arguments`（取出工具调用事件的参数；参数单独存进 content store 时会去那里读） | [类型](types.md) |
| `noeta.sdk` | `PluginManifest`、`ManifestContribution`、`PluginBuilder`、`PluginSet`、`load_plugins`、`SurfaceSpec`、`SurfaceRegistry`、`standard_registry`、`grant_trust`、`is_trusted`、`PluginError` 和几个插件告警 | [插件清单](plugin-manifest.md)、[插件扩展点](plugin-surfaces.md) |
| `noeta.sdk` | `Reminder`、`ResidentActivation`、`RecallView`、`ReminderProvider`、`TURN_INTAKE` | [插件扩展点](plugin-surfaces.md) |
| `noeta.sdk` | `run_consolidation`、`consolidation_due`、`build_consolidation_digest`、`SkillUsage`、`skill_usage_from_events`、`rank_skills_by_usage`、`decayed_usage_score` | [下文](#记忆与技能辅助函数) |
| `noeta.sdk.providers` | `AnthropicProvider`、`OpenAICompatProvider`、`OpenAIResponsesProvider`、`CATALOG`、`ModelSpec`、`register_models`、`find_spec`、`catalog_models` | [接入模型](../guides/models.md) |
| `noeta.sdk.storage` | `open_storage_stack`、`build_storage_stack`、`is_memory_path`、`is_postgres_url`，以及 Sqlite / Postgres 适配器 | [Options → 存储](options.md#存储) |
| `noeta.sdk.testing` | `FakeLLMProvider`、`FakeStreamingLLMProvider` | [类型 → 测试替身](types.md#测试替身) |
| `noeta.presets`（也可以 `noeta.sdk.presets`） | 官方 agent | [Presets](presets.md) |

几个子模块分开放，是为了用到才加载：`providers` 会拉进 `httpx`，`storage` 用 Postgres 时会拉进 `psycopg`，`testing` 在生产代码里根本导入不到。

## `query`

```python
query(options, goal, *, provider=None, workspace_dir=None, model=None,
      images=(), plugins=None, host_config=None) -> QueryResult
```

临时建一个 `Client(multi_turn=False)`，把一个目标跑到出结果，然后关掉。

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `options` | `Options` | 必填 | agent 配置 |
| `goal` | `str` | 必填 | 任务内容 |
| `provider` | `LLMProvider \| None` | `None` | 优先于 `Options.provider`，两者必须有一个 |
| `workspace_dir` | `Path \| None` | `None` | 没给就用 `Options.cwd`，再没有就用当前目录 |
| `model` | `str \| None` | `None` | 这个 client 的默认模型（不走白名单检查） |
| `images` | `Sequence[ImageBlock]` | `()` | 随目标一起发的图片 |
| `plugins` | `PluginSet \| None` | `None` | 已加载的插件 |
| `host_config` | `HostConfig \| None` | `None` | 持久化存储等部署配置 |

```python
from noeta.sdk import HostConfig, Options, query
from noeta.sdk.providers import AnthropicProvider

result = query(
    Options(system_prompt="Answer in one sentence."),
    goal="What is an append-only log?",
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
    host_config=HostConfig(storage_path="noeta.sqlite"),  # 可选：把记录存下来
)
print(result.answer())
```

### `QueryResult`

本身就是 `list[EventEnvelope]`，可以像列表一样遍历和下标访问，另外多四样东西：

| 成员 | 返回 | 说明 |
| --- | --- | --- |
| `.task_id` | `str` | 跑的是哪个任务 |
| `.messages()` | `list[ViewItem]` | 可读的对话记录，内容已经取出来了 |
| `.answer()` | `Any` | 最终答案；任务失败或没跑完会抛 `QueryFailedError`。如果还有工具调用（agent 自己的或子 agent 的）在等审批，错误的 `reason` 会直说，并给出 handle 和子任务 id：`query()` 没有人可问，要传 `Options.can_use_tool` |
| `.usage()` | `UsageReport` | 这次查询花了多少，子 agent 也算在内；和 `Client.usage()` 给的是同一种报告，在临时 client 关掉之前就算好了 |

::: warning
这些结果在临时 client 关闭前就已经算好。别拿原始事件去配一个新的 content store 重新算，引用的内容已经找不到了。
:::

## `Client`

```python
Client(options, *, provider=None, workspace_dir=None, model=None,
       multi_turn=True, host_config=None, allowed_models=None, plugins=None,
       principal=LOCAL_PRINCIPAL)
```

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `options` | `Options` | 必填 | agent 配置 |
| `provider` | `LLMProvider \| None` | `None` | 优先于 `Options.provider`；两个都没有会抛 `ValueError` |
| `workspace_dir` | `Path \| None` | `None` | 没给就用 `Options.cwd`，再没有就用 `Path.cwd()` |
| `model` | `str \| None` | `None` | 这个 client 的默认模型，不走白名单检查 |
| `multi_turn` | `bool` | `True` | `True`：一轮结束后停下等下一句；`False`：一轮结束任务就完成 |
| `host_config` | `HostConfig \| None` | `None` | 存储、沙箱、MCP、记忆等部署配置；`None` 就全在内存里 |
| `allowed_models` | `Sequence[str] \| None` | `None` | 每轮 `model_selector` 的白名单；`None` 用 `DEFAULT_MODEL_ALLOWLIST`（`opus`、`sonnet`、`haiku`）；`()` 表示一个都不许 |
| `plugins` | `PluginSet \| None` | `None` | 已加载的插件；影响 agent 本身的部分只在 `Options.plugins` 激活时生效，guard 和 observer 对所有任务都生效 |
| `principal` | `Principal` | `LOCAL_PRINCIPAL` | 某一轮没单独指定时，由谁来操作。`model_selector` 必须同时在它的 `allowed_models` 和 Client 的 `allowed_models` 里；它的 `identity` 会记到 `ModelBound` 上。`LOCAL_PRINCIPAL` 什么模型都允许，所以只剩 `allowed_models` 这一道限制 |

属性：`registry`（编译好的 `AgentRegistry`）、`main_agent_name`、`workers_running`。

用 `with` 包起来，保证 `shutdown()` 一定会执行：

```python
from noeta.sdk import Client, Options
from noeta.sdk.providers import AnthropicProvider

with Client(Options(system_prompt="You are a coding assistant."),
            provider=AnthropicProvider(), model="claude-sonnet-5", workspace_dir=".") as client:
    out = client.start(goal="Summarise README.md")
    print(client.task_answer(out.task_id))
    out = client.send_goal(out.task_id, goal="Now list its headings.")
```

### 开始和推进一轮

下面每个方法都在当前线程里把这一轮跑完，返回 `DriveOutcome(task_id, status, wake_handle)`。设了 `Options.can_use_tool` 时，需要审批的调用都会先交给它判断。

没有 async 版本的接口：每个方法都会阻塞到这一轮结束。在 asyncio 代码里，把它放到工作线程上调用即可——`await asyncio.to_thread(client.send_goal, task_id, goal=...)`——这样用是安全的。

| 方法 | 签名（`task_id` 之后都是关键字参数） | 说明 |
| --- | --- | --- |
| `start` | `(*, goal, agent=None, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), workspace_dir=None, effort=None, activations=(), attachment_texts=(), principal=None)` | 新建任务并跑第一轮 |
| `send_goal` | `(task_id, *, goal, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), effort=None, activations=(), attachment_texts=(), principal=None)` | 追加一轮。如果上一次同样内容的 `send_goal` 已经把目标记下、却在开跑前失败了（任务停下时的原因是 `seed_failed`），重试会直接接着这条已记下的目标跑，不会再记一遍 |
| `inject_goal` | `(task_id, *, goal, images=(), goal_origin=None, drive=True)` | 任务正在跑：先记下这条消息，下一轮边界时送进去，立刻返回；停在等下一句：等同 `send_goal`（`drive=False` 时抛 `NotResumableError`） |
| `deliver_event` | `(task_id, *, event_kind, payload=None)` | 唤醒在 `wait_external` 上等 `event_kind` 的任务；`payload` 记成一条系统消息 |

| 每轮参数 | 说明 |
| --- | --- |
| `agent` | 跑哪个 agent，默认主 agent |
| `model_selector` | 这一轮用的模型别名，要在 `allowed_models` 里，也要是当前操作者允许的，否则抛 `ModelSelectorError` |
| `principal` | 这一轮由谁来操作，给一个 Client 同时服务多个用户的宿主用；`None` 就用 Client 的 `principal`。检查和记录都在这一轮被准备好（seed）的时候完成，所以 `seed_start` / `seed_send_goal` 也接收它，`drive_seeded` / `dispatch_seeded` 不用再传 |
| `permission_mode` | 这一轮的审批模式：`default` / `acceptEdits` / `bypassPermissions` |
| `enabled_mcp` | 这一轮启用的 MCP 别名（由 `HostConfig.mcp_server_resolver` 解析） |
| `workspace_dir` | 只有 `start` 有：记到任务上，之后每轮都沿用 |
| `effort` | 这一轮的推理强度 |
| `activations` | 这一轮开始前先加载的技能名，`/skill-name` 命令走的就是它 |
| `attachment_texts` | 宿主准备好的上下文（`@` 引用、任务说明等），每段在目标之前记成一条单独的系统消息 |

`DriveOutcome.status` 是这一轮之后任务的状态（正常结束是 `suspended`，失败或取消后是 `terminal`）。`wake_handle` 说明在等什么：`NEXT_GOAL_WAKE_HANDLE` 是等下一句，`approval-{call_id}` 是等审批，`None` 表示没在等人。

### 批准、拒绝、回答

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `approve` | `(task_id, *, call_id, reason=None, resolver="client")` | 执行这次调用，继续 |
| `deny` | `(task_id, *, call_id, reason=None, resolver="client")` | 拒绝；模型会看到拒绝理由，这一轮接着跑 |
| `answer` | `(task_id, *, question_id, answers, answered_by="client")` | 回答 `AskUserQuestion` 的提问 |

```python
APPROVAL = "approval-"

def pending_approval(client, task_id):
    """某个任务日志里最新一条还没处理的审批请求。"""
    events = client.events(task_id)
    resolved = {e.payload.call_id for e in events
                if e.type == "ToolCallApprovalResolved"}
    return next((e for e in reversed(events)
                 if e.type == "ToolCallApprovalRequested"
                 and e.payload.call_id not in resolved), None)

out = client.start(goal="Refactor utils.py")
while out.wake_handle and out.wake_handle.startswith(APPROVAL):
    call_id = out.wake_handle[len(APPROVAL):]
    req = pending_approval(client, out.task_id)  # 是子 agent 在问时为 None
    # 在这里看 req.payload（工具名、参数）决定：批准还是拒绝
    out = client.approve(out.task_id, call_id=call_id)
```

handle 永远是 `approval-{call_id}`：工具调用要审批时如此；`finish` 或派生子任务要审批时是 `approval-finish-{task_id}` / `approval-spawn-{task_id}`，对应的 `call_id` 是 `finish-{task_id}` / `spawn-{task_id}`；前台子 agent 的请求也会以同样的形式出现在根任务的返回结果上。所以 `call_id` 直接从 handle 取，用根任务 id 调 `approve` / `deny` / `answer` 会转给正在等的那个子 agent（传子 agent 自己的 id 也行）。

想看*在问什么*（工具名、参数），读 `ToolCallApprovalRequested` 事件。它记在发问的那个任务的日志里：根任务自己问就在根任务的日志里，子 agent 问就在子 agent 的日志里（这时 `pending_approval(client, out.task_id)` 是 `None`，要改读子 agent 的事件）。要拿最新一条*还没处理*的请求，别拿日志里的第一条：一轮里问过两次时，第一条已经处理过了，再批一次会抛 `NotResumableError`。

### 先落盘，再推进

HTTP 请求线程不能一直等一整轮跑完。`seed_*` 在请求线程上把所有需要持久化和校验的步骤做完（所以 `ModelSelectorError` / `NotResumableError` 还是同步抛出），返回一个 `SeededTurn`。

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `seed_start`、`seed_send_goal`、`seed_approve`、`seed_deny`、`seed_answer`、`seed_deliver_event` | 和对应方法一样 | 返回 `SeededTurn`，不跑这一轮 |
| `drive_seeded` | `(seeded) -> DriveOutcome` | 在当前线程跑 |
| `dispatch_seeded` | `(seeded) -> None` | 交给 worker 池，立刻返回；需要先 `start_workers` |

### 控制对话

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `interrupt` | `(task_id, *, reason=None, interrupted_by="user", force=False)` | 在下一个边界停掉正在跑的这一轮，任务回到等下一句，`send_goal` 可以直接接着聊。线程安全。`force=True` 用来清掉卡死的一步，先调一次普通 `interrupt` |
| `cancel` | `(task_id, *, reason="cancelled", cascade=False)` | 结束对话（终态），顺带杀掉它的后台 shell |
| `close` | `(task_id, *, closed_by="user", reason=None)` | 标记归档；状态仍是 `suspended`，再 `send_goal` 会自动重新打开 |
| `reopen` | `(task_id, *, reopened_by="user", reason=None)` | 去掉归档标记 |
| `rewind` | `(task_id, *, message_seq)` | 撤销 `message_seq` 那条用户消息及之后的一切，改过的文件会恢复；日志本身只追加不删 |
| `fork` | `(task_id, *, message_seq)` | 拿那条消息之前的历史开一个新任务，原任务不动，返回新任务的 `task_id`。只能 fork 根任务，两边共用同一个工作目录 |

### 查询

只读，不写任何东西。

| 方法 | 返回 | 说明 |
| --- | --- | --- |
| `events(task_id)` | `list[EventEnvelope]` | 完整事件流 |
| `events_after(task_id, after_seq=None)` | `list[EventEnvelope]` | 游标之后的事件 |
| `messages(task_id)` | `list[ViewItem]` | 可读的对话记录 |
| `task_answer(task_id)` | `Any` | 最近一轮的原始答案（设了 `output_schema` 时是 `dict`）；没有就是 `None` |
| `task_status(task_id)` | `TaskStatus \| None` | `task_id`、`status`、`closed`、`wake_handle`、`parent_task_id`；任务不存在返回 `None` |
| `usage(task_id, *, include_children=True)` | `UsageReport` | 按模型汇总的费用、token 和耗时，默认把它派出去的所有子 agent 也算进来；看到 `$0` 先查 `unpriced_models` 再信 |
| `suspend_reason(task_id)` | `SuspendReason \| None` | 上次为什么停下；拿 `.kind` 和 `SUSPEND_REASON_WAITING_HUMAN` / `_INTERRUPTED` / `_TURN_FAILED` 比较 |
| `task_streams()` | `list[TaskStreamSummary]` | 所有事件流：`task_id`、`last_seq`、`last_event_time` |
| `task_summaries()` | `list[dict]` | 每个任务一行汇总；要读完整个日志，适合启动时修复，别拿来渲染列表 |
| `subscribe(callback)` | 取消订阅的函数 | 所有任务新提交的事件 |
| `get_content(content_hash)` | `bytes \| None` | 读一块存储内容 |
| `put_content(body, *, media_type)` | `ContentRef` | 存一段字节（比如上传的图片） |
| `memory_root(task_id=None)` | `Path` | 这个任务用的记忆目录 |
| `delete_task(task_id)` | `DeleteTaskResult` | 彻底删除任务和它的子任务：`{ok, task_id, deleted, reason?}`；正在跑返回 `reason="running"`，找不到返回 `"not_found"` |

### worker 与收尾

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `start_workers` | `(num_workers=1, *, poll_interval=0.1, heartbeat_interval=30.0, stale_sweep_interval=10.0, timer_poll_interval=1.0, lease_seconds=600.0, shutdown_grace_s=10.0, lease_backoff_max_s=None)` | 在这个 client 的队列上起常驻 worker 线程；调第二次抛 `RuntimeError`。`lease_backoff_max_s` 是调度一直出错时退避时间的上限（`None` 用 `WorkerLoop` 的默认值）；可靠性信号交给 `HostConfig.reliability_sink` |
| `stop_workers` | `(timeout=None) -> bool` | 有 worker 没按时退出就返回 `False`，再调一次即可 |
| `reconnect_mcp` | `(alias=None)` | 断开池里的 MCP 连接（全部或某个别名）；正在跑的这一轮用完才断 |
| `add_sandbox_lifecycle_listener` | `(on_allocate, on_release)` | 容器分配和释放时的回调；没配沙箱时什么也不做 |
| `shutdown` | `()` | 可重复调用：停 worker、observer、MCP 连接和沙箱；杀掉这个 client 的后台 shell；后台子 agent 在下一步边界停下，不会被取消，存储上的下一个 `Client` 会接着跑；关闭由 `storage_path` 打开的存储（你自己传进来的存储不关） |

## 记忆与技能辅助函数

| 函数 | 说明 |
| --- | --- |
| `run_consolidation(client, *, memory_root, now=None, debounce=True, debounce_hours=24.0, max_root_tasks=10, max_chars_per_root_task=16000, include_task=None, on_seeded=None) -> bool` | 排一次后台记忆整理；排上了返回 `True` |
| `consolidation_due(memory_root, *, now, debounce_hours=24.0) -> bool` | 只检查是否过了间隔 |
| `build_consolidation_digest(client, *, since=None, max_root_tasks=10, max_chars_per_root_task=16000, include_task=None) -> str \| None` | 只生成摘要，给自己调度整理的宿主用 |
| `skill_usage_from_events(events) -> dict[str, SkillUsage]` | 统计技能使用次数；`SkillUsage(count, last_used_at)` |
| `decayed_usage_score(usage, *, now, half_life_days=7.0, floor=0.1) -> float` | 按时间衰减的分数 |
| `rank_skills_by_usage(usage, *, now, half_life_days=7.0, floor=0.1) -> dict[str, float]` | `skill_menu_rank_resolver` 要返回的排序 |

## 错误

用 `isinstance(exc, CodedError)` 加 `exc.code` 判断，别匹配报错文字。

| 错误 | `code` | 什么时候抛 |
| --- | --- | --- |
| `QueryFailedError`（`task_id`、`status`、`reason`、`retryable`、`detail`） | `query_failed` | 任务失败或没跑完时调 `QueryResult.answer()`；`detail` 是记录下来的失败详情（比如模型服务的报错正文），没有就是 `""` |
| `InvalidTurnOptionError` | `invalid_turn_option` | `start` / `send_goal` 收到的 `permission_mode` 或 `effort` 是 `Options` 也会拒绝的值；在写入任何东西之前就拒绝 |
| `AnswerValidationError` | `invalid_answer` | `answer` / `seed_answer` 给的回答和待回答的问题对不上；同时也是 `ValueError` |
| `QuestionNotPendingError`（`task_id`、`question_id`） | `question_not_pending` | 任务在等一个提问，但这个提问已经不在待回答状态，却调了 `answer` |
| `WorkspaceEscape` | `workspace_escape` | 路径解析到工作区外面；同时也是 `ValueError` |
| `ModelSelectorError` | `model_selector_rejected` | `model_selector` 不在白名单里 |
| `ProviderSelectorError` | `provider_selector_rejected` | 宿主没配置这组 `(provider, model)` |
| `NotResumableError` | `not_resumable` | 任务没在等这个操作：`deliver_event` 的事件它根本没在等；`approve` / `deny` / `answer` 针对的请求已经处理过，或任务已结束；对不存在的 id 调 `send_goal` / `approve` |
| `TaskAlreadyTerminalError` | `task_already_terminal` | 对已结束的任务调 `cancel` / `interrupt` / `close` / `reopen` |
| `UnknownTaskError`（`task_id`、`verb`、`reason`） | `unknown_task` | 对不存在的 id 调 `cancel` / `interrupt` / `close` / `reopen`；在写入任何东西之前就拒绝 |
| `NotForkableError`（`task_id`、`reason`） | `not_forkable` | `fork` 的 id 不存在、是子任务，或 `message_seq` 不是用户消息 |
| `UnsupportedSubtaskSuspend` | `unsupported_subtask_suspend` | `Client` 的方法不会抛它：子 agent 的审批和提问都会交到根任务上，子 agent 等定时器时根任务停在 `wake_handle=None`，见[子 agent](../guides/subagents.md) |

## 下一步

- [Options](options.md)：配置这些方法运行的 agent
- [类型](types.md)：事件、消息、`@tool`、测试替身
- [任务与唤醒](../how-it-works/tasks-and-waking.md)：两轮之间发生了什么
