# 事件溯源：state = fold(log)

Noeta 不把"当前状态"作为事实来源存储。一个 Task 的事实来源是它的只追加 **EventLog**；你在任何时刻想要的状态，都是对该日志进行 fold 的结果：

> state = fold(该 Task 的所有事件)

状态对象是一个可丢弃的投影；日志才是主副本。Noeta 所宣称的一切——持久性、崩溃恢复、重放、审计——都是这一个决策的结果，而非在它旁边额外构建的功能。

## EventLog

每个 Task 拥有一条只追加的 `EventEnvelope` 记录流。每次状态变更都会发出一个信封：`TaskCreated`、`MessagesAppended`、`ContextPlanComposed`、`ToolCallStarted`、`LLMRequestFinished`、`TaskSuspended`、`TaskWoken`、`TaskCompleted` 等等。不存在一张 Engine 额外读取的任务表。

一个信封携带所属任务、事件类型、类型化 payload、一个单调递增的 `seq`、一个 trace id，以及一个指明写入者角色的 `origin` 标记（`engine`、`llm`、`observer`、`tool`、`system`）。`seq` 由日志在写入时分配——调用者交给它的是一个占位值——因此每条流恰好有一个确定的重放顺序。

fold 沿着这个顺序遍历，把每个信封路由到为其类型注册的处理器。处理器表被刻意做成不完备的：一个无法识别的事件类型会被记录并跳过，而不是抛出异常，因此一条由"认识比读取方更多事件类型的生产者"写出的流，仍然能被 fold。

## 大内容存放在日志旁边

信封 payload 的上限是 4 KB（`EVENT_PAYLOAD_MAX_BYTES`）；超过上限的写入会抛出 `PayloadTooLarge`。任何更大的内容——一个完整的 LLM 请求/响应体、一个大型工具输出、一个压缩摘要、一个超大的目标——都会进入 **ContentStore**，一个按内容寻址、以 SHA-256 去重的 blob 存储；信封只携带一个 `ContentRef(hash, size, media_type)`。即使是快照，也是一个普通事件，其 payload 是一个 `state_ref`。日志始终保持为一串小记录，"日志是唯一事实来源"这一原则也就成立。

## 单写者不变式

只有当没有任何状态变更绕过日志抢先发生时，fold 才能承诺"重放日志能精确还原运行过程"。Noeta 通过把 Task 状态切成四个类型化切片来强制这一点——滚动对话流、Policy 的长时记忆、组合出的上下文切片，以及治理计数器——并把每个切片钉死给恰好一个写者。尤其值得注意的是，Policy 不能给自己的记忆赋值：它把一个 `TaskStatePatch` 附加到它返回的 Decision 上，Engine 把它作为一个 `TaskStatePatched` 事件落地，然后由 fold 写回。完整的切片-写者对应关系见[架构概览](../architecture/overview.md)。

## 为什么这很重要

- **构造上即持久** —— 在任务执行中途杀掉进程，fold 能把任务原样带回来。不存在一个可能被遗漏的单独"保存"步骤。
- **可复现** —— 同一条日志在任何机器上的任何进程里都 fold 出字节级相同的状态（见 [Fold 与快照](fold-and-snapshot.md)）。
- **一种机制，多种用途** —— 恢复一个任务、在 UI 中展示它、事后审计它，全都是同一个操作：一次 fold。

相关：[任务模型](task-model.md) ·
[Fold 与快照](fold-and-snapshot.md) ·
[Composer 与缓存](composer-and-cache.md)
