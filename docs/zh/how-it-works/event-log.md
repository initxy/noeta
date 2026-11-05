# 任务就是一条日志

noeta 的每个任务都是一条只追加的事件日志。本页讲日志里记了什么、任务状态和下一次模型请求怎样从日志算出来，以及快照怎样让这件事变快，同时又绝不变成第二份“真相”。

## 为什么用日志

一个任务可能跑几分钟，也可能跑好几天；它调用有副作用的工具，等人回话，还得扛住崩溃、在进程和机器之间搬家。它的状态总得放在某个地方：进程内存里、原地更新的数据库行里，或者一条日志里。

noeta 放在日志里（[ADR-001](https://github.com/initxy/noeta/blob/main/docs/adr/001-task-is-an-event-log-with-one-writer.md)）：

- 日志里只有少数几种固定的事件类型（`noeta/core/events.py`）。
- 任务状态是日志的**折叠**（fold）结果：按顺序读事件，一条条应用上去。不做 I/O，不看时钟，没有随机数（`noeta/core/loop/state.py`）。
- 下一次模型请求是这个状态的**纯函数**（`noeta/core/loop/request.py`）。
- `tasks` 表里的那一行是日志的投影，每次追加时在同一个事务里一起写。

这样一来，崩溃恢复、换一台机器接着跑、分叉对话、回退、审计、精确复用提示词缓存，全都是同一个操作：折叠日志。

其他方案都有具体的毛病。原地更新的行没有历史，既没法恢复也没法分叉，两个写入者还会悄无声息地互相覆盖。内存里的会话加定期存档，一崩溃就丢掉上次存档之后的一切，还会把副作用再做一遍。让前端直接往日志里追加事件，就没法和工人排出先后，也没法去重；所以前端改成往收件箱投递（[等待与唤醒](./waiting.md#the-inbox)）。

## 十种事件

| 事件 | 什么时候写 |
|---|---|
| `TaskCreated` | 任务创建时（总是 seq 1）：用哪个代理、配置、父任务和根任务，以及接续哪段对话（如果有）。 |
| `InputTaken` | 工人从收件箱取件时：取到的条目本身，以及取到的最后一个收件箱序号。 |
| `ModelCalled` | 调用模型之前：模型名、请求的哈希、第几次尝试。 |
| `ModelReplied` | 模型的回复：消息（文字、推理、工具调用）、停止原因、token 用量。 |
| `ToolStarted` | 工具调用执行之前，记下实际要执行的调用（被改写过的话是改写后的）。 |
| `ToolFinished` | 调用的结果，外加只给前端看、模型永远看不到的数据。 |
| `Compacted` | 组装请求时，把某个序号之前的对话换成一段摘要。 |
| `Waiting` | 任务停下来等：等输入、等批准、等提问的答复、等子任务、等定时器，或者等外部信号。 |
| `TaskEnded` | 任务结束：`done`、`failed` 或 `cancelled`。 |
| `ExtensionRecorded` | 插件（或内核）自己的持久记录，由那个插件自己折叠。 |

每种事件的每个字段见[事件参考](../reference/events.md)。

种类刻意很少。插件要记住什么，就写一条 `ExtensionRecorded(extension, kind, data)`，由它自己的 `reduce` 折叠；内核自己的流水账（设了一个定时器、任务用了哪些插件、这一轮失败了）也用这同一种事件，记在 `extension="core"` 名下，每种 kind 在 `noeta/core/records.py` 里有一个确定的格式。加一项能力从来不用加事件类型，所以旧版前端总能读懂新版任务的日志。

## 日志长什么样

下面是一条真实的日志，用脚本模拟的模型加上文件工具跑一遍，再打印出来：人问某个文件里写了什么，模型用一次工具调用读了文件，然后回答。

<Diagram name="core-event-log" />

```text
 1 TaskCreated        agent=demo
 2 ExtensionRecorded  core/prompts   {}                      开始时找到的提示词覆盖文件
 3 ExtensionRecorded  core/plugins   {pins: [...]}           任务用到的插件
 4 InputTaken         items=[Say "what is in a.txt?"]  through=1
 5 ModelCalled        model=m  request_hash=372920a9…
 6 ModelReplied       tool_use Read {file_path: a.txt}   stop_reason=tool_use
 7 ToolStarted        Read {file_path: a.txt}
 8 ExtensionRecorded  fs/read        {path, digest, mtime, …}  文件工具记下读过什么
 9 ToolFinished       "1\thello world"
10 ModelCalled        model=m  request_hash=223cf056…
11 ModelReplied       "It says hello world."   stop_reason=end_turn
12 Waiting            on=input  detail={answer: "It says hello world."}
```

几个值得注意的地方：

- **收件箱的内容被整个抄进了日志。**`InputTaken` 里是完整的消息，不是指向收件箱的引用。光看日志就知道任务看到过什么。
- **第 2、3 条在第一次接手时写**，早于第一步：提示词覆盖文件，以及正在用的插件版本。记下来，后来的工人才能发出一模一样的字节，缺了其中哪个插件也才会拒绝开跑；接手时发现插件版本变了，就重新记一笔（[插件版本记录](./recovery.md#plugin-pins)）。
- **插件的记录和结果一起落地。**第 8 条是文件工具记下 `Read` 读过的文件（这样后面的 `Edit` 能检查文件读过之后有没有被改过），和后面那条 `ToolFinished` 写在同一次追加里。
- **`ModelCalled` 写在调用之前，`ToolStarted` 写在工具执行之前。**崩溃恢复就靠这个顺序（[单写者与崩溃恢复](./recovery.md)）。
- **`Waiting` 让这次运行结束。**等插件都看过这条 `Waiting`、各自的记录也写进去之后，工人交还认领（除非有后台任务要它留着，见[保留认领](./waiting.md#kept-claims)）；在收件箱有新东西之前，这个任务不占任何工人。

长一点的任务也是这个样子，只是中间更长：提醒和插件的备注表现为 `ExtensionRecorded`（有些带着 `message`，会加进对话）；并行的只读调用表现为一次追加里的几条 `ToolStarted`，后面跟着各自完成时写下的 `ToolFinished`；一次压缩就是一条 `Compacted`。

## 折叠

`noeta/core/loop/state.py` 里的 `apply(state, recorded, reducers)` 把一条事件折进 `TaskState`，`fold` 把所有事件依次折进去。状态里存的是下一步需要的东西：

- `messages`——模型看到的对话（压缩之后的），以及每条消息来自哪个日志序号；
- `calls`——最近一次回复里还没全部得到结果的工具调用，每个都标着已开始、已完成或正在等待；
- `turn_open`、`waiting`、`answers`、`spawned` / `children`、`timers`、排队中的消息；
- 计数：`steps_in_turn`、`model_attempts`、`usage`、`tool_calls`；
- `ext`——每个插件自己的状态，按插件名存放，由插件的 `reduce` 构建。

然后 `noeta/core/loop/decide.py` 里的 `next_action(state, one_shot=..., max_steps=...)` 只看状态和代理的两项设置，决定工人下一步做什么（调用模型、执行调用、恢复被打断的调用、等待、结束……）。[任务的一步](./one-step.md)会逐项讲。

折叠不做 I/O。`pyproject.toml` 里有一条引用规则专门管它：`noeta.core.loop` 不准引用运行器、存储和 provider。同样的事件永远得到同样的状态，恢复的全部基础就在这里。

插件的 `reduce(state, kind, data)` 也必须同样是纯的。它只看到自己的记录（以及它在 `Extension.folds` 里声明要一起折叠的那些插件的记录），外加内核的一种记录：`core.erased`，在人的消息被从对话里抹掉时出现（提示词被拦下，[ADR-026](https://github.com/initxy/noeta/blob/main/docs/adr/026-a-blocked-prompt-is-erased-by-a-core-record.md)）。

## 请求是状态的函数 {#the-request-is-a-function-of-the-state}

`noeta/core/loop/request.py` 里的 `build_request(state, ...)` 组装模型请求：

- 系统提示词分成两个带缓存的块，和 Claude Code 发的一样：代理的身份那一行，然后是代理的提示词，各插件提供的内容按顺序插在里面；
- 消息就是 `state.messages`，原样不动；
- 工具按名字排序，服务端工具排最后，延迟加载的工具单独标出；
- 一个缓存断点，放在最新一条消息上。

放进请求的东西，要么来自折叠，要么在任务一生中固定不变、并在第一次接手时记进日志（提示词覆盖，`core/prompts`）。提醒——插件在调用模型前加的那些文字——在 `ModelCalled` 之前就先记进日志。所以崩溃之后换一台机器接手的工人，组装出来的字节和原来一样，provider 的提示词缓存照样命中。`ModelCalled.request_hash` 是发出去的那份请求的哈希，对不上一眼就能看出来。请求怎么排布才利于缓存，见[上下文、缓存与压缩](./context.md)。

## 日志是唯一完整的记录

从 [ADR-030](https://github.com/initxy/noeta/blob/main/docs/adr/030-the-log-is-the-only-full-transcript.md) 起，折叠只保留下一步需要的东西。压缩之后，`state.messages` 是摘要加上摘要之后的内容，而不是发过的每一条消息。完整的东西都在日志里。

<Diagram name="core-log-vs-fold" />

- 保存对话记录、审计、回退，都去读日志（`noeta/plugins/compaction/transcript.py` 会重新折叠日志，遇到 `history_from` 就顺着读进源任务的日志）。
- 插件如果需要压缩之前的某件事，就在事情发生时把它记进自己的状态（`tool_search` 插件记已加载的工具，记忆插件记读过的页面），和工具结果写在同一次追加里。
- 折叠里唯一还随调用次数增长的是 `TaskState.call_ids`（每次调用几十个字节），用来保证即使网关重复使用工具调用 id，整个任务里的 id 也不重复。

另一种做法是在折叠里保留完整历史，结果每份快照都随任务的年龄一起长：一个压缩过 14 次的测试任务，快照有 255 KB；现在是 5.9 KB。

## 接续另一个任务的对话

`TaskCreated.history_from` 让一个任务从另一个任务的对话开始：先折叠源任务日志里 `history_through` 之前的部分，再折叠新任务自己的事件。分叉、回退、恢复一个子代理，都是这么做的。创建任务时如果没给 `history_through`，就钉在源任务当时的最后一个序号上（`noeta.core.store.pin_history`），这样源任务以后再长，也不会改变新任务折叠出的结果。

源任务的状态经由 `continued()` 带过来：压缩后的对话，以及每个插件的状态（经由 `Extension.continue_state`）；和源任务那次运行绑定的东西（它的收件箱、定时器、子任务、没结束的那一轮）都不带。源任务里还没结束的调用，在副本里会得到一个结果，说明它们是在原来那段对话里跑的。

## 大块内容放在哪

没有单独的大对象存储。事件整条存进存储的 `events` 表，格式是 msgspec 的 JSON 字节（Postgres 上用 `bytea`，所以工具输出里带 `\u0000` 也不会让追加失败）。

让日志保持小巧的是大输出规则，这是内核的规则（`noeta/core/large_output.py`）：工具结果超过上限（默认 50,000 个字符；一次回复里所有结果加起来 200,000），就把全文存成一个文件，放在任务自己的目录里、溢出目录（spill root）之下，通过任务的执行环境写入；日志里只留预览和路径。模型需要时用 `Read` 去读剩下的部分，不管接下来是哪个工人在跑这个任务。见[上下文、缓存与压缩](./context.md)。

## 快照 {#snapshots}

每次认领都把很长的日志从头折叠一遍会越来越慢：任务每次等待、崩溃、换机器之后都要被重新认领，代价会随任务年龄增长。**快照**是任务在某个序号时的折叠结果，存下来之后，认领时只要折叠快照加上它之后的事件（[ADR-006](https://github.com/initxy/noeta/blob/main/docs/adr/006-fold-snapshots.md)）。

<Diagram name="core-snapshot" />

**什么时候写。**每次追加之后（认领时刚加载完也算），当日志比最新快照多出至少 `Runtime.snapshot_every` 条事件（默认 100；`noeta worker --snapshot-every`，`0` 表示从不写），**并且**这些事件编码后的字节数至少是快照大小的十分之一（`SNAPSHOT_GROWTH`），就写一份新的（`noeta/core/runner/snapshotting.py`）。把间隔和快照大小挂钩，快照的大小就按几何级数增长，写入的总字节数保持在日志大小的 11 倍左右以内，而认领时要折叠的事件最多只有快照字节数的十分之一。用固定间隔时，一条 21,627 条事件的日志写了 699 MiB 快照（日志的 91 倍）；换成这条规则后是 75 MiB（[ADR-012](https://github.com/initxy/noeta/blob/main/docs/adr/012-snapshots-follow-state-size.md)）。

**怎么写。**在后台，从上一份快照之后**已经存进存储的**事件折叠而来，从不拿工人手里的实时状态，所以快照一定就是折叠日志得到的结果。状态必须能原样解码回来才会保存（`snapshots.encode`）；某个插件的状态没法被编码如实承载时，这个任务就不写快照，绝不写一份错的。写快照不需要认领：它是日志前缀的折叠结果，而日志前缀永远不变。

**按折叠代码定版本。**快照的版本是一个哈希，算进了内核负责折叠的模块（`events`、`records`、`types`、`loop/state`，以及它们在 noeta 里引用到的所有模块）、代理名、每个插件的版本记录（名字、来源、版本号或文件哈希、参数），以及每个插件的类和它们引用的模块的源码（`noeta/core/snapshots.py`）。源码哈希每个进程只算一次（工人启动时，或者第一次遇到某个代理时），所以磁盘上的代码升级了，正在跑的工人打上的版本也不会变。工人只用和自己同版本的快照，否则就从头折叠日志。升级或改插件只会让快照用不上，永远不会让旧的折叠规则被套用。手工维护版本号的方案被否掉了，因为忘了改号就会折叠出错误的结果。

**两个版本同时存在。**每个任务最多保留 `SNAPSHOT_VERSIONS`（2）份，每个折叠版本一份，这样滚动升级期间新旧两个版本的工人不会互相覆盖（[ADR-023](https://github.com/initxy/noeta/blob/main/docs/adr/023-snapshots-per-fold-version-and-newer-logs-left-to-newer-workers.md)）。

**结束时删除。**结束任务的那次追加会在同一个事务里删掉它的快照，`put_snapshot` 也不会给已结束的任务留快照：已结束的任务不会再被认领。

快照**可以没有，但绝不会错**。测试套件专门守着这一点：任何测试加载的每条任务日志都会用两种方式折叠——从头折叠，以及从几个不同序号的快照开始折叠——再比较结果（`tests/conftest.py`、`tests/snapshot_check.py`）。

在一条 20,002 条事件的日志上实测，一次认领在 sqlite 上从 120 ms 降到 21 ms，在 Postgres 上从 197 ms 降到 119 ms。

## 日志读不了的时候 {#when-a-log-cannot-be-read}

- **是更新的版本写的。**滚动升级期间，旧工人可能遇到只有新版本才会写的记录类型、标签或取值（msgspec 报 “Invalid value”，或者一条它不认识的 `core` 记录，`records.NewerRecord`）。跳过它可能折叠出另一个任务，所以工人什么都不写，等租约自己过期，迟早会有新版本的工人来认领。收件箱里新版本才会写的条目也一样处理。
- **日志坏了。**因为别的原因怎么都读不了、折叠不了的日志（或收件箱条目），在同一个工人上连续 `LOAD_ATTEMPTS`（3）次认领失败后，任务以 `unreadable_log` 结束为 `failed`；因为没有折叠出的状态，这条结束是根据任务行写的。父任务会收到通知，子任务会被取消。
- **快照解不开。**忽略它，从头折叠日志。

插件不会无意中造成第一种情况：`ToolContext.record` 和 `TaskView.record` 拒绝写内核不认识的 `core` 记录（`records.check_writable`）。

## 相关

- [任务的一步](./one-step.md)：工人怎么追加这些事件。
- [单写者与崩溃恢复](./recovery.md)：为什么只有一个工人能追加。
- [上下文、缓存与压缩](./context.md)：`Compacted` 做了什么，请求怎么被缓存。
- [事件参考](../reference/events.md)：每个字段。
