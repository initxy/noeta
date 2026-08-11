# query / Client

这两个是让一个 agent 真正跑起来的动词。`query` 把一个目标驱动到答案然后把一切拆掉；`Client` 让一场对话跨越多轮、跨越审批、跨越重启地活着。两者都定义在 `packages/noeta-sdk/noeta/client/client.py`，并从 `noeta.sdk` 重新导出。

如果你要配置的是这个 agent *是什么*，你需要的是 [Options](sdk-options.md)。本页讲的是怎么*运行*它。

## 我该用哪一个？

| 你想要 | 用 | 为什么 |
| --- | --- | --- |
| 一个目标、一个答案，不再追问 | `query(...)` | 创建一个用完即弃的 `Client`，驱动到终止，返回整条信封流 |
| 一场对话——追问、审批、取消 | `Client` | 让任务保持存活；每个动词都恢复同一个 `task_id` |
| 一个进程里同时跑很多场对话 | `Client` + `start_workers(n)` | 由一个常驻池在后台排空各轮，而不是压在调用线程上 |

## `query`

```python
query(options, goal, *, provider=None, workspace_dir=None, model=None,
      images=(), plugins=None, host_config=None) -> QueryResult
```

它构建一个临时的 `Client(multi_turn=False)`，好让 agent 抵达一个真正的终止状态，而不是停在下一目标挂起上；然后驱动一轮、fold 出各个投影，最后关掉这个 client。它的参数与 `Client` 构造函数一致，因此这条语法糖路径并不局限于内存存储——传入一个 `host_config` 就能把这次运行持久记录下来。

```python
from noeta.sdk import HostConfig, Options, query

result = query(
    Options(system_prompt="Answer in one sentence."),
    goal="What does fold(events) mean here?",
    provider=my_provider,
    workspace_dir=".",
    host_config=HostConfig(storage_path="noeta.sqlite"),
)

print(result.task_id)      # → 't-1a2b3c…'
print(len(result))         # → 14   (QueryResult is a list of EventEnvelope)
print(result.answer())     # → 'State is derived by replaying the event log.'
```

### `QueryResult`

一个 `list[EventEnvelope]` 子类——迭代和索引的行为都和列表一样——外加三样东西：

| 成员 | 返回 | 备注 |
| --- | --- | --- |
| `.task_id` | `str` | 被驱动的那个任务 |
| `.messages()` | `list[ViewItem]` | 人类可读的视图，每个 `ContentRef` 都已解引用 |
| `.answer()` | `Any` | 终止答案；任务失败或从未抵达终止时**抛出 `QueryFailedError`** |

这些投影是在拆除*之前*针对临时 client 的 ContentStore 物化的。不要用一个新的存储去重新投影原始信封——它们引用的大体积正文将不再能解析。若想要一种宽容的读法，从 `.messages()` 里取出终止的那个 `Result` 项，按它的 `status` 分支。

## `Client`

```python
Client(options, *, provider=None, workspace_dir=None, model=None,
       multi_turn=True, host_config=None, allowed_models=None, plugins=None)
```

provider 必须来自 `provider` 关键字参数或 `Options.provider`，否则构造函数抛出 `ValueError`。工作区按 `workspace_dir` > `Options.cwd` > `Path.cwd()` 的顺序解析。存储默认为内存中；传入一个 [`HostConfig`](sdk-options.md#hostconfig) 以注入持久化后端。

`allowed_models` 是每轮模型选择器的白名单。`None` 回落到 `DEFAULT_MODEL_ALLOWLIST`（`opus` / `sonnet` / `haiku`）；一个显式的**空**序列则不授权任何选择器，而宿主默认值仍然绑定。

`plugins` 是一个已加载的 `PluginSet`（见 [插件](plugins.md)）。它的身份面贡献只有在 `Options.plugins` 激活了该插件的地方才会到达某个 agent；而它的 guard 与 observer 是进程级生效的。加载集里不存在的激活名会让构建失败。

`Client` 是一个上下文管理器，因此 `shutdown` 不会被遗忘：

```python
from noeta.sdk import Client, Options

with Client(Options(system_prompt="…"), provider=my_provider,
            workspace_dir=".") as client:
    outcome = client.start(goal="Summarise README.md")
    print(outcome.task_id)      # → 't-9f8e…'
```

属性：`registry`（编译出的 `AgentRegistry`）、`main_agent_name`、`workers_running`。

## 驱动轮次的动词

每个动词都在调用线程上跑完整轮并返回一个 `DriveOutcome`。当配置了 `Options.can_use_tool` 时它们都会经它排空，因此无论是哪个动词恢复了对话，一次被门控的工具调用都以同样的方式被解决。

| 方法 | 签名（`task_id` 之后为关键字参数） |
| --- | --- |
| `start` | `(*, goal, agent=None, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), workspace_dir=None, effort=None, activations=(), attachment_texts=())` |
| `send_goal` | `(task_id, *, goal, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), effort=None, activations=(), attachment_texts=())` |
| `approve` | `(task_id, *, call_id, reason=None, resolver="client")` |
| `deny` | `(task_id, *, call_id, reason=None, resolver="client")` |
| `answer` | `(task_id, *, question_id, answers, answered_by="client")` |
| `deliver_event` | `(task_id, *, event_kind, payload=None)` |

`start` 时的 `workspace_dir` 被一次性焊入持久化的 `TaskHostBound` 记录；之后每一轮都靠 fold 解析它，这也是 `send_goal` 没有这个参数的原因。`permission_mode`、`enabled_mcp`、`effort` 和 `activations` 是每轮的、非持久的宿主旋钮。`activations` 在循环开始前钉住内置 skill——这正是 `/skill-name` 斜杠命令所依附的通道。

`attachment_texts` 是宿主拼好的参考快照（`@` 提及、任务简报、工作区摘要），每一条作为独立的 `origin="system"` 消息落账在 goal **之前**，所以逐字稿绝不会把它们记到人头上。它们是普通的落账消息，续流读得回来、不会重读。文本在发送时已经定稿就用这条；如果它必须在**记账的那一刻**现算——因为要读实时状态——那就改为贡献一个 `reminder_provider`（见[插件面](plugin-surfaces.md)），它的产出落在 goal **之后**。两条通道都只用公开名就能走通：`Reminder`、`RecallView`、`ReminderProvider` 和 `TURN_INTAKE` 都从 `noeta.sdk` 导出。

`deliver_event` 唤醒一个挂在 `wait_external` 上的任务。匹配按 `event_kind` 精确进行；可选的 `payload` 会作为恢复轮上一条 `origin="system"` 的消息被记录，而不是作为唤醒事件本身。投递一个任务并不在等待的事件会抛出 `NotResumableError`。

每个动词都返回一个带三个字段的 `DriveOutcome`：`task_id`、`status`（这一轮尘埃落定后 fold 出的任务状态）和 `wake_handle`（任务现在所等待的 `HumanResponseReceived` 句柄，或 `None`）。调用方正是靠这个句柄来区分一次例行的下一目标挂起和一次在等审批的挂起：

```python
outcome = client.start(goal="Refactor utils.py")
print(outcome.status, outcome.wake_handle)
# → suspended approval-call_7c21

if outcome.wake_handle == f"approval-{call_id}":
    outcome = client.approve(outcome.task_id, call_id=call_id)

client.send_goal(outcome.task_id, goal="Now add a test for it.")
```

一次被门控的工具调用挂在 `approval-{call_id}` 上；被门控的 `finish` 或 spawn 则分别用 `approval-finish-{task_id}` / `approval-spawn-{task_id}`。请从 `client.events(task_id)` 里的 `ToolCallApprovalRequested` 事件上读取 `call_id`，而不要去解析这个句柄。

## seed / drive 拆分

一个异步传输层不应该为了跑完一整轮而占住一个请求线程。`seed_*` 在请求线程上完成每一个持久的、经过校验的步骤——因此一个类型化的拒绝（`ModelSelectorError`、`NotResumableError`）仍然会同步地表现为一个 4xx——并返回一个 `SeededTurn`，再由你来驱动它。

| 方法 | 签名 |
| --- | --- |
| `seed_start` | 同 `start` |
| `seed_send_goal` | 同 `send_goal` |
| `seed_approve` / `seed_deny` | 同 `approve` / `deny` |
| `seed_answer` | 同 `answer` |
| `seed_deliver_event` | 同 `deliver_event` |
| `drive_seeded` | `(seeded) -> DriveOutcome` —— **在当前线程上**把 seed 好的这一轮驱动到它的下一个边界 |
| `dispatch_seeded` | `(seeded) -> None` —— 交给常驻 worker 池，立刻返回 |

按「谁该阻塞」来选。`drive_seeded` 在调用线程上跑完这一轮，适合你自己拥有的后台线程；`dispatch_seeded` 把 seed 的租约交回就绪队列，由[常驻 worker](#常驻-worker-池) 接手并立即返回 —— 这是 HTTP 处理器想要的形状，因为持久的 seed 已经让这次 ack 具备崩溃安全性。两种方式下，进度都通过已提交的事件流体现。

## 常驻 worker 池

有 worker 在跑时，`dispatch_seeded` 会把 seed 的租约交回就绪队列，而不是另起一个一次性线程，因此多场对话可以并发推进。唤醒投递依然是持久、单 worker、恰好一次的：一个租约持有一个任务，直到它的下一次挂起或终止。

| 方法 | 签名 |
| --- | --- |
| `start_workers` | `(num_workers=1, *, poll_interval=0.1, heartbeat_interval=30.0, stale_sweep_interval=10.0, timer_poll_interval=1.0, lease_seconds=600.0, shutdown_grace_s=10.0)`；被调用两次会抛出 `RuntimeError` |
| `stop_workers` | `(timeout=None) -> bool` —— 有 worker 未按时退出时返回 `False`；这个池仍被跟踪着，因此重试一次就能收尾 |

```python
client.start_workers(4)
print(client.workers_running)   # → True
...
print(client.stop_workers(timeout=30))   # → True
```

要让 worker 跑在自己的进程里，请直接使用那个库原语——见 [WorkerLoop](worker-loop.md)。

## 对话生命周期

| 方法 | 签名与效果 |
| --- | --- |
| `cancel` | `(task_id, *, reason="cancelled", cascade=False)` —— 杀掉这场对话；它变为终止 |
| `interrupt` | `(task_id, *, reason=None, interrupted_by="user")` —— 让进行中的这一轮在下一个边界停下，任务停在它的下一目标挂起上，因此 `send_goal` 直接就能继续；对并发驱动中的一轮是线程安全的 |
| `close` | `(task_id, *, closed_by="user", reason=None)` —— 归档它 |
| `reopen` | `(task_id, *, reopened_by="user", reason=None)` |
| `rewind` | `(task_id, *, message_seq)` —— 重定基到 `message_seq` 处那条用户消息之前：那条消息、它的输出以及之后的每一轮都成为死历史（日志仍然只追加），被撤销的那一段所编辑的工作区文件会被还原 |
| `fork` | `(task_id, *, message_seq)` —— 同一个锚点，相反的保留方式：铸出一个**新**任务，继承到那个边界为止的历史，源任务原封不动。返回的 `DriveOutcome.task_id` 是这个 fork 的。仅限根任务；两个分支共享同一个工作区 |

`NEXT_GOAL_WAKE_HANDLE` 是一场对话在两轮之间停靠的那个唤醒句柄。宿主的会话停止接缝就是靠这个常量来识别那次收尾的下一目标挂起的。

## 检查与存储

纯读——没有外部 IO，对任务没有影响。

| 方法 | 返回 |
| --- | --- |
| `events(task_id)` | `list[EventEnvelope]` |
| `messages(task_id)` | `list[ViewItem]` —— fold 出的人类视图 |
| `task_answer(task_id)` | 最近一轮的终态答案，取**原值**，从该轮落到的那个生命周期事件上读（`TaskCompleted`；多轮对话里一轮干完挂起则是 `TaskSuspended`）。最近一轮没有答案时为 `None`。要拿值就用它——`output_schema` 的答案在这里是 `dict`，而 `messages()` 为了逐字稿会经 `str()` 渲染 |
| `events_after(task_id, after_seq=None)` | 严格位于某个游标之后的流 |
| `task_streams()` | 每条被驱动过的流一个 `TaskStreamSummary`，携带 `task_id` 和 `last_seq` |
| `delete_task(task_id)` | `{"ok", "task_id", "deleted": [...], "reason"?}`；会以 `reason="running"` 或 `"not_found"` 拒绝 |
| `get_content(content_hash)` | `bytes \| None` |
| `put_content(body, *, media_type)` | `ContentRef` |
| `memory_root(task_id=None)` | `Path` —— 这个任务在多租户链下解析到的那个存储 |
| `subscribe(callback)` | 一个取消订阅的可调用对象；提交后的信封，覆盖所有任务 |
| `add_sandbox_lifecycle_listener(on_allocate, on_release)` | 为容器跟踪型副作用准备的产品接线；没有 sandbox 时是空操作 |
| `shutdown()` | 幂等：停掉 worker、拆除 observer 与 trace sink、释放 sandbox |

## 记忆整理

三个宿主可调用的入口负责策展长期记忆存储。它们都从 `noeta.sdk` 导出，定义在 `client/consolidation.py`。

| 函数 | 效果 |
| --- | --- |
| `run_consolidation(client, *, memory_root, now=None, debounce=True, debounce_hours=24.0, max_root_tasks=10, max_chars_per_root_task=16000, include_task=None, on_seeded=None) -> bool` | 入队一次后台运行；当且仅当真的入队了才返回 `True`。防抖未到期和无内容可摘要这两种情况都返回 `False`，且不抛异常 |
| `consolidation_due(memory_root, *, now, debounce_hours=24.0) -> bool` | 单独的防抖那一半 |
| `build_consolidation_digest(client, *, since=None, max_root_tasks=10, max_chars_per_root_task=16000, include_task=None) -> str \| None` | 单独的摘要那一半，供自己编排整理流程的宿主使用 |

## 错误

边界代码应该按**结构**匹配错误——`isinstance(exc, CodedError)` 加上 `exc.code`——而绝不要按消息文本。`CodedError` 是基类（`noeta/protocols/errors.py`）。

| 错误 | `code` | 由谁抛出 |
| --- | --- | --- |
| `QueryFailedError` —— 携带 `task_id`、`status`、`reason`、`retryable` | `query_failed` | `QueryResult.answer()` |
| `ModelSelectorError` | `model_selector_rejected` | 轮次驱动器，在 seed 时 |
| `ProviderSelectorError` | `provider_selector_rejected` | 轮次驱动器，在 seed 时 |
| `NotResumableError` | `not_resumable` | `deliver_event`，以及对一个接不了目标的任务调用 `send_goal` |
| `TaskAlreadyTerminalError` | `task_already_terminal` | 对一个已结束任务调用任何动词 |
| `UnknownTaskError` —— 带 `task_id`、`verb`、`reason` | `unknown_task` | 对不指向任何活流的 id 调用 `cancel` / `interrupt` / `close` / `reopen`。在该动词写入**之前**就拒掉，所以一个打错的 id 不可能造出一条以控制事件为创世的流 |
| `UnsupportedSubtaskSuspend` | `unsupported_subtask_suspend` | 子任务排空 |

## 下一步

- [Options](sdk-options.md) —— 这些动词所运行的那份配方
- [类型与测试](sdk-types.md) —— `EventEnvelope`、`ViewItem`、`FakeLLMProvider`
- [WorkerLoop](worker-loop.md) —— 在独立进程里跑一个排空循环
- [唤醒与恢复](../concepts/wake-resume.md) —— 两轮之间发生了什么
