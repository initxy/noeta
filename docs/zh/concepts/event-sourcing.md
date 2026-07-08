# 事件溯源：state = fold(log)

Noeta 不把"当前状态"作为事实来源存储。一个任务的事实来源是它的只追加 **EventLog**；你在任何时刻想要的状态，都是从开头对该日志进行 fold 的结果：

> 当前状态 = fold(从创建到现在的所有事件)

状态对象是一个可丢弃的投影；日志才是主副本。Noeta 所宣称的一切——持久性、崩溃恢复、重放、审计——都是这一决策的结果，而非在其旁边额外构建的功能。

## EventLog

每个 Task 拥有一条只追加的 `EventEnvelope` 记录流。每次状态变更都会发出一个信封：`TaskCreated`、`MessagesAppended`、`LLMRequestStarted`、`ToolCallStarted`、`TaskSuspended`、`TaskWoken`、`TaskCompleted` 等等。不存在一个 Engine 额外读取的"任务表"——日志是唯一的事实来源。

一个信封携带所属任务、事件类型、类型化 payload，以及一个单调递增的序列号。序列号由日志在写入时分配，而非由调用者指定，这保证了每条流的重放顺序是确定的：fold 就是"按序列号升序将每个 payload 喂给其处理器"。

## 大内容存放在日志旁边

信封 payload 的上限为 4 KB。任何更大的内容——完整的 LLM 请求/响应体、大型工具输出——都会进入 **ContentStore**，一个按内容寻址、按哈希去重的 blob 存储；信封只携带一个 `ContentRef(hash, size, media_type)`。即使是快照，也是一个普通事件，其 payload 是一个引用。日志始终保持为一串小记录，"日志是唯一事实来源"这一原则从不动摇。

## 单写者不变式

只有当所有状态变更都必须先经过日志时，fold 才能承诺"重放日志能精确还原运行过程"。Noeta 通过将任务状态切分为四个切片来强制执行这一点——对话流、Policy 的长时记忆、上下文计划，以及治理计数器——并将每个切片固定给恰好一个写者。值得注意的是，Policy 不能直接写入自己的记忆：它将状态补丁附加到返回的 Decision 上，Engine 将其作为事件落地，然后由 fold 写回。完整的切片-写者对应关系见[架构概览](../architecture/overview.md)。

## 为什么这很重要

- **构造上即持久** —— 在任务执行中途杀掉进程，fold 能将任务完整恢复。不存在一个可能被遗漏的单独"保存"步骤。
- **可复现** —— 同一条日志在任何进程、任何机器上 fold 出字节级相同的状态（见 [Fold & 快照](fold-and-snapshot.md)）。
- **一种机制，多种用途** —— 恢复任务、在 UI 中展示任务、事后审计任务，全都是同一个操作：一次 fold。

相关：[任务模型](task-model.md) ·
[Fold & 快照](fold-and-snapshot.md) ·
[Composer & cache](composer-and-cache.md)
