# Provider 中立

Noeta 通过自己的与供应商无关的**内部协议**与 LLM 通信。每个供应商——Anthropic Messages、OpenAI Chat Completions、OpenAI Responses 网关——都有一个**适配器**，在边界处进行双向翻译：出站（中立请求 → 线上格式）和入站（线上响应 → 中立形态）。

设计意图一句话：**任何供应商的线上格式都不会成为内部契约。** 如果 Anthropic 的消息形态被直接提升为内部类型，每个其他 provider 从出生起就是二等公民，供应商的特性会通过该类型渗入 Engine。相反，内部形态是中立的，特性留在适配器中：

- **错误被归入一个中立分类法** —— transient、context-overflow、fatal——因此 Engine 的重试和压缩逻辑永远不关心另一端是谁。
- **供应商特定的机制从不进入核心** —— Anthropic cache 断点仅停留在线上格式，永远不到达账本；扩展思考往返、按模型的视觉门控，以及推理努力层级，全部生活在各自的适配器内部。
- **Token 流式传输是一个可选能力，不是第二个契约** —— 能流式传输的适配器实现 `StreamingProvider`：`complete_streaming` 仍然返回完整响应，它沿途发出的 token 是从不接触账本的瞬时副作用。运行时探测该能力并回退到普通的 `complete`，因此没有该能力的 provider（或任何自定义 `Options.provider`）可以不变地工作，无论哪种方式记录的交换都是相同的。

## 由架构而非纪律强制执行

中立性由一条导入规则钉死：**运行时内核被禁止导入 provider 包**，由 CI 中的 import-linter 检查。内核在物理上不能依赖供应商。Provider 生活在边缘的一个适配器带中，只有最外层的接线层将具体供应商接入——这就是为什么更换 provider 是一次接线变更（`Options.provider`），而非一次重写。

## 为什么事件溯源的系统格外在意

因为写入 EventLog 的事件是中立形态的，记录本身就不含供应商：一个针对 Anthropic 运行的 Task 可以在一台没有安装 Anthropic SDK 的机器上被 fold、检查和审计（见[事件溯源](event-sourcing.md)）。cache 断等线上级制品被刻意排除在日志之外，因此供应商细节永远不会被焊入本应是长期、可读的事实来源中。

代价是诚实的：每个供应商需要一个适配器层来构建和维护。回报是能超越任何供应商关系的记录，以及一个可证明——而非仅仅约定俗成地——对供应商无知的 Engine。

相关：[Composer & cache](composer-and-cache.md) ·
[事件溯源](event-sourcing.md) ·
[架构概览](../architecture/overview.md)
