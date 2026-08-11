# 概念

这些页面解释 Noeta *为什么*是现在这个样子。它们是背景阅读而非操作指南——这里没有任何一步要求你去执行命令。如果你更想先看到东西跑起来，可以先做[快速上手](../tutorials/quickstart.md)，之后再回来。

请按下面的顺序阅读。每一页只假设你读过它上面的那些，并且每一页都以一段平实的概述和一张图开场。

## 三个想法撑起整个设计

其余的一切 —— 审计、replay、挂起 / 恢复、provider 中立 —— 都是从这三个里长出来的。

### 1. 状态是事件日志的一次 fold

<p align="center">
  <img src="../../assets/diagrams/event-sourcing.svg" alt="Event sourcing — 事件追加进 EventLog，大对象进 ContentStore，fold 重建四个状态切片" width="820">
</p>

每个 task 拥有一条只追加的事件流：交给它的目标、每次组装出的 context plan、每次模型响应、每次工具调用及其结果、每次挂起与唤醒。没有一张供引擎读写的 task 表。谁需要当前状态，就从头 fold 这条流把它算出来 —— 状态对象是一份可丢弃的投影，日志才是母本。事件 payload 保持很小（上限 4 KB）；更大的东西，比如一整个响应体或一份大的工具输出，会进入内容寻址的存储，事件里只留一个引用。因为状态只能由 fold 产生，"agent 做了什么"和"agent 是什么"永远不会打架。

→ [事件溯源](event-sourcing.md) · [Fold 与快照](fold-and-snapshot.md) · [状态与写者](../architecture/state-and-writers.md)

### 2. 中途杀掉它，它会恢复

<p align="center">
  <img src="../../assets/diagrams/crash-resume.svg" alt="崩溃与恢复 —— worker A 在步骤中途死亡，lease 过期，worker B fold 日志并精确一次地继续" width="820">
</p>

一个 worker 对 task 取得一份 **lease** —— 一个短期的、靠 heartbeat 续期的独占持有 —— 并把它推进到下一个挂起点或终止态。每一次向日志写入都要出示这个 lease id，所以同一个 task 在任何时刻只可能有一个写者。worker 一旦死掉，heartbeat 停止，lease 过期，task 回到就绪队列；下一个 worker fold 日志，把被打断的那次 attempt 封存为死历史，然后从最后一个持久点继续。同一套机制也覆盖有意的等待：task 可以为一个人工回答、一个定时器或一个 subtask 挂起，睡着期间不产生任何成本，而唤醒它的 wake 是持久的、单 worker 的、精确一次投递的 —— 至少一次投递加上幂等消费。

→ [唤醒与恢复](wake-resume.md) · [任务模型](task-model.md) · [部署 worker](../how-to/deploy-worker.md)

### 3. 两个包，能力即插件

<p align="center">
  <img src="../../assets/diagrams/architecture.svg" alt="Noeta 架构 —— 你的代码 import noeta.sdk，其下是 noeta-runtime 内核，builtins 只经插件加载器触达内核" width="820">
</p>

Noeta 以两个库交付，共享同一个 `noeta.` 命名空间。**`noeta-sdk`** 是你唯一要 import 的东西：`query` / `Client` / `Options` / `@tool`、预设 agent，以及每一项官方能力。**`noeta-runtime`** 是纯内核 —— Engine、fold、snapshot、Worker、Dispatcher、lease、context composer —— 它没有声明任何依赖。内核自身不含任何能力：文件工具、web 工具、memory、browser、MCP、sandbox、存储后端，以及每一个 provider 适配器，都是内置**插件**，只能经由加载器的动态 ref 解析触达内核，这条规则由 import linter 在每次构建时强制执行。正是这一道边界，让 provider 中立成为结构性事实而不是一句承诺，也让你的插件走的路和 Noeta 自己的完全一样。

→ [架构概览](../architecture/overview.md) · [包与导入规则](../architecture/packages.md) · [扩展平面](../architecture/extension-planes.md)

## 阅读顺序

1. **[事件溯源](event-sourcing.md)** —— Noeta 从不把"当前状态"当作事实。它把发生的一切追加到每个任务自己的日志里，再从这条日志重新算出状态。从这里开始；其余每一页都倚靠这一个想法。

2. **[任务模型](task-model.md)** —— Task 是唯一的工作单元。对话、后台作业、被委派出去的子代理，全都是 Task，每个都有一条日志、四个状态切片和四种状态。

3. **[引擎与执行](engine-execution.md)** —— 一个 Task 究竟是怎么向前推进的：构建模型的输入，问它下一步做什么，执行它，重复，直到 Task 开始等待或结束。

4. **[Fold 与快照](fold-and-snapshot.md)** —— 把日志还原为状态的那个函数，它为什么被刻意做得平淡无奇，以及快照如何在不成为第二个事实来源的前提下让它变快。

5. **[唤醒与恢复](wake-resume.md)** —— 一个 Task 在等待人、等待定时器、等待子任务或等待外部系统时会发生什么，以及即使某台机器在半途宕掉，它又是如何被恰好一次地接续上的。

6. **[Guard 与 Observer](guard-observer.md)** —— 挂接到一个运行中代理上的两种方式：一种可以在动作发生之前拦住它，另一种只能在事后旁观。没有第三种。

7. **[Composer 与上下文缓存](composer-and-cache.md)** —— Noeta 如何决定模型在每次调用中看到什么，以及这个布局为什么是围绕 provider 的 prompt 缓存排布的。

8. **[Provider 中立](provider-neutrality.md)** —— 为什么不允许任何厂商的消息格式变成 Noeta 的内部格式，以及这一点是如何靠一条 import 规则、而不是靠自觉来强制的。

## 一句话版本

一个 **Task** 就是代理的一次运行。发生在它身上的一切都被追加到它的 **EventLog**；它的状态按需从这条日志 **fold** 出来。**Engine** 把一个 Task 推进一步：组装模型看到的内容，问 **Policy** 该做什么，记录结果。当 Task 不得不等待时，它挂起，它的唤醒条件被持久地保留，直到有东西与之匹配。Guard 可以在动作放行的路上否决它；Observer 在事后观察日志。模型看到的一切都由 **ContextComposer** 在每一轮当场重新组装，而每个 LLM 厂商都待在一个适配器后面。

## 下一步

- [快速上手](../tutorials/quickstart.md) —— 大约五分钟跑起一个代理。
- [架构概览](../architecture/overview.md) —— 从模块与包的角度看同一套系统。
- [术语表](../reference/glossary.md) —— 这些页面上的每个术语，在一处定义。
