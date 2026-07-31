# 任务模型

Noeta 运行的一切都是一个 **Task**。一个 Task 是一个可寻址的代理工作单元：一个 `task_id`、一个 `status`、当另一个 Task 派生它时的一个 `parent_task_id`，以及一个在创建时就固定的 `subtask_depth`。它的完整状态按需从自己的 EventLog fold 得出；Engine 不在多个 Step 之间保留任何任务状态（见[事件溯源](event-sourcing.md)）。

## 四个切片，一个写者

一个 Task 的可变状态被切成四个带类型的切片：

| 切片 | 持有 |
| --- | --- |
| `RuntimeState` | 滚动的消息日志和上一轮的 token 用量 |
| `TaskState` | Policy 的长视野记忆——目标、todos、决策、活跃内容 |
| `ContextState` | 最新的 context plan、压缩摘要、被保留在消息流之外的 thinking block |
| `GovernanceState` | fold 出的计数器——成本、迭代次数、拒绝、子任务结果、审批、绑定 |

每个切片都由 `fold` 从 Task 自己流上的事件回写，而 Engine 是那条流上唯一的发射者。一个想改动 `TaskState` 的 Policy 会把一个 `TaskStatePatch` 附加到它返回的 Decision 上；Engine 把它落成一个事件，再由 fold 施加它。这正是让 `fold(events)` 等于实际运行过的状态的原因。

## 生命周期

<p align="center">
  <img src="../../assets/task-lifecycle.svg" alt="任务生命周期——统一的挂起、唤醒事件和终止退出" width="820">
  <br>
  <em>所有等待都是一个 <code>suspended</code> 状态加上一个类型化的唤醒条件；一个唤醒事件将 Task 重新入队，等待下一次租约。</em>
</p>

`status` 是四个值之一：

- **`pending`** —— 已创建（或重新入队），等待一个 Worker 租用它。
- **`running`** —— 一个 Worker 持有 Lease，Engine 正在逐步推进 Task（见[引擎与执行](engine-execution.md)）。
- **`suspended`** —— Task 释放了执行权，正在等待。所有等待——一个子任务完成、一个人类回答、一个定时器触发、一个外部信号——都是这一个状态加上 `wake_on` 上的一个类型化 `WakeCondition`（见[唤醒与恢复](wake-resume.md)）。
- **`terminal`** —— Task 结束了。`TaskCompleted`、`TaskFailed` 或 `TaskCancelled` 关闭这条流，并在完成和失败出口之前写入一个快照。

## 父与子

一个 Task 可以派生 Subtask。一个 Subtask 在结构上与它的父任务完全相同——自己的 EventLog、自己的 fold、自己的生命周期——仅通过 `parent_task_id` 关联。因此"多代理"不过就是许多个 Task：父任务在派生后挂起，每个子任务的结果作为一个唤醒事件流回来。整棵树可以仅从事件重建，每个节点独立恢复。

## Task 不是什么

- **不是 Session。** 多轮对话就是一个 Task 反复接收输入：每一轮是一个 唤醒 → 几步 → 挂起 的循环，Task 在轮次之间停留在 `suspended`。
- **不是 Workflow 实例。** 一个编排脚本是一个 Task，由它的 Policy 解释；它派发的每个助手都是一个真正的 Subtask。不存在单独的工作流引擎，也不存在工作流原语。
- **不是 Agent。** 一个 Agent 是一个具名的、可派生的配置——prompt、工具、plugin——即一个 Task 的"类"。一个 Agent 可以被许多 Task 实例化。

相关：[事件溯源](event-sourcing.md) ·
[唤醒与恢复](wake-resume.md) ·
[引擎与执行](engine-execution.md)
