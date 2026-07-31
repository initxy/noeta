# 引擎与执行

**Engine** 是 Noeta 中真正推动 Task 前进的那一部分。你把一个 Task 交给它，它一直运行到这个 Task 必须等待某件事、或者已经结束为止，然后返回。它在两次调用之间不在内存里留下任何东西：每一次调用都从一个刚刚从 EventLog fold 出来的 Task 开始（见[事件溯源](event-sourcing.md)）。

唯一的动词是 `run_one_step(task, lease_id=…)`。"一步"的意思是*直到下一次挂起或终止为止*——而不是一次模型往返。一个跑了十次工具调用、三轮模型对话的步骤，仍然只是一次 `run_one_step`。

<p align="center">
  <img src="../../assets/diagrams/engine-execution.svg" alt="引擎执行——compose → decide → dispatch，tool_calls 就地循环" width="820">
</p>

## 这个循环：compose → decide → dispatch

在一次调用内部，Engine 重复三个阶段：

1. **组装（Compose）。** **ContextComposer** 从 fold 出的状态组装出 `View`——模型将要看到的确切输入——Engine 记录一个 `ContextPlanComposed` 信封，指明这一轮是由什么构建的（见 [Composer 与缓存](composer-and-cache.md)）。这在循环的每一圈发生一次，因此一个长步骤会组装很多次，而每一次事后都可审计。

2. **决策（Decide）。** **Policy** 读取 `View` 并返回一个类型化的 `Decision`。Policy 是一个纯函数：它不发出事件、不接触存储，在任何地方都没有写权限。它只陈述一个立场。`ReActPolicy` 是默认项；测试中由一个确定性的桩 Policy 顶替它。

3. **分发（Dispatch）。** Engine 按决策类型路由，并把它的效果——工具调用、LLM 往返、子任务派生、挂起、终止——作为信封，通过经过租约验证的 EventLog 落地。

一个 `Decision` 就是一个小 dataclass。最常见的那个携带模型请求的调用：

```python
ToolCallsDecision(
    calls=[ToolCall(tool_name="read", arguments={"path": "README.md"},
                    call_id="call_1")],
)
```

这个决策让循环继续转下去：Engine 运行工具、追加结果、重新组装，然后再问 Policy 一次。只有一个退出型决策才结束这次调用。

## 一轮在宿主里的位置

往外看一层，下面是宿主视角下的一整轮——一个目标进来，一个 Worker 租用这个 Task，上面那个步骤循环跑起来，一个答案回出去：

<p align="center">
  <img src="../../assets/diagrams/turn-sequence.svg" alt="一轮——宿主代码 → Client → Engine → Provider → 工具 → EventLog，以及返回路径" width="820">
</p>

租约与释放之间的一切，都是一次 `run_one_step` 调用。

## Decision 词汇表

Policy 说的是一套小而刻意中立的词汇，Engine 把每个决策路由到三个目的地之一：

| 路由 | Decision | 发生什么 |
| --- | --- | --- |
| **继续** | `ToolCallsDecision`、`StatePatchDecision`、`CompactionRequestedDecision`、后台的 `SpawnSubtaskDecision` | 发出事件，不挂起，循环回到 compose → decide |
| **挂起** | 前台的 `SpawnSubtaskDecision`、`SpawnSubtasksDecision`、`YieldForHumanDecision`、`WaitTimerDecision`、`WaitExternalDecision` | 写入一个快照，发出 `TaskSuspended`，释放执行权并等待被唤醒 |
| **终止** | `FinishDecision`、`FailDecision` | 写入一个快照和一个终止事件；Task 结束 |

这套词汇的中立是刻意的：其中没有任何一个变体在指称某个产品功能。更新一个待办清单是一次 `state_patch`，向用户提问是一次 `yield_for_human`，调用一个 skill 也是一次 `state_patch`。Engine 不给这些负载赋予任何含义——翻译工作由贡献了那个 control tool 的 built-in 完成。

## 两个处在分界线上的决策

- 一个 `background=True` 的 `SpawnSubtaskDecision`：当接好了后台启动器时会让本轮继续，否则在一道栅栏上挂起。
- 一个 `ToolCallsDecision` 通常会继续，但一个要求审批的 Guard 会当场把它变成一次挂起（见 [Guard 与 Observer](guard-observer.md)）。被拦下的调用会先被记录，因此恢复时能把它精确重建出来。

把"陈述立场"（Policy）与"记入账本"（Engine）分开，就是从执行侧看到的单写者不变式。*决策*的权利是一个开放的扩展点——你可以换上自己的 Policy——而*记录*的权利保持封闭，因此即使一个行为不当的 Policy 也无法破坏事实来源。

## Engine 守住的边界

Engine 对 Worker、Dispatcher 或任何传输一无所知。它把一个 Task 推进一步就停下。它的控制流只负责路由决策；真正的工作住在逐决策的处理器里。

出问题时有两个细节要紧：

- **取消是协作式的。** 一个可选的 `cancelled` 谓词会在每一圈的开头被轮询，并在 Policy 决策后立即再轮询一次，因此一次落在往返中途的取消会放弃那个结果，而不是中断一个线程。它的粒度因此是轮次边界。
- **长步骤仍然会留下恢复点。** 一个从不让出的 Policy 否则会什么都不留给恢复用，所以 Engine 每连续 20 个工具调用轮次就写一个快照（`CONSECUTIVE_TOOL_CALLS_SNAPSHOT_THRESHOLD`）。

主循环本身是**封闭的**——它不是一个扩展面。开放的是它周围的一切：Policy、工具、Guard、Observer，以及贡献它们的插件。

## 下一步

- [唤醒与恢复](wake-resume.md) —— 一个挂起型决策之后会发生什么。
- [Composer 与上下文缓存](composer-and-cache.md) —— 组装阶段究竟构建了什么。
- [Worker loop](../reference/worker-loop.md) —— 租用 Task 并调用 Engine 的那个组件。
- [扩展平面](../architecture/extension-planes.md) —— 循环周围哪些部分是你可以替换的。
