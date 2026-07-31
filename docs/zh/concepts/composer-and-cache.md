# Composer 与上下文缓存

模型每次调用看到的内容，不是某处一个不断膨胀的缓冲区里的对话记录。它是每一轮都从 Task 的 fold 状态当场重新组装出来的，负责组装的组件叫 **ContextComposer**。它产出的东西叫 **View**：真正发到线上的那份 prompt、工具 schema 和消息列表。

选择组装而不是累积，换来两件事。Composer 是一个纯函数——相同的状态永远组装出相同的 View——而且每一次组装都被记录下来，因此你事后可以回去看第 37 轮时模型究竟被展示了什么。

<p align="center">
  <img src="../../assets/diagrams/context-composer.svg" alt="上下文组装器——fold 出的状态被组装成稳定前缀、半稳定段和动态后缀，再发给 provider" width="820">
</p>

Composer 唯一的副作用是把 plan 正文写入 ContentStore，好让 Engine 拿到一个可以附加的引用。它在每一轮 compose → decide 中运行一次，因此只要 Policy 让循环继续转，一次 `run_one_step` 就会组装多次，而 Engine 为每一次都记录一个 `ContextPlanComposed` 信封。

## 三个段，按波动性排序

View 按各部分变化的频率被切分成三个段：

| 段 | 包含 | 何时变化 |
| --- | --- | --- |
| `stable_prefix` | 系统 prompt | 身份或工具集变化时 |
| `semi_stable` | 循环开始前就已激活的常驻内容 | 常驻内容集变化时 |
| `dynamic_suffix` | 滚动对话、工具结果、reminders | 每一轮 |

工具 schema 并不放在某个段内，而是随 `View.provider_tool_schemas` 一同携带，但它们会与系统 prompt *一起*被哈希——因此即便 prompt 文本完全相同，替换一个工具也会让 `stable_prefix` 的哈希发生轮换。

## 这个布局为什么长这样

这个切分是为了**缓存**。Provider 按前缀缓存 KV 状态：只要前缀相对上一次调用逐字节不变，它就被复用，而不是重新编码、重新计费。靠前的位置上多出一个字节，整个请求的这份缓存就全丢了。

因此 Composer 把一切稳定的内容都推到前面并保持字节稳定——排序过的 schema 键、无时间戳、固定的字段顺序、工具描述为空时整段省略——并把所有波动都圈进尾部。`stable_prefix` 的哈希是 `sha256(to_canonical_bytes((stable_content, provider_tool_schemas)))`。

这与让 fold 可复现的是同一套确定性纪律（见 [Fold 与快照](fold-and-snapshot.md)）。在那里它换来重放；在这里它换来缓存命中。

## 常驻内容

有些内容应该坐在对话前面并一直待在那里——skill 目录、记忆索引、项目 instructions、环境事实。那就是 `semi_stable` 段，而东西通过一种叫**内容通道**（content channel）的机制进入它。

一次激活被记录为一个 `ContextContentRecorded` 事件，为该 kind 注册的渲染器会在之后每一次组装时放置这份内容。注册顺序*就是* `semi_stable` 的布局，内置的使用者占据固定的条带：先 `skill`，再 `memory`，再 `instructions`，再 `environment`，任何宿主注册的 kind 排在它们之后。

因为常驻内容是从 fold 状态重新渲染的、而不是存在消息历史里，压缩冲不掉它。它能在长对话里免费存活下来。

## 位置由激活锚点决定

一份常驻内容渲染在*哪里*，取决于它*何时*被激活。fold 会记录每份常驻内容的**锚点**（anchor）——激活那一刻滚动历史的长度——一条规则就覆盖了所有 kind：

- **循环开始前**激活（记忆索引、根 instructions 文件、seed 阶段的 skills——锚点在第一条 assistant 消息处或之前）→ 渲染在 `semi_stable` 中，成为整个会话据以运行的头部的一部分。
- **任务中途**激活（模型在第 40 轮调用了一个 skill）→ 渲染在 `dynamic_suffix` 中，**位于它的锚点位置**：一条消息，落在对话里它被激活的那个点上。

这是一个缓存决策，不是一个审美决策。`semi_stable` 位于对话之前，因此在对话中途改写它，会让 provider 从那里到整条记录末尾的缓存全部失效——每次激活都要完整重新预热一次，而恰恰在 skills 真正会被用到的长会话里，这代价最重。锚定插入只为插入的那些 token 付费。

有两个细节让它保持安全：

- **插入永远不会切断一次工具往返。** 索引会向前滑过任何 `role="tool"` 消息，因此内容绝不会落在一个 assistant `tool_use` 与其结果之间（provider 会拒绝这种形状）。这个滑动是确定性的：相同的 fold 状态，相同的字节。
- **压缩会在摘要边缘按锚点顺序重新挂载这些内容。** 这在发生的当下是免费的——压缩本来就已经让缓存失效了——而且是自动的，因为常驻内容是从状态渲染的，不是存在历史里的。

### instructions 文件随模型阅读而被发现

一个默认关闭的宿主开关（`HostConfig.instructions_discovery`）会装配一个工具后置钩子。在成功 `read` 一个**工作区内**的文件之后，运行时会从工作区根一路走到该文件所在的目录——最浅的优先，根本身排除在外，因为根文件在循环开始前就已加载——并在每个尚未贡献过 instructions 的目录中激活它找到的第一个 `NOETA.md` / `AGENTS.md`。

这是 monorepo 的场景：一棵子树携带自己的约定。每次激活都是一个普通的内容通道事件，在这一轮的工具结果之后发出，因此它锚定在触发它的那次 read 之后，是追加而不是改写头部。

即便 `read` 本身并不受工作区约束（见[内置工具](../reference/tools.md)），发现机制也被围栏限制在工作区内。阅读是观察；instructions 会引导 agent。若从模型碰巧瞥到的任意路径自动加载它们，就等于让一个任意目录来编程这个 agent，因此自动加载的范围始终留在会话被指定的那个目录内。

## 压缩是一个事件，不是一次编辑

当对话变得过长时，总得有东西让步。Noeta 把这件事记录为一个事件，而不是就地编辑历史：

1. Policy 决定压缩，并交回摘要以及它覆盖的边界。
2. Engine 发出 `CompactionRequested`，随后发出携带摘要正文引用的 `Compacted`。
3. fold 把两者都投影到上下文切片上。
4. 下一次组装把被覆盖的前缀换成单条摘要消息——而 `stable_prefix` 保持不动，原始消息仍留在日志里。

由此有两个后果。压缩是**可审计且可复现的**：它就在日志里，因此一个被恢复的 Task 会以相同方式压缩，你也能在事后精确看到削减了什么。而且**没有东西被抹除**：摘要是在组装时应用的一层，而不是一次覆盖，因此完整历史在底层仍然可 fold（见[事件溯源](event-sourcing.md)）。

一个自旋保护为此兜底。如果一次压缩的边界不会推进到已折叠部分之外，它会让 Task 失败，而不是永远循环下去。另有一个独立的探测器会在若干次压缩相隔仅数轮接连发生时锁存一个"抖动"（thrashing）标志，一条 reminder 会把它转化成提示，让模型别再反复重读同一批大块内容。

## 两个只作用于尾部的机制

两者都严格活在 `dynamic_suffix` 中，因此谁都无法搅动已缓存的头部：

- **尾部裁剪**是一个泄压阀，不是一个夹钳。只有当请求逼近模型可用窗口时，Composer 才会把早于某个 token 预算的工具输出清理成一个精简的 `[tool output cleared]` 标记。这些块保留各自的 call id，因此对话仍然良构，而每一份被清理正文的 ContentStore 引用都会进入 plan——可供审计解引用，却不出现在 prompt 中。在窗口以下什么都不清理，因此一个半空的上下文绝不会逼模型重跑一个工具。
- **compose 时的 reminders** 是追加在尾部最末端的纯渲染器：未完成的 todos、一条委派提示、压缩抖动时的一条阅读策略提示。它们只属于 View——从不写入消息流，也从不记录为事件——并在每次 compose 时从 fold 状态重新推导，因此恢复时它们能免费重现。

一轮是由什么构建的，全都落在 plan 正文里：各段的哈希、真正渲染出来的 skills、保留与清理了哪些消息、被清理正文的引用，以及任何被内联或跳过的 skill 资源。

## 下一步

- [引擎与执行](engine-execution.md) —— Composer 在其中运行的那个循环。
- [Provider 中立](provider-neutrality.md) —— View 抵达一个适配器之后会怎样。
- [内置工具](../reference/tools.md) —— 那些 schema 随稳定前缀一同发出的工具。
- [扩展平面](../architecture/extension-planes.md) —— Composer 开放的两个注册钩子（`content_kind` 和 `reminder`）。
