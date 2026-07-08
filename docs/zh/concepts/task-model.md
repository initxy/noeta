# 任务模型

Noeta 运行的一切都是一个 **Task**——不存在与之并列的 Session、Run、Job 或 Workflow。一个 Task 是一个可寻址的代理工作单元：它有一个 `task_id`、一个 `status`，以及当它由另一个 Task 派生时的 `parent_task_id`。它的完整状态按需从自己的 EventLog fold 得出；Engine 从不在多次运行之间将任务状态保存在内存中（见[事件溯源](event-sourcing.md)）。

## 生命周期

<p align="center">
  <img src="../../assets/task-lifecycle.svg" alt="任务生命周期——统一的挂起、唤醒事件和终止退出" width="820">
  <br>
  <em>所有等待都是一个 <code>suspended</code> 状态加上一个类型化的唤醒条件；一个唤醒事件将 Task 重新入队，等待下一次租约。</em>
</p>

一个 Task 经历四种状态：

- **`pending`** —— 已创建（或重新入队），等待 Worker 租用它。
- **`running`** —— 一个 Worker 持有租约，Engine 正在逐步推进 Task（见[引擎与执行](engine-execution.md)）。
- **`suspended`** —— Task 释放了执行权，正在等待。所有等待——子任务完成、人类回答、定时器触发——都是这一个状态加上一个描述等待内容的类型化 `WakeCondition`（见[唤醒与恢复](wake-resume.md)）。
- **终止态** —— completed、failed 或 cancelled。一个快照和一个终止事件关闭这条流。

## 父与子

一个 Task 可以派生子任务（Subtask）。子任务在结构上与其父任务完全相同——自己的 EventLog、自己的 fold、自己的生命周期——仅通过 `parent_task_id` 关联。因此 Noeta 中的"多代理"就是许多个 Task：父任务在派生后挂起，结果作为唤醒事件流回给它。整棵树可以仅从事件重建，每个节点独立恢复。

## Task 不是什么

- **不是 Session。** 多轮对话就是一个 Task 反复接收用户输入：每一轮是一个 唤醒 → 几步执行 → 挂起 的循环，Task 在轮次之间停留在 `suspended` 状态。
- **不是 Workflow 实例。** 固定流程是一个确定性 Policy 加上子任务派生——不存在单独的工作流引擎或工作流原语。
- **不是 Agent。** Agent 是一个具名的、可派生的配置——prompt、工具、能力——即 Task 的"类"。一个 Agent 可以被许多 Task 实例化。

相关：[事件溯源](event-sourcing.md) ·
[唤醒与恢复](wake-resume.md) ·
[引擎与执行](engine-execution.md)
