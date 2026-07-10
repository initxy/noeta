# 修复:resident 多 worker 路径下子任务被 multi-turn wrapper 套住导致死锁

## Goal

让 `resolve_engine` 对子任务(depth > 0 / 有 `parent_task_id`)不再套 `MultiTurnReActPolicy(final=False)`,
使被空闲 worker 抢答认领的子任务跑完时正常发出 `TaskCompleted`,而非挂起在
`noeta-code-next-goal` 上;并让同一竞态下的 `__workflow__` child 路由到
`OrchestrationPolicy` engine 而非 `UnknownAgentError`。

## Non-goals

- 不重构 drain / worker 认领竞态架构(不强行让子任务"只走 drain")。worker 抢答子任务是既有
  容错路径(`run_leased_task` 已为此做了 goal-seeding 防御),保留。
- 不改 `CodeSessionRunner` 进程内 CLI 路径(它走 drain + `set_turn_final`,本就不受影响)。
- 不动 `multi_turn_policy_wrapper` 本身,也不动 `MultiTurnReActPolicy` 的语义。包装只该套顶层会话,
  这个职责划分不变,变的是"谁来决定套不套"。

## Context

### 根因

`MultiTurnReActPolicy(final=False)` 把 `FinishDecision` 偷换成
`YieldForHumanDecision(prompt=NEXT_GOAL_WAKE_HANDLE)`(`multi_turn.py:81-91`),让任务
挂起"等下一条消息"而非真正结束。这个包装**只该套顶层会话**(支持 `noeta code chat` 多轮对话)。

子任务是一次性跑腿的,跑完应 `TaskCompleted`。驱动子任务有两条路径:

1. **drain 路径(正确)**:`_build_subtask_engine` 显式 `policy_wrapper=None`(`resolver.py:795`),
   注释明写"children are one-shot, never multi-turn wrapped"。父任务 step 后在 `_settle_subtasks`
   (`worker.py:1761`)同步驱动子任务。
2. **worker 抢答路径(bug)**:多 worker(`num_workers ≥ 2`,`lifecycle.py:119`)或多进程共 dispatcher 时,
   子任务入就绪队列,空闲 worker 的 `tick()` 抢先 non-targeted lease 到它,经
   `run_leased_task → resolve_engine → _engine_for_agent` 驱动。`_engine_for_agent` **无条件**套
   `self.policy_wrapper`(`resolver.py:1018`),对子任务没有任何 depth/parent 守卫。于是子任务跑完被
   包装拦下挂起,不发 `TaskCompleted`。

`ChildLifecycleObserver` 只在 `TaskCompleted`/`TaskFailed`/`TaskCancelled` 时唤醒父任务
(`observers.py:93-136`),对 `TaskSuspended` 无反应。父任务挂在 `SubtaskGroupCompleted` 屏障上
永不被唤醒 → 死锁。单 worker 不触发,因为父任务 step 后在**同一线程**同步 drain,没有第二个 worker 来抢。

### 同一根因的第二个症状

`resolve_engine`(`resolver.py:367-444`)只对 `"unnamed"` 特殊分支,**没有 `WORKFLOW_AGENT_NAME`
分支**;而 drain 的 `_build_subtask_engine`(`resolver.py:763`)有。同一多 worker 竞态下,
`__workflow__` child 被 worker 认领会走 `resolve_engine → _lookup_agent("__workflow__")` →
`UnknownAgentError`。

### 一个必须一起改的坑:engine 缓存

`_engine_for_agent` 的缓存键(`resolver.py:979-982`)是
`(agent_name, model, ask_user_question_enabled, workspace, provider, permission_mode,
 mcp_aliases, effort, exec_env_ref)`——**不含"是否子任务 / 是否套 wrapper"维度**。

若 root 与子任务同 agent+model 且 `ask_user_question` 同值(explorer 子 agent 通常 `ask=False`,
root 也可能 `ask=False`),会命中同一缓存条目。先建的一方决定内容:root engine 带 wrapper,
会泄漏给子任务,使"子任务不套 wrapper"的修复被缓存掩盖。drain 路径靠"子任务走 uncached 的
`_build_engine` 直调"(注释 `resolver.py:813-815`)绕开了缓存;`resolve_engine` 走的是
`_engine_for_agent`(带缓存),没有这个绕开。

### 对照:`ask_user_question` 已有同样的 depth 掩码

`resolve_engine` 已经对子任务关掉 `ask_user_question_enabled`(`resolver.py:432-436`:depth>0 / 有
parent 时强制 `False`),且 `ask` 是缓存键的一维。这正是 wrapper 修复要照搬的模式。

## Decisions

1. **修复点在 `resolve_engine`,不在 `_engine_for_agent`。** 与 `ask_user_question` 掩码同构:在
   `resolve_engine` 计算出"这是子任务"后,把 `policy_wrapper=None` 透传下去。`_engine_for_agent`
   增加一个 `policy_wrapper` 形参(默认 `self.policy_wrapper`),让这一处覆盖生效,其余调用方不动。

   *理由*:把"子任务身份决定套不套 wrapper"的判断收在 `resolve_engine`(它本来就 hold 着 task +
   已经在做 depth 判断),而不是在 `_engine_for_agent`(它只有 agent,没有 task 父子关系)。与既有
   `ask_user_question` 掩码同一位置,可读、可测、改动最小。

2. **子任务判定依据:`parent_task_id is not None`**,与现有 `ask_user_question` 掩码的判定
   (`resolver.py:434-435`:`getattr(task, "parent_task_id", None) is not None and
   subtask_depth == 0`)完全一致。不引入新判据。

3. **`__workflow__` 路由:在 `resolve_engine` 开头加分支**,与 drain 的 `_build_subtask_engine`
   (`resolver.py:763`)一致——`agent_name_of(...) == WORKFLOW_AGENT_NAME` 时调
   `_build_orchestration_engine(task_id, allowed_subtask_agents=...)`。`allowed_subtask_agents` 取
   父任务根 agent 的 spawnable 集(与 `_build_drain_host` 的 `inherited_subtasks` 同源),workflow
   child 本身不再递归,可传继承集或空集——取与 drain 一致的最小实现。

   *理由*:同一竞态同一根因,顺手堵掉;`_build_orchestration_engine` 是已有 abstract seam,
   SdkHost 已有实现,worker 路径可直接复用,无需新代码路径。

4. **缓存键加 subtask 维度。** `_engine_for_agent` 的 cache key 末尾追加一维 `is_subtask: bool`
   (或等价的 `policy_wrapper is None` 标志),使"带 wrapper 的 root engine"与"不带 wrapper 的子任务
   engine"即便其余维度全同也不共享条目。

   *理由*:不改这个,decision 1 的修复在 root/子任务同 agent+model 时被缓存掩盖。给 key 加维是
   最小、与既有 9 维 key 风格一致的改法。

5. **不改 drain 路径。** `_build_subtask_engine` 已经 `policy_wrapper=None`,行为正确,不动。

## Implementation plan

### 1. `_engine_for_agent` 增加 `policy_wrapper` 形参

`resolver.py:890`。签名增加 `policy_wrapper: Optional[Callable[[Policy], Policy]] = None`,
内部用 `effective_wrapper = policy_wrapper if policy_wrapper is not None else self.policy_wrapper`,
传给 `_build_engine`(`resolver.py:1018`)。

cache key(`resolver.py:979-982`)追加第 10 维:`effective_wrapper is None`(bool)——以
"wrapper 是否 None"而非 wrapper 对象本身入键(对象不可 hash),且语义正是"这是不是子任务 engine"。

### 2. `resolve_engine` 对子任务传 `policy_wrapper=None`

`resolver.py:367-444`。复用 `ask_user_question` 掩码已算出的 `is_subtask`(或同等判定),
在两处 `_engine_for_agent` 调用(`unnamed` 分支 line 413、主分支 line 429)传入
`policy_wrapper=None` 当 `is_subtask` 时,否则不传(默认 `self.policy_wrapper`)。

### 3. `resolve_engine` 开头加 `__workflow__` 分支

`resolve_engine`(`resolver.py:386` 附近,`name = agent_name_of(...)` 之后):若
`name == WORKFLOW_AGENT_NAME`,直接 `return self._build_orchestration_engine(
task_id, allowed_subtask_agents=<继承集>)`,与 drain `_build_subtask_engine`(`resolver.py:763-766`)
一致。注意 `_build_orchestration_engine` 返回的 engine 不经缓存(它本就是 uncached 直建,与 drain
一致),无需动 cache。

### 4. 不改 worker / drain / multi_turn

`worker.py`、`subtask_drain.py`、`multi_turn.py`、`driver.py` 均不动。

## Task breakdown

- **T1** `_engine_for_agent` 加 `policy_wrapper` 形参 + cache key 加 `wrapper is None` 维。
  (前置:无)
- **T2** `resolve_engine` 子任务传 `policy_wrapper=None`。依赖 T1(用到新形参)。
- **T3** `resolve_engine` 开头加 `__workflow__` 分支。可与 T2 并行(同文件不同位置,但都改
  `resolve_engine`,实际串行更安全)。
- **T4** 测试三层(见 Acceptance)。依赖 T1–T3。

T1 → (T2, T3 串行) → T4。

## Dependencies / sequencing

T1 是底座(T2 依赖其新形参)。T2、T3 都改 `resolve_engine`,串行避免冲突。T4 最后。无外部依赖。

## Acceptance criteria

1. **多 worker 子任务完成(行为)**:`num_workers ≥ 2` 的部署下,主 agent 派出的 explorer 子任务
   跑完时发出 `TaskCompleted`(事件流可见),**不是** `TaskSuspended(wake_on=HumanResponseReceived(
   handle="noeta-code-next-goal"))`。父任务被 `SubtaskGroupCompleted` 唤醒并走到 `terminal`。
   ——复现原死锁场景,断言不再挂起。
2. **workflow 路由(行为)**:`num_workers ≥ 2` 下,`__workflow__` child 被 worker 认领时,
   `resolve_engine` 路由到 `OrchestrationPolicy` engine 并正常驱动,**不**抛 `UnknownAgentError`。
3. **缓存隔离(单测)**:构造 root 与子任务同 agent+model+workspace 且 `ask_user_question` 同值的
   场景,断言 `resolve_engine(root_task)` 返回的 engine 带有 `MultiTurnReActPolicy` 包装,
   `resolve_engine(child_task)` 返回的 engine **不**带包装,且二者不共享同一缓存条目(可用
   `_engines` 的 key 集合或 wrapper 类型断言)。
4. **回归**:单 worker(`num_workers=1`)路径行为不变;drain 路径(进程内 `CodeSessionRunner`)
   行为不变;既有 multi-turn / delegation / workflow 测试全绿。

## Risks

- **缓存键扩维的兼容性**:key 从 9 维变 10 维。缓存是进程内 LRU(`_MAX_CACHED_ENGINES`),重启即空,
  无持久化兼容问题。但若存在直接断言 key 形状的测试,需同步更新。
- **`__workflow__` 分支的 `allowed_subtask_agents` 取值**:drain 里取根 agent 的 spawnable 集
  (`inherited_subtasks`)。`resolve_engine` 拿到的是子任务 task,要回溯到根 agent 取集——需确认
  `_build_drain_host` 的取集逻辑能否在 `resolve_engine` 复用,或退化为空集(workflow child 是否
  允许再 spawn 取决于 `run_workflow` 语义)。实现时核实;若 workflow child 不再递归 spawn,空集即可,
  与 drain 行为等价则取继承集。
- **判定边界**:子任务判定用 `parent_task_id is not None`。需确认 background subagent
  (`spawn_subagent(background=True)`)是否也经 `resolve_engine`——若它走独立 driver 路径
  (background-subagent ADR),不受影响;若也经 `resolve_engine`,套不套 wrapper 对它同样应是
  "不套"(background 子任务也是一次性)。实现时核实 background 路径,确保结论一致。

## Files / areas to inspect

- `packages/noeta-runtime/noeta/execution/resolver.py` — `resolve_engine`(:367)、
  `_engine_for_agent`(:890)、cache key(:979)、`_build_subtask_engine`(:758,对照)、
  `_build_orchestration_engine`(:252,abstract seam)、`_build_drain_host`(:666,
  `inherited_subtasks` 取集逻辑)。
- `packages/noeta-runtime/noeta/execution/multi_turn.py` — `MultiTurnReActPolicy`(语义,不改)。
- `packages/noeta-runtime/noeta/core/observers.py` — `ChildLifecycleObserver`(为何挂起不唤醒,
  不改,理解死锁)。
- `packages/noeta-runtime/noeta/runtime/worker.py` — `run_leased_task`(:688)、子任务 goal-seeding
  (:800,worker 抢答证据)、`_settle_subtasks`(:1811)。
- `packages/noeta-sdk/noeta/client/client.py:250` — `policy_wrapper` wiring(确认 resident 非 None)。
- `apps/noeta-agent/noeta/agent/backend/lifecycle.py:119` — `num_workers` 默认值。
- `docs/adr/engine-policy-dataflow.md` — Decision / single-writer 边界(约束参考)。
- `docs/adr/subtask-fanout-and-durable-wake.md`、`docs/adr/worker-lease-model.md` — 竞态与唤醒背景。
- 测试:`tests/test_code_multi_turn.py`、delegation / subtask 相关测试(回归基线)。
