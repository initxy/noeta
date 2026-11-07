# 单写者与崩溃恢复

任何一个工人都可能在任何一台机器上跑某个任务，而工人会死、会卡住、会断网。本页讲 noeta 怎样保证一个任务永远只有一个工人在写，以及下一个工人怎样处理上一个留下的半截工作：一次模型调用、一次工具调用、一个插件的事件处理。

## 只有一个写入者

工人跑一个任务之前，先从存储里**认领**它：

- `claim(worker, queues, lease_seconds)` 取一个可认领的任务，置为 `running`，记下这个工人是持有者，**把任务的 epoch 加一**，并设一个租约（`lease_until`）。返回的 `reclaimed` 说明上一个认领是不是还挂着租约、没有交还（[反复把工人弄死的任务](#a-task-that-keeps-killing-its-worker)）。
- `renew(claim, lease_seconds)` 延长租约。工人每过三分之一租期续一次（默认租期 30 秒；`noeta worker --lease`）。
- `release(claim)` 交还认领但不结束任务（工人停机时）：running 的任务变回 `ready`。
- `requeue(root_id, queue, to)` 把一整棵任务树从一个队列挪到另一个，但只要树里有任务的租约还没过期就不挪（服务端接手前端开的对话时用，见 [HTTP 接口](../reference/http-api.md#和任务交互)）。

可认领的任务是：`ready`（且到点了）、`waiting` 且 `ready_at` 已到、或者 `running` / 保留着认领但租约已经过期（`noeta/core/store.py`，`Store.claim`）。

之后每次追加都带两个条件，由存储在写入的同一个事务里检查：

1. 认领的 **epoch** 仍然等于任务的 `owner_epoch`，并且认领还有租约；
2. 日志的最后一个序号等于工人给出的 **`expected_seq`**（它最后看到的那个）。

任何一条不满足，存储就抛出 `Conflict`，什么都不写。工人立刻停下，不再写任何东西。

<Diagram name="core-claim-epochs" />

所以租约只管时机。租约决定的是：一个没了声音的工人，它的任务什么时候可以被别人接手。这是一种猜测，猜错了（那个工人只是慢）也只是多一次重试，绝不会写两遍。真正起隔离作用的是 epoch。一个卡住的工人（长时间 GC 停顿、`SIGSTOP`、网络分区）醒来时，如果任务已经被别的工人认领，它手里拿的是旧 epoch；它第一次追加就会被拒绝，然后停下。“只有一个写入者”是存储强制保证的性质，不是一个卡住的进程可能打破的约定（[ADR-001](https://github.com/initxy/noeta/blob/main/docs/adr/001-task-is-an-event-log-with-one-writer.md)）。

两种存储怎样让这些检查不受并发干扰：

- **Postgres**（`noeta/backends/stores/postgres.py`）。追加先锁住任务那一行（`SELECT ... FOR UPDATE`），再检查 epoch 和序号；对这个任务的其他追加、认领、续租、交还、投递都要等锁，拿到锁时看到的已经是前一个的结果。认领用 `FOR UPDATE SKIP LOCKED` 选行，所以两个工人永远不会拿到同一个任务，也不会互相等；认领用的索引只包含可以被认领的行，闲着等待的任务根本不会被扫到。租约时间用数据库的时钟，所以时钟有偏差的工人对“谁持有租约”的看法也一致。死锁（一次追加往一个正在反向投递的任务投递）会让其中一个事务整体回滚，然后连同检查一起重跑。
- **sqlite**（`noeta/backends/stores/sqlite.py`）。每次写入是一个 `BEGIN IMMEDIATE` 事务，检查都在事务里做，所以同一台机器上的多个进程可以共用一个文件。

存储的契约测试（`noeta/testing/store_contract.py`）要求任何存储都遵守这些规则：两个抢着写的工人不可能都追加成功；租约过期的任务会被重新认领、原来的持有者写不进去；保留认领的等待会一直跟着它的认领。

### 工人丢了认领时

| 发生了什么 | 工人怎么做 |
|---|---|
| 续租返回空（认领被别人拿走了） | 标记认领已丢，取消正在进行的这一步；反正下一次写入也会失败。 |
| 续租一直失败（存储连不上） | 一直重试，直到按它自己的时钟租约该到期了，然后同样放弃这个任务。 |
| 追加抛出 `Conflict` | 停下，不再写任何东西。 |
| 干活时存储连不上（`StoreUnavailable`） | 停下。租约过期后，等存储恢复，任务会被重新认领。 |
| 正在 `COMMIT` 时连接断了（`CommitUnknown`） | 从不盲目重试：工人当作认领已丢停下来，下一个跑这个任务的人读日志就知道什么写进去了。如果断在认领时，工人会去找存储里记着归它、而它自己却没在跑的任务，直接接过来，而不是干等租约过期。 |
| 工人停机 | `release` 立刻把每个 running 的任务交还成 `ready`；保留认领在等待的任务继续等待（[保留认领](./waiting.md#kept-claims)）。 |

工人丢了认领，也就丢了它为这个任务保留的东西：它保留的认领都会被标记为已丢，它名下的后台任务随之停掉。

## 恢复就是折叠日志

没有单独的恢复流程。下一个认领任务的工人像每次认领一样折叠日志，`next_action` 看日志里有什么就做什么。可能做到一半的东西有两种：模型调用和工具调用。

<Diagram name="core-tool-recovery" />

### 模型调用做到一半

`ModelCalled` 在请求发出前就追加了。调用过程中崩溃，日志里就有一条没有对应 `ModelReplied` 的 `ModelCalled`。这一轮还开着，所以下一个认领者直接再调用一次模型。请求是从同一条日志重新组装的，字节一样，provider 的提示词缓存照样有效。流式输出到一半的文字从没记下来，也不需要。

如果某次调用每次都把工人搞死，就会死循环。`TaskState.model_attempts` 数的是没有回复的 `ModelCalled`；到 3 次，这一轮以 `llm_error: the model call kept being interrupted` 失败（交互式任务接着等下一条消息，一次性任务以 `failed` 结束）。一次调用内部对 provider 错误的重试是另一回事，不计入这里（[模型这一步](./one-step.md#when-the-provider-fails)）。

### 工具调用做到一半 {#a-tool-call-cut-short}

`ToolStarted` 在工具执行前就追加了。调用过程中崩溃，日志里就有一条没有对应 `ToolFinished` 的 `ToolStarted`：工具可能全做了、做了一部分、或者什么都没做。下一个认领者的 `next_action` 返回 `Interrupted`，由 `recover_calls`（`noeta/core/runner/calls.py`）对每个这样的调用按下面的顺序决定：

1. **工具自己去查结果。**工具可以定义 `recover(input, ctx)`。它拿到的 `ctx.idempotency_key` 和当初那次调用的一样，都是 `<任务 id>:<调用 id>`，在哪个工人上都一样；查得到结果就返回结果，查不到返回 `None`。
2. **工具可以安全重跑。**`ToolFlags` 声明了 `retry_safe` 的工具（再跑一次不会改变调用之外的任何东西：`Read`、`WebFetch`、`WebSearch`、`ToolSearch`、MCP 的资源读取工具、待办工具……）会重新开始，总共最多开始 `MAX_ATTEMPTS`（3）次，免得一个会把工人搞死的调用被无限重跑。
3. **否则结果未知。**这次调用得到一个报错结果，告诉模型：它在结果被记下之前就被打断了，所以结果未知——可能完全生效、部分生效、或者没生效，再跑之前先核实一下效果（原文是英文："Interrupted before its result was recorded, so its outcome is unknown …"）。模型看得到文件、能跑命令，可以自己去核实。

恢复的调用不再过关卡：它们执行时已经被放行过，日志里记的也是实际执行的那个调用（改写之后的）。停下来等待的调用不算“做到一半”：它在等答复，答复来了就接着做。

因此，除非工具主动选择，副作用**最多发生一次**。这是安全的默认：盲目重跑一次 `rm`、一笔付款、一次部署，比让模型自己看一眼糟糕得多。这个键是给能做得更好的工具用的：

- 把 `ctx.idempotency_key` 交给能丢弃重复请求的外部系统（比如 HTTP 的 `Idempotency-Key` 头）。MCP 插件每次 `tools/call` 都把它放在 `_meta` 里发出去。
- 往别的任务投递时把它当去重键：`SubagentHandback` 的 `recover` 就是直接再跑一次，因为它的报告是以 `subtasks:handback:<键>` 为去重键投递给父任务的，重复的那份会被丢掉。
- 或者做清理：`Agent` 工具的 `recover` 会删掉某次启动留下的 worktree（启动本身和调用结果写在一起，所以做到一半的调用并没有真的启动子代理）。

子任务天生就是安全的：工具创建的子任务和它的 `ToolFinished`（或停下来等待）在同一次追加里写入，子任务 id 由调用推出来（`<任务>.<调用>.<n>`）。在这次追加之前崩溃，就没有子任务；之后崩溃，结果已经记下了。

### 插件的 hook 和它们的记录

插件在 hook 里写的记录和投递，和它们所属的事件一起追加：工具的记录和它的 `ToolFinished` 一起，插件入队的通知和决定发它的那条记录一起（`TaskView.enqueue`）。崩溃时要么都在，要么都不在。

`on_event` 在事件提交之后才运行，所以崩溃可能让某次调用丢掉。承受不起的插件在自己的状态里记一个进度，从 `delivered_through(view)` 返回它；之后每次认领都会把进度之后的事件再交给它一次，于是每条事件它至少能看到一次（它的副作用应当按事件序号做成幂等的）。`Waiting` 要等它的所有 `on_event` 都跑完、记录都落地之后才交还任务，所以中间崩溃的话，任务会在租约过期后被重新认领，而这条等待不会被写两遍。

### 反复把工人弄死的任务 {#a-task-that-keeps-killing-its-worker}

上面的上限都是按单次调用算的。一次运行也可能在别处把工人弄死：某个插件 hook 把内存吃光，组装请求时某个原生库段错误，模型看到“结果未知”后又去调那条会杀掉进程的命令，或者卡死到续不上租约。这样每次接手都会再崩一次；工人有好几个的话，这个任务会把它们一个接一个拖垮。

认领时存储会告诉这次认领：上一个认领有没有交还。行上还挂着租约（没人 release，也没有哪次追加把任务交回来），`Claim.reclaimed` 就是 true，这是在认领自己的事务里读的。停机时的 release 不算。真正要跑这个任务的工人（在插件检查之后，所以把任务留给别人的工人什么都不写）会在插件的 `on_start` 之前记一条 `core/reclaimed`。计数在日志里，所以是下一个工人写的，绝不靠那个正在死掉的工人；这次追加和别的追加一样受认领 epoch 约束，两个工人抢同一个任务，不会都记上。

折叠时，这些记录累加在 `TaskState.reclaims` 里，直到任务**在某条记录之后做完一步**：最近一条 `reclaimed` 之后记下了一次模型回复，而且它的调用全都有了结果（没有调用的回复当场就算做完）。把崩溃时做到一半的那个调用补完不算，因为它是崩溃之前开始的。停下来等待也不清零：崩溃可能就发生在 `Waiting` 写下之后、认领交还之前。一个健康的任务丢了一次机器，只会记一条 `reclaimed`，到下一步就清零了。

任务最多连续重启 `Runtime.restart_limit` 次（默认 3；`noeta worker --restart-limit`，`0` 表示不限）。再崩一次，下一次接手就不再跑它，把还开着的调用都答上，直接以 `failed` 结束，错误是 `restart_limit: the task's run stopped without handing it back 4 times in a row ...`。这是一条普通的 `TaskEnded`：前端照常显示为失败，子代理的父任务会收到一条失败的 `ChildEnded`。默认值比上面那几道按单次调用算的上限高，所以一次总在中途死掉的模型调用或 `retry_safe` 调用会先被它自己的上限拦下，模型还能接着干。

## 插件版本记录 {#plugin-pins}

任务第一次开始时，以及之后每次接手发现插件有变化时，都会记下它用到的插件：`core/plugins`，每个插件的 `registry.pin`（名字、来源、包版本或文件哈希、参数的哈希）。

缺少日志里记着的某个插件的工人，绝不会在缺这个插件的情况下跑这个任务：那样可能折叠出不同的结果、关卡判得不一样、或者少了一个已经承诺给模型的工具。它什么都不写，等租约过期，好让有这个插件的工人来认领（滚动安装期间就是这样）。在同一个工人上、日志没变的情况下连续 `LOAD_ATTEMPTS`（3）次认领都是这样，它就以 `plugin_missing` 让任务失败，写明缺了哪些插件，而不是让任务永远被踢来踢去（[ADR-023](https://github.com/initxy/noeta/blob/main/docs/adr/023-snapshots-per-fold-version-and-newer-logs-left-to-newer-workers.md)）。

插件**变了**（新版本、参数不同）的话，会重新记一次，任务从下一轮起用新的继续跑；变化在日志里看得见，绝不会悄悄发生。在运行中的系统上重新加载插件，见[插件体系](./plugins.md)和[多机运行](./many-machines.md)。

## 快照与版本

快照的版本是负责折叠的代码的哈希（内核的折叠模块、代理、每个插件的版本记录和源码）。版本不同的工人会忽略它，从头折叠日志；快照可以没有，但绝不会错（[任务就是一条日志](./event-log.md#snapshots)）。

日志里因为是新版本写的、本版本解不开的条目，留给新版本的工人：这次认领什么都不写，等租约过期。因为别的原因读不了的日志，连续 3 次认领之后任务以 `unreadable_log` 失败（[日志读不了的时候](./event-log.md#when-a-log-cannot-be-read)）。

## 每种重试都有上限

恢复不会无限循环。上限都在 `noeta/core/` 里：

| 什么在重复 | 上限 | 之后 |
|---|---|---|
| 一次模型调用内部的 provider 错误 | 共 9 次（`retry.DEFAULT_MAX_ATTEMPTS`），退避最长 30 秒 | 这一轮以 `llm_error` 失败 |
| 工人在模型调用中途死掉 | 3 条没有回复的 `ModelCalled`（`texts.MAX_MODEL_ATTEMPTS`） | 这一轮以 `llm_error` 失败 |
| 工人在 `retry_safe` 工具调用中途死掉 | 开始 3 次（`tool.MAX_ATTEMPTS`） | “结果未知” |
| 运行器或插件 hook 里的 bug | 连续 2 次 | 第一次让这一轮以 `internal_error` 失败，第二次让任务以 `failed` 结束 |
| 读不了的日志，或缺插件 | 同一个工人上连续 3 次认领（`task.LOAD_ATTEMPTS`） | 任务以 `failed` 结束 |
| 输出被截断、空回复 | 各自连续 3 次 | 这一轮以 `llm_truncated` 失败 / 不带答案地结束 |
| 上一个认领没交还就重启，期间一步也没做完 | 连续 3 次（`Runtime.restart_limit`） | 再崩一次，任务就以 `failed`（`restart_limit`）结束 |

## 证明：`make cluster`

本页说的这些，都由 `make cluster`（`tests/cluster/`）端到端地测着。它需要 Docker 或者一个 Postgres 连接串（`NOETA_TEST_POSTGRES_DSN`）。它在同一个 Postgres 存储上起好几个 `noeta worker --lease 1` 进程，用一个照脚本回答的模型（`tests/cluster/scripted`）驱动：带记账副作用的工具调用、后台 `Bash` 任务、子代理、`ScheduleWakeup` 定时器和 cron 任务，以及在这些进行中发来的消息。与此同时，它随机 `SIGKILL` 工人再重启，一次杀掉所有工人，或者把工人 `SIGSTOP` 一个半到三个租期再恢复。跑完之后检查：

- 每个任务都在期限内跑完它的脚本；
- 没有哪个副作用记号被记了两次，日志里显示已完成的调用恰好记了一次；
- 每条发出的消息都被它的任务恰好取走一次；
- 每个后台任务在日志里的结局都是“结束”或“丢失”，没有下落不明的，而且模型恰好被告知一次；
- 日志的不变量都成立（`tests/cluster/invariants.py`）：模型调用和回复一一配对，工具的开始和完成一一配对，没有收件箱条目被取两次；抽样看任务行，epoch 从不倒退，并且只对应一个持有者。

`NOETA_CLUSTER_SEED` 可以重放某一次运行；`NOETA_CLUSTER_TASKS` 和 `NOETA_CLUSTER_SECONDS` 控制规模。它不在 `make check` 里。

## 相关

- [任务就是一条日志](./event-log.md)：恢复所依赖的折叠。
- [等待与唤醒](./waiting.md)：保留认领，以及保留认领的工人死掉时会怎样。
- [多机运行](./many-machines.md)：在 Postgres 上部署工人。
- [排错](../operations/troubleshooting.md)：运维视角下的 `plugin_missing`、`unreadable_log` 等失败。
