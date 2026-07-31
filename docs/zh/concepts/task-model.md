# 任务模型

一个 **Task** 就是代理的一次运行，也是 Noeta 唯一的工作单元。一场持续数周的对话是一个 Task。一个每晚跑一次的作业是一个 Task。你委派去做一小块调研的子代理也是一个 Task。它们底下没有 session 对象、没有工作流实例，也没有另一种对话类型。

Task 本身很小：一个 `task_id`、一个 `status`、当有东西派生它时的一个 `parent_task_id`，以及一个在创建时就固定的 `subtask_depth`。其余的一切——它的历史、它的记忆、它的计数器——都按需从它自己的 EventLog fold 出来（见[事件溯源](event-sourcing.md)）。

<p align="center">
  <img src="../../assets/diagrams/task-lifecycle.svg" alt="任务生命周期——pending → running → suspended（四种唤醒条件）→ terminal" width="820">
</p>

## 四种状态

`status` 恰好是四个值之一，上图就是完整的状态机：

- **`pending`** —— 已创建，或在一次唤醒后重新入队，等待一个 Worker 把它取走。
- **`running`** —— 一个 Worker 持有 Lease，Engine 正在逐步推进这个 Task（见[引擎与执行](engine-execution.md)）。
- **`suspended`** —— Task 交出了执行权，正在等待。*所有*等待都是这一个状态，加上记录在 `wake_on` 上的一个类型化 `WakeCondition`，无论它等的是一个子任务、一个人、一个定时器还是一个外部信号（见[唤醒与恢复](wake-resume.md)）。
- **`terminal`** —— Task 结束了。`TaskCompleted`、`TaskFailed` 或 `TaskCancelled` 关闭这条流，并在完成和失败这两个出口之前写入一个快照。

把所有种类的等待都收拢成一个状态，换来的回报是：只有一条恢复路径需要编写、测试和推敲——而不是四条。

## 四个状态切片，各有唯一写者

一个 Task 的可变状态被切成四个带类型的切片。这样切分，正是为了让每一块都能拥有唯一的写者：

| 切片 | 持有 | 写者 |
| --- | --- | --- |
| `RuntimeState` | 滚动的消息日志和上一轮的 token 用量 | Engine |
| `TaskState` | 长视野记忆——目标、阶段、todos、决策、活跃内容 | Policy，经由 `state_patch` |
| `ContextState` | 最新的 context plan、压缩摘要、每轮的 thinking | fold |
| `GovernanceState` | fold 出的计数器——成本、迭代次数、拒绝、子任务结果 | fold |

每个切片都由 `fold` 从这个 Task 自己流上的事件回写，而 Engine 是那条流上唯一的发射者。一个想改动 `TaskState` 的 Policy 会把一个 `TaskStatePatch` 附加到它返回的决策上；Engine 把它落成一个事件，再由 fold 施加它。正是这层间接，让 `fold(events)` 等于实际运行过的状态。

`TaskState` 是最值得记住名字的那个切片。它是一个长视野代理存放"目前为止已经搞清楚了什么"的地方，也是"能连续工作数小时的代理"与"只回答一个问题的代理"之间最主要的结构差别。

## 父与子

一个 Task 可以派生 **Subtask**。一个 Subtask 在结构上与它的父任务完全相同——自己的 EventLog、自己的 fold、自己的生命周期——只通过 `parent_task_id` 关联，外加一个 `subtask_depth`，由预算给它设上限，让委派无法无限递归。

所以"多代理"并不是一个单独的功能。父任务在派生后挂起，每个子任务作为普通 Task 运行，每个子任务的结果作为一个唤醒事件回来。整棵树都可以仅从事件重建，而每个节点都独立于其他节点恢复。

一点词汇说明：一棵委派树的根是 `root_task_id`，它是那些寿命超过单个 Step 的东西的生命周期所有者——后台 shell、后台子代理、一个 sandbox 容器。

## Task 不是什么

- **不是 Session。** 一场多轮对话就是一个 Task 反复接收输入。每一轮是一次"唤醒 → 若干步 → 挂起"的循环，两轮之间 Task 停在 `suspended`，带着一个 `HumanResponseReceived` 条件。想要一个用户可见的"session"的宿主，自己去构建那个概念。
- **不是 Workflow 实例。** 一个固定流程就是一个确定性 Policy 加上若干派生决策；一个临场编排脚本就是一个 Task，由它的 Policy 来解释。不存在工作流引擎，也不存在工作流原语。
- **不是 Agent。** 一个 **Agent** 是一个具名的、可派生的配置——prompt、工具、插件、预算——即 Task 所实例化的那个"类"。一个 Agent 可以被许多 Task 实例化。它不携带任何可调用对象，也不是一个运行时实体。

## 下一步

- [引擎与执行](engine-execution.md) —— 一个 Task 如何从 `pending` 走到它的下一次挂起。
- [唤醒与恢复](wake-resume.md) —— 在 `suspended` 状态里会发生什么。
- [生成子代理](../how-to/spawn-subagents.md) —— 父与子的实操版本。
- [SDK Options](../reference/sdk-options.md) —— 那些会编译成一个 Agent 的 `Options` 字段。
