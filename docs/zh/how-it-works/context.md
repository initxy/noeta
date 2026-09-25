# 上下文与缓存

模型每次看到的内容不是一个越堆越长的缓冲区。上下文组装器（`ThreeSegmentComposer`）每一轮都从 Task fold 出的状态重新拼一遍，并且按能让 provider 的 prompt 缓存持续命中的方式排布。每次拼出的内容都会存档，第 37 轮模型到底看到了什么，事后都查得到。

<NtContext lang="zh" />

## 保证什么

- **状态相同，字节相同。** 组装过程是确定的：schema 的键排好序、不放时间戳、字段顺序固定。同样的状态永远拼出同样的 prompt。
- **缓存住的开头不动。** 对话过程中会变的东西都加在末尾，provider 可以一直复用开头的缓存。
- **压缩只是一层覆盖。** 摘要只在模型看到的内容里替代旧消息，原始消息仍留在日志里。
- **每次的 prompt 都可审计。** 每次拼出的内容都存下来，由一条 `ContextPlanComposed` 事件指向它。

## 按变化频率分成三段

| 段 | 放什么 | 何时变 |
| --- | --- | --- |
| `stable_prefix` | system prompt（和工具定义一起算哈希） | agent 的身份或工具集变了 |
| `semi_stable` | 对话开始前加载的内容：技能列表、记忆索引、项目说明、环境信息 | 这批内容变了 |
| `dynamic_suffix` | 对话、工具结果、提醒 | 每一步只在末尾追加；之前的消息从不改写，所以一直走缓存 |

provider 只有在 prompt 开头逐字节不变时才能复用缓存，开头改一个字节，整次请求都要重算。所以换一个工具、启用一个插件都会让缓存失效：一个 agent 的工具和插件要事先定好，不要每轮改。

## 对话中途加进来的内容

常驻内容放在哪，取决于它是什么时候加载的：

- **模型第一次回复之前**加载的（记忆索引、根目录的说明文件、开始时选定的技能）→ 放在 `semi_stable`，属于缓存住的开头。
- **任务进行中**加载的（比如模型在第 40 轮调用了一个技能）→ 作为一条消息插在对话里它被加载的位置。

插在加载位置只需付新增那部分 token 的钱；如果改写开头，整段对话的缓存都要作废。插入位置不会把一次工具调用和它的结果拆开。

打开 `HostConfig.instructions_discovery=True` 后，模型成功 `Read` 工作区内的某个文件时，会从工作区根目录往下到该文件所在目录，逐层加载还没加载过的 `NOETA.md` / `AGENTS.md` / `CLAUDE.md`。适合每个子目录有自己约定的 monorepo。它只在工作区内查找。

## 压缩

对话太长时：

1. policy 返回一段摘要，以及它覆盖的范围。
2. Engine 先记 `CompactionRequested`，再记 `Compacted`，后者指向摘要内容。
3. 从下一轮起，组装器用一条摘要消息代替被覆盖的那段。稳定前缀不动，原始消息仍在日志里。

恢复后的 Task 会以同样的方式压缩；模型也可以用 `RecallHistory` 工具把被压缩的那段读回来。如果一次压缩没能让边界往前推进，Task 会直接失败，不会原地打转。

## 只在末尾生效的两个机制

- **清理旧工具输出。** 只有请求快要超出模型可用窗口时，才把较早的工具输出换成 `[tool output cleared]`。调用 id 保留，对话结构仍然合法，原始内容仍在内容存储里。
- **提醒。** 每次 prompt 最末尾的几句短说明——没做完的待办、被压缩的历史在哪。它们每轮根据状态重新算，从不写进日志。

## 对你意味着什么

- 一个 Task 里保持 system prompt 和工具集不变，缓存才能一直热着。
- 每轮都变、随时间变的信息放进提醒，不要放进 system prompt。
- 想加自己的常驻内容或提醒，就在插件里注册 `content_kind` 或 `reminder`；组装器本身不能替换。

设计记录：
[unified context supply](https://github.com/initxy/noeta/blob/main/docs/adr/unified-context-supply.md) ·
[anchored content placement](https://github.com/initxy/noeta/blob/main/docs/adr/anchored-content-placement.md) ·
[context compaction](https://github.com/initxy/noeta/blob/main/docs/adr/context-compaction.md)

## 下一步

- [Engine](engine.md)：组装器所在的循环。
- [插件系统](plugin-system.md)：`content_kind` 和 `reminder` 两个扩展点。
- [接入模型](../guides/models.md)：prompt 到了适配器那一层会怎样。
