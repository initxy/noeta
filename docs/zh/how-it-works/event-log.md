# 事件日志

Noeta 从不保存 Task 的「当前状态」。Task 上发生的每件事都追加到它自己的 **EventLog** 里，需要状态时再从日志算出来：

> 状态 = fold(这个 Task 的全部事件)

日志才是正本，状态对象用完就可以扔。崩溃恢复、重放、审计，做的都是同一件事：fold。

<NtEventLog lang="zh" />

## 保证什么

- **重放逐字节一致。** 同一份日志在任何进程、任何机器上 fold 出的状态逐字节相同。fold 只读日志和内容存储，不看时钟、不用随机数、不联网、不调模型。
- **不改旧记录。** 纠错、回退、压缩都是追加新事件，原来的记录一直留在日志里。
- **每个 Task 只有一个写入方。** 每次追加都要出示 worker 的租约；租约已被收回的 worker 写不进去。
- **快照只是加速。** 把所有快照删掉，行为不变，只是慢一些。

## 日志里有什么

一条典型的事件流，一行一条：

| `seq` | 类型 | 记录的内容 |
| --- | --- | --- |
| 1 | `TaskCreated` | 目标、agent 名、父任务 |
| 2 | `MessagesAppended` | 用户的消息 |
| 3 | `ContextPlanComposed` | 指向这次给模型看的完整内容 |
| 4 | `LLMRequestFinished` | 模型回复和 token 用量 |
| 5 | `ToolCallStarted` | 比如 `Read(file_path="README.md")` |
| … | | 循环继续 |
| 41 | `TaskSuspended` | Task 开始等待 |
| 42 | `TaskWoken` | 等的东西到了 |
| 58 | `TaskCompleted` | 最终回答 |

每条记录是一个 `EventEnvelope`，带有 `seq`（追加时由日志分配）、`type`、`actor`、`origin`（`engine` / `llm` / `observer` / `tool` / `system`）和一份很小的 payload。payload 上限 4 KB（`EVENT_PAYLOAD_MAX_BYTES`）；更大的东西——完整的模型响应、大段工具输出、快照内容——放进按内容寻址的 **ContentStore**，事件里只存一个指向它的 `ContentRef`。

## 四块状态，只有 fold 能改

Task 的状态拆成四块，只有 fold 会改它们，所以任何改动都必须先留下事件：

| 状态块 | 改动从哪来 | 内容 |
| --- | --- | --- |
| `RuntimeState` | Engine 记下的事件 | 对话消息、每轮用量 |
| `TaskState` | policy，只能通过决策里附带的 `TaskStatePatch` | 目标、阶段、待办、当前加载的内容 |
| `ContextState` | Engine 记下的上下文组装结果 | 上下文记录的引用、压缩摘要、内容插入位置 |
| `GovernanceState` | 从整条事件流累计出来 | 花费、轮数和 token 计数、子任务结果 |

policy 决定要改什么，但由 Engine 记成 `TaskStatePatched` 事件，再由 fold 写回。「决定」和「记录」是两种权限，分别在两个组件手里。

## 崩溃后怎么恢复

<NtRecovery lang="zh" />

1. worker A 执行到一半被杀，心跳停了，租约过期。
2. 过期检查把 Task 放回就绪队列。
3. worker B 拿到租约，fold 日志——这本来就是每一步开头都要做的事。
4. B 写一条 `StepAttemptAbandoned`，把中断的那次尝试标记作废。如果那次尝试里的每个工具调用都不用审批就能通过 guard，就自动重跑这一步——重跑可能把已经执行过的调用再执行一遍。只要有调用需要审批或会被拒绝、那次尝试派出过子任务，或者用到了不认识的工具，就把 Task 停住，等人来继续。连续作废三次一定会停住，崩溃循环不会无限重试。

除此之外没有别的恢复逻辑，因为从来就没有什么需要「保存」的东西。这套保证的边界见[已知限制](../operations/limitations.md)。

## 快照

日志很长时，从头重放会变慢，所以 fold 也可以从最新的**基线**事件恢复，只重放它之后的部分：

| 基线事件 | 什么时候写 |
| --- | --- |
| `TaskSnapshot` | 每次挂起和结束前，以及连续 20 轮工具调用后 |
| `TaskRewound` | 对话回退到更早的一轮 |
| `StepAttemptAbandoned` | 把一次中断的尝试标记作废 |
| `TaskForked` | 从这个 Task 分出一个新 Task |

测试会用 `ignore_snapshots=True` 强制从头 fold，检查两条路径得到的字节完全一样。如果某个快照缺少当前 fold 需要的字段，就丢掉它从头重放：慢一点，但不会错。同样的规则保证半年前挂起的 Task 用今天的代码照样能 fold。

## 对你意味着什么

- 用 `Client.events` 读 Task 的完整历史，用 `Client.messages` 读消息；恢复时用的就是这些数据。
- 能读到存储的进程就能恢复任何 Task，所以扩容只是换存储，不用改代码。
- 想*改变*行为的钩子应该放在 policy 或 guard 里，不能放在 observer 里，见 [Engine](engine.md)。

设计记录：
[event-sourced truth](https://github.com/initxy/noeta/blob/main/docs/adr/event-sourced-truth.md) ·
[single-writer invariant](https://github.com/initxy/noeta/blob/main/docs/adr/single-writer-invariant.md) ·
[step-attempt recovery](https://github.com/initxy/noeta/blob/main/docs/adr/step-attempt-recovery.md)

## 下一步

- [任务与唤醒](tasks-and-waking.md)：Task 怎么等待，又怎么只恢复一次。
- [Engine](engine.md)：事件是谁写的。
- [部署](../guides/deploy.md)：运行负责恢复的 worker。
