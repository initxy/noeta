# Provider 中立

Noeta 通过自己的与供应商无关的**内部协议**与 LLM 通信。每个供应商都有一个**适配器**，在边界处进行双向翻译：出站（中立的 `LLMRequest` → 线上格式）和入站（线上响应 → 中立的 `LLMResponse`）。有三个随包发布，可从 `noeta.sdk.providers` 获取：`AnthropicProvider`（Anthropic Messages）、`OpenAICompatProvider`（OpenAI Chat Completions），以及 `OpenAIResponsesProvider`（OpenAI Responses）。

设计意图一句话：**任何供应商的线上格式都不会成为内部契约。** 如果把 Anthropic 的消息形态直接提升为内部类型，那么其他每个 provider 从出生起就是二等公民，供应商的怪癖会渗入 Engine。相反，内部形态是中立的，怪癖留在适配器中：

- **错误被折叠进一个中立的分类法。** `TransientError`、`ContextOverflowError` 和 `FatalError` 携带 `category` 值 `"transient"` / `"overflow"` / `"fatal"`。运行时包装器把一个 provider 异常翻译成一个错误 `LLMResponse`（`stop_reason="error"`、`raw["category"]`），而不是让它逃逸出去，因此 Policy 按 category 分支，重试和压缩逻辑从不关心另一端是谁。
- **供应商机制从不进入核心。** Anthropic cache 断点只作用于出站的线上主体，永不到达账本；扩展思考往返、按模型的视觉门控，以及推理努力层级，全部生活在各自的适配器内部。
- **连定价也是中立的。** 一行 `ModelSpec` 描述一个模型，无关供应商——上下文窗口、输出上限、每 MTok 的价格，其中 cache 读和写分别计价。任何供应商的线上键都不会成为字段名；每个适配器把自己的用量映射进中立的 `Usage`，再由 `CATALOG` 对其计价。
- **流式传输是一个可选能力，不是第二个契约。** 能流式传输的适配器实现 `StreamingProvider.complete_streaming(request, on_delta, request_headers=None)`，它仍然返回完整的 `LLMResponse`；那些 delta 是从不接触账本的瞬时副作用。运行时用 `isinstance` 探测（streaming → 感知 header → 普通的 `complete`），因此没有该能力的 provider 也能不变地工作，两种方式记录下来的交换都完全相同。

## 由架构而非纪律强制执行

中立性由一条导入规则钉死，而非一个约定。适配器生活在 `noeta.builtins.providers.impl` 中，而 `.importlinter` 的 `sdk-core-not-builtins` 契约禁止内核与 SDK 核心对 `noeta.builtins` 做**任何**静态导入。内核在物理上无法依赖某个供应商；plugin loader 的动态 `ref` 解析是唯一的门。就连 `noeta.sdk.providers` 也通过模块的 `__getattr__` 惰性地重新导出这三个类，因此导入 SDK 从不会连带拖进一个 HTTP 客户端。

因此选择一个供应商是接线，而非重写：把一个具体的适配器作为 `Client(provider=…)` 或 `Options.provider` 传入。这个选择被排除在 agent 身份之外——两个仅在 provider 上不同、其余完全相等的配方，编译成相同的 `AgentSpec`。

## 为什么事件溯源的系统格外在意

因为写入 EventLog 的事件是中立形态的，记录本身就不含供应商：一个针对 Anthropic 运行的 Task，可以在一个从不导入 Anthropic 适配器的进程里被 fold、检查和审计（见[事件溯源](event-sourcing.md)）。cache 断点这类线上级制品被刻意排除在日志之外，因此供应商细节永远不会被焊入本应长期存在的事实来源中。

代价是诚实的：每个供应商需要一个适配器层来构建和维护。回报是能超越任何供应商关系的记录，以及一个可证明——而非仅仅约定俗成地——对供应商无知的 Engine。

相关：[Composer 与 cache](composer-and-cache.md) ·
[事件溯源](event-sourcing.md) ·
[架构概览](../architecture/overview.md)
