# 状态与写入者

一个 Task 的事实基础是它的 append-only 事件日志；它在任意时刻的状态都通过 fold 这份日志计算得出，从不作为一等副本存储。只有当没有任何东西能在不留下事件的情况下改变状态时，这个承诺才成立。

本页讲强制它的三个机制：状态被切成切片、每片恰好一个写入者；无法伪造的作者标记；以及一个仍然读得懂由旧版本代码写下的记录的 fold。

它背后的概念是[事件溯源](../concepts/event-sourcing.md)；本页是让它成真的那套架构。

## 四个切片，各有唯一写入者

如果任何组件都能给状态的任何部分赋值，fold 的重建结果就会不再匹配实际跑过的东西。所以 Task 状态被切成四个带类型的切片（`packages/noeta-runtime/noeta/protocols/task.py`），每片恰好一个写入者：

| 切片 | 唯一写入者 | 内容 |
| --- | --- | --- |
| `RuntimeState` | Engine | 滚动的对话消息、每轮用量、上一次转移、上一次输入 token 计数 |
| `TaskState` | Policy —— 只能经由 Decision 上的 `TaskStatePatch` | 目标、阶段、待办事项、决策记录、活跃内容 |
| `ContextState` | fold | 上下文计划 ref、compaction 摘要、每轮的思考内容、内容锚点 |
| `GovernanceState` | fold | 成本、迭代与 token 计数、拒绝、子任务结果、来源信息 |

最能说明问题的一格是 `TaskState`。Policy 是一个 agent 长程记忆的所在，而 Policy **不能给它赋值**。它把一个 `TaskStatePatch` 附加到它返回的 Decision 上；Engine 把那个落地成一个事件；fold 再把切片写回去。这次写入是一个被记录的事实，而不是一次变更。

`ContextState` 是第二能说明问题的一格。composer 产出上下文计划，但它从不写 `task.context` —— 它把计划体放进 ContentStore，Engine 把得到的 ref 附到一个 `ContextPlanComposed` 信封上，fold 再从那里派生出这个切片。compaction 状态、被剥离的思考块和内容锚点，全都以同样的方式到达，只经由 fold 处理器。

`GovernanceState` 是端到端派生出来的。没有任何东西给它打补丁，这就是为什么一次实时运行和一次恢复后的运行看到的计数完全一致。

有一个 `RuntimeState` 字段值得单独点名：`last_input_tokens` 保存 provider 为最近一次往返所报告的输入 token 数。compaction 的触发把那个真实数字当作基线，只对此后追加的消息做估算 —— 因为纯字符启发式会系统性地低估带缓存、结构化块或图片的提示。

## 两类作者标记

"这是谁写的"被记录在两个不同的层面上，而它们回答的是不同的问题。

**在信封上** —— 每个事件都携带一个 `actor` 和一个 `origin`。`actor` 是一个自由形式的身份字符串（`"engine"`、`"llm"`、`"plugin:environment"`）；`origin` 是一个封闭词汇表 `engine` / `llm` / `observer` / `tool` / `system`，指明是哪个 Noeta *角色*写下了它。像 `AuditObserver` 这样的读取方按 `origin` 分类、按 `actor` 归因。

**在消息上** —— 一个 `Message` 可以携带 `origin`，取 `human` / `system` / `memory` 之一，默认为 `None`，意思是"该角色的天然作者"。它与 `role` 正交：role 说的是这一轮走哪个通道，origin 说的是它是谁写的。一次记忆 recall 和一个用户的提问都是 `role="user"`，只有 `origin` 把它们区分开。

消息 origin **同样是单写入者的**。只有 Engine 的记录路径 `Engine.append_user_message` 可以设置它。Policy 合成的消息，在每一个接收消息的 Decision 接缝上都会被剥掉 origin（`strip_message_origin`），因此 Policy 无法把自己的文本冒充成人类的一轮。在模型或工具输出里伪造的标记只是文本 —— 它永远进不了这个字段。

厂商的标签语法也从不进入账本。Anthropic 适配器在线上把 `system` / `memory` 注入包进 `<system-reminder>`；OpenAI 兼容适配器把它们渲染成对话中间的 system 消息。记录本身保持中立。

## lease 就是那道强制

切片归属是一条设计规则。让它成为机械保证的是 lease。

一个 Worker 持有一个 `Lease(lease_id, task_id, expires_at)` —— 对一个 Task 的排他、由心跳续期的持有 —— 并在每一次 EventLog 追加时出示 `lease_id`。EventLog 在每次 emit 时都会（经由刻意做得很窄的 `LeaseRegistry.is_lease_valid`）询问 Dispatcher，因此**只有持有活跃 lease 的那一方能写某个 Task 的流**。

这就是并发 worker 之所以安全的原因。一个 lease 已被回收的 worker —— 一次长 GC 暂停、一次卡住的 Step —— 会在追加处被拒绝，而不是被允许绕到新一代身后落下一次写入。在 Postgres 上，这个检查是一次事务内的 `FOR SHARE` 行测试，对着数据库时钟求值，从而把每台主机的时钟偏移从裁决中剔除；SQLite 和内存版都是单主机的。

Observer 在每个信封提交之后同步地看到它，跑在写入者线程上，但在写入者锁**之外**，且它们的异常会被吞掉。Observer 是读取方；它永远不可能变成第二个写入者。

## 持久唤醒，一次

挂起与恢复走的是同一套不变式。[唤醒与恢复](../concepts/wake-resume.md)陈述了这条保证；机制是四条规则：

- Dispatcher 通过身份字段投影把一个到来的唤醒事件匹配到某个挂起的 Task，并持久保存这次匹配。投递发生在拿 lease 的时刻，经由 `Lease.wake_event`。
- Worker 把这次唤醒穿进 `engine.note_woken`，后者在 Step 继续之前写下 `TaskWoken(wake_event=…)`。**那次写入就是持久化的落定点。**
- 这次匹配**比 lease 活得更久**。它只会被一次消费性的 `release(consumed_wake_event=…)` 清除。在拿到 lease 与写下 `TaskWoken` 之间崩溃，唤醒仍留在原处；`requeue_stale()` 把 Task 放回就绪，下一次拿 lease 时重新投递同一个唤醒。
- 消费是**幂等的**。Worker 的 woken 分支是一个以最新匹配的 `TaskWoken` 为键的恢复状态机：一次重投递如果其 `TaskWoken` 已经落地，就会被对账，而不是再写一条。

至少一次投递加上幂等消费就是恰好一次，而 lease 围栏让它限定在单个 worker 上。对一个没有排队唤醒的挂起 Task 尝试恢复，会报告带类型的诊断 `suspended_without_wake_event` —— 意思是"在等一件还没发生的事"，而不是一次故障。

Step 中途的崩溃会在下一次拿 lease 时恢复：被中断的 attempt 会被一个 `StepAttemptAbandoned` 标记密封起来，若它没有副作用则重新驱动，否则该 Task 会被停放下来等人处理。范围和尚未闭合的边界都收录在[已知限制](../operations/limitations.md)里。

## fold 由旧代码写下的记录

载荷和切片会演进，但半年前挂起的一个 Task 今天仍然必须能 fold。有三个机制承载这一点，而且值得把每个机制到底做了什么说清楚。

**规范字节与顺序无关。** `to_canonical_bytes` 以排序过的键和紧凑分隔符序列化，因此一个字段在 dataclass 声明里的位置对字节没有任何影响。字段顺序是可读性约定，不是兼容机制。

**跨版本的字节相等来自对 `None` 的显式退出。** 一个 dataclass 声明 `__canonical_omit_none__` 来指名哪些字段在未设置时会从字节流里消失。往这样的类里加一个字段、把它默认为 `None`，于是旧记录（从来没有过这个字段）和当前代码（把它 fold 成默认值）就序列化成相同的字节 —— 因此当时算出的哈希今天仍然对得上。

**恢复时容忍已不存在的键。** `restore_dataclass` 把存储下来的内容体过滤成当前类所声明的那些字段，而不是把未知的键 splat 进一个会抛错的构造函数。它覆盖 snapshot 的 `GovernanceState` —— 那个会不断累积计数的切片 —— 以及事件载荷的恢复路径。

当某个 snapshot 完全早于那些累积字段时，fold 会**丢弃它并从头重放**：更慢，但永不出错。这是一个刻意的默认 —— snapshot 只是一个加速点，而不带 snapshot 的 fold 会重建出同样的状态。

## 接下来去哪

- [包与导入规则](packages.md) —— 这一切之下的导入规则
- [Fold 与快照](../concepts/fold-and-snapshot.md) —— 这个概念及其后果
- [唤醒与恢复](../concepts/wake-resume.md) —— 完整的投递保证
- [已知限制](../operations/limitations.md) —— 恢复在哪里停下
