# 事件溯源：state = fold(log)

大多数系统会把一个任务的当前状态放在某处——一行记录、一个文档、一个 blob——然后就地更新它。Noeta 反其道而行。发生在一个 Task 身上的一切都被追加到这个 Task 自己的 **EventLog** 里，而你想要的状态则在你索取时从日志重新算出。日志是母本；状态对象只是一个可以随手丢掉再重建的一次性投影。

这个重建步骤叫做 **fold**：

> state = fold(the Task's events)

<p align="center">
  <img src="../../assets/diagrams/event-sourcing.svg" alt="事件溯源——事件追加进 EventLog，大体积正文进入 ContentStore，fold 重建四个状态切片" width="820">
</p>

持久性、崩溃恢复、重放与审计，全都是这一个决定的推论——而不是在它旁边加装的功能。

## 日志里到底有什么

每个 Task 拥有一条只追加的记录流。下面是一条典型流的开头，一行一条记录：

| `seq` | 类型 | 记录了什么 |
| --- | --- | --- |
| 1 | `TaskCreated` | 不可变的头部——目标、agent 名、父任务 |
| 2 | `MessagesAppended` | 用户的消息 |
| 3 | `ContextPlanComposed` | 一个指向"模型究竟被展示了什么"的引用 |
| 4 | `LLMRequestFinished` | 模型的回复及其 token 用量 |
| 5 | `ToolCallStarted` | `read(path="README.md")` |
| 6 | `MessagesAppended` | 工具的结果，回到对话中 |
| … | | 循环继续 |
| 41 | `TaskSuspended` | 这个 Task 在等待某件事 |
| 42 | `TaskWoken` | 它等的东西到了 |
| 58 | `TaskCompleted` | 最终答案 |

Engine 不会去读什么单独的任务表。如果它不在这条流上，它就没有发生过。

## 一条记录长什么样

每条记录都是一个 `EventEnvelope`。除了带类型的负载之外，它还携带 `seq`（在流中的位置）、`type`、`actor`（谁写的）、`trace_id`、`causation_id` 和 `origin`——一个标记，指明追加它的那个*角色*，取值为 `engine` / `llm` / `observer` / `tool` / `system` 之一。

`seq` 由日志在追加时自行分配，调用方交进来的只是一个占位值。这正是每条流都拥有唯一确定的重放顺序的原因。

fold 沿着这个顺序前进，把每个信封路由到为其类型注册的处理器。这张处理器表被刻意做成非穷尽的：遇到不认识的类型会被记录并跳过，而不是抛错，因此一条由更新版本的生产者写出的流，在更旧的读取方那里依然能 fold 出来。

## 大体积正文放在日志旁边

信封负载被限制在 4 KB（`EVENT_PAYLOAD_MAX_BYTES`）；超限的写入会抛出 `PayloadTooLarge`。更大的东西进入 **ContentStore**——一个按 SHA-256 去重的内容寻址 blob 存储——信封里只携带一个指向它的 `ContentRef(hash, size, media_type)`。

一个完整的 LLM 请求体、一份很大的工具输出、一份压缩摘要、一个超长的目标——它们走的都是这条路。快照也一样：它就是一个普通事件，负载只是一个 `state_ref`。日志始终是一串小记录，而"日志是唯一的事实来源"这句话继续成立。

## 每个切片只有一个写者

只有当任何状态变更都必须先经过日志时，fold 才敢承诺"重放日志得到的就是实际运行过的东西"。Noeta 的做法是把 Task 状态切成四个带类型的切片，并把每个切片钉死在恰好一个写者上——切片本身见[任务模型](task-model.md)，完整的对应表见[状态与写入者](../architecture/state-and-writers.md)。

Policy 是最清楚的例子。它是决定代理下一步做什么的组件，而它*不能*给自己的记忆切片赋值。它只能把一个 `TaskStatePatch` 附加到它返回的 Decision 上，由 Engine 把它落成一个 `TaskStatePatched` 事件，再由 fold 回写。"想改变状态"和"有权记录它"是两种不同的权利，握在两个不同的组件手里。

## 这样做换来了什么

- **构造上即持久。** 在任务中途把进程杀掉，一次 fold 就能把 Task 原样带回来。不存在任何人可能忘记的单独"保存"步骤。
- **可复现。** 同一条日志在任何机器上的任何进程里都 fold 出字节级相同的状态——见 [Fold 与快照](fold-and-snapshot.md)。
- **一种机制，多种用途。** 恢复一个崩溃的 Task、在 UI 里渲染它、一个月后审计它，都是同一个操作：一次 fold。
- **没有东西被抹除。** 更正是新事件，而不是编辑。哪怕是一次回退，也只是追加一个标记来指明新的基线；旧记录仍然留在流上。

## 下一步

- [任务模型](task-model.md) —— Task 是什么，以及 fold 写入的那四个状态切片。
- [Fold 与快照](fold-and-snapshot.md) —— fold 函数本身，以及快照如何让它保持快速。
- [状态与写入者](../architecture/state-and-writers.md) —— 切片与写者的对应表，以及带版本的 fold 规则。
- [SDK 类型](../reference/sdk-types.md) —— 你从 `Client.events` 和 `Client.messages` 拿回来的事件与消息类型。
