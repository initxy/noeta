# 引擎与执行

Engine 是一个**无状态的步骤驱动器**：`run_one_step(task, lease_id=…)` 将 Task 恰好推进一个 Policy 决策，然后返回。它在调用之间不持有任务状态——每一步都从对 EventLog 的一次全新 fold 开始（见[事件溯源](event-sourcing.md)）。

<p align="center">
  <img src="../../assets/turn-sequence.svg" alt="任务执行的一轮——目标提交、租约、步骤循环、完成，通过 SSE 流式传输" width="820">
  <br>
  <em>通过内置代理完成一整轮：提交 → 租用 → 步骤循环 → 完成。步骤循环的每次迭代就是一次 <code>run_one_step</code>。</em>
</p>

## 一步：组合 → 决策 → 分发

1. **组合（Compose）。** ContextComposer 从 fold 后的状态组装出 View——模型将看到的确切输入——一个 `ContextPlanComposed` 信封记录了这一步是由什么构建的（见 [Composer & cache](composer-and-cache.md)）。
2. **决策（Decide）。** Policy 读取 View 并返回一个类型化的 `Decision`。Policy 是一个纯函数：它不发出事件、不接触存储、没有写入权限——它只陈述一个立场。生产环境的 Policy 是 ReAct；确定性的桩 Policy 用于测试。
3. **分发（Dispatch）。** Engine 根据 Decision 类型路由，并将其效果——工具调用、LLM 往返、子任务派生、挂起、终止——作为信封通过经过租约验证的 EventLog 落地。

Guard 在这条热路径上运行，可以在动作发生前否决它（见 [Guard 与 Observer](guard-observer.md)）。

## Decision 词汇表

Policy 使用一套小而中立的词汇——`ToolCallsDecision`、`SpawnSubtaskDecision`、`YieldForHumanDecision`、`WaitTimerDecision`、`FinishDecision`、`FailDecision`，以及循环继续的写入如状态补丁和压缩请求。Engine 将每个 Decision 路由到三个目的地之一：

| 路由 | Decision | 发生什么 |
| --- | --- | --- |
| 继续 | 工具调用、状态补丁、压缩 | 发出事件，不挂起，运行下一步 |
| 挂起 | 派生子任务、等待人类、等待定时器 | 释放执行权，等待被唤醒 |
| 终止 | 完成、失败 | 写入一个快照和一个终止事件；Task 结束 |

将"陈述立场"（Policy）与"记入账本"（Engine）分开，是从执行侧看到的单写者不变式：决策权是开放的——你可以替换成自己的 Policy——而记录权保持封闭，所以即使行为不当的 Policy 也无法破坏事实来源。

## Engine 保持的边界

Engine 对 Worker、Dispatcher 或 HTTP 一无所知——它将一个 Task 推进一步就停止。它被刻意保持得很小：控制流只路由 Decision，将实际工作委托给外围处理器。取消是协作式的——Engine 在组合和决策之间的安全点探测停止请求，而不是中断线程。

相关：[任务模型](task-model.md) ·
[唤醒与恢复](wake-resume.md) ·
[架构概览](../architecture/overview.md)
