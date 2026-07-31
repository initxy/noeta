# 引擎与执行

Engine 是一个**无状态的步骤驱动器**：`run_one_step(task, lease_id=…)` 把 Task 推进到它的下一个**挂起或终止**，然后返回。它在调用之间不持有任务状态——由调用者交给它一个从 EventLog 全新 fold 出来的 Task（见[事件溯源](event-sourcing.md)）。

这里的"一步"是一个*轮次边界*，不是一次模型往返。在同一次调用内部，Engine 会持续循环：组装一个 View，询问 Policy，落地 Decision 的效果，并——对每一个让循环继续的 Decision——再转一圈。只有一个退出型 Decision 才结束这次调用，把 Task 转到 `terminal` 或 `suspended`。一个跑了十次工具调用的步骤，仍然只是一次 `run_one_step`。

<p align="center">
  <img src="../../assets/turn-sequence.svg" alt="任务执行的一轮——目标提交、租约、步骤循环、完成，流式传输到宿主 UI" width="820">
  <br>
  <em>通过一个嵌入宿主完成一整轮：提交 → 租用 → 步骤循环 → 完成。整个步骤循环都跑在一次 <code>run_one_step</code> 调用里。</em>
</p>

## 一轮：组合 → 决策 → 分发

1. **组合（Compose）。** ContextComposer 从 fold 后的状态组装出 View——模型将看到的确切输入——Engine 记录一个 `ContextPlanComposed` 信封，指明这一轮是由什么构建的（见 [Composer 与缓存](composer-and-cache.md)）。这在循环的每一轮发生一次。
2. **决策（Decide）。** Policy 读取 View 并返回一个类型化的 `Decision`。Policy 是一个纯函数：它不发出事件、不接触存储、没有写入权限——它只陈述一个立场。`ReActPolicy` 是默认项；确定性的桩 Policy 在测试中顶替它。
3. **分发（Dispatch）。** Engine 根据 Decision 类型路由，并把它的效果——工具调用、LLM 往返、子任务派生、挂起、终止——作为信封通过经过租约验证的 EventLog 落地。

Guard 在这条热路径上运行，可以在一个动作发生前否决它（见 [Guard 与 Observer](guard-observer.md)）。

## Decision 词汇表

Policy 说的是一套小而中立的词汇，Engine 把每个 Decision 路由到三个目的地之一：

| 路由 | Decision | 发生什么 |
| --- | --- | --- |
| 继续 | `ToolCallsDecision`、`StatePatchDecision`、`CompactionRequestedDecision`、后台的 `SpawnSubtaskDecision` | 发出事件，不挂起，循环回到 compose → decide |
| 挂起 | 前台的 `SpawnSubtaskDecision`、`SpawnSubtasksDecision`、`YieldForHumanDecision`、`WaitTimerDecision`、`WaitExternalDecision` | 写入一个快照，发出 `TaskSuspended`，释放执行权并等待被唤醒 |
| 终止 | `FinishDecision`、`FailDecision` | 写入一个快照和一个终止事件；Task 结束 |

有两个 Decision 处在分界线上。一个 `background=True` 的 `SpawnSubtaskDecision`：当接好了后台启动器时会让本轮继续，否则在一道栅栏上挂起。一个 `ToolCallsDecision` 通常会继续，但一个要求审批的 Guard 会当场把它变成一次挂起——被拦下的调用会先被记录，因此恢复时能把它精确重建出来。

把"陈述立场"（Policy）与"记入账本"（Engine）分开，是从执行侧看到的单写者不变式：决策权是开放的——你可以换上自己的 Policy——而记录权保持封闭，因此即使一个行为不当的 Policy 也无法破坏事实来源。

## Engine 守住的边界

Engine 对 Worker、Dispatcher 或任何传输一无所知——它把一个 Task 推进一步就停止。它被刻意保持得很小：控制流只负责路由 Decision，把实际工作委托给逐 Decision 的处理器。取消是协作式的：一个可选的 `cancelled` 谓词会在每一轮的开头被轮询，并在 Policy 决策后立即再轮询一次，因此一个落在往返中途的取消会放弃那个结果，而不是中断一个线程。而一个从不让出的 Policy 仍然能拿到恢复点——Engine 每连续 20 个工具调用轮次就写一个快照。

相关：[任务模型](task-model.md) ·
[唤醒与恢复](wake-resume.md) ·
[架构概览](../architecture/overview.md)
