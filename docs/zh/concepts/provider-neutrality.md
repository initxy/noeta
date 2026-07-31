# Provider 中立

Noeta 内部不说任何厂商的 API。它有自己的一套中立请求与响应形态，每个厂商各配一个**适配器**，在边界处双向翻译：出站（中立的 `LLMRequest` → 该厂商的线上格式）和入站（线上响应 → 中立的 `LLMResponse`）。

随包发布三个适配器，都可从 `noeta.sdk.providers` 获取：`AnthropicProvider`（Anthropic Messages）、`OpenAICompatProvider`（OpenAI Chat Completions），以及 `OpenAIResponsesProvider`（OpenAI Responses）。

<p align="center">
  <img src="../../assets/diagrams/provider-neutrality.svg" alt="Provider 中立——Engine 用一套中立的 LLM 协议与三个厂商适配器通信，它们从不渗入内核" width="820">
</p>

## 选一个是接线，不是重写

挑一个适配器，构造它，交给 `Client`：

```python
from noeta.sdk import Client, Options
from noeta.sdk.providers import OpenAICompatProvider

client = Client(
    Options(system_prompt="You are a helpful assistant."),
    provider=OpenAICompatProvider(base_url="https://api.example.com/v1"),
)
```

你也可以把它设为 `Options.provider`；两者同时存在时 `Client` 的关键字参数获胜。无论哪种方式，这个选择都被排除在 agent 身份之外——两个仅在 provider 上不同、其余完全相等的配方，编译成相同的 `AgentSpec`。

## 规则：任何厂商的格式都不会成为内部契约

把 Anthropic 的消息形态直接抬进内部类型，那么其他每个 provider 从出生起就是二等公民，厂商的怪癖也会渗进 Engine。所以内部形态是中立的，怪癖被圈在适配器里。有四个地方能看出这一点：

- **错误被折叠进一个中立的分类法。** `TransientError`、`ContextOverflowError` 和 `FatalError` 携带 `category` 值 `"transient"` / `"overflow"` / `"fatal"`。运行时包装器把一个 provider 异常变成一个错误 `LLMResponse`（`stop_reason="error"`、`raw["category"]`），而不是让它逃逸出去，因此 Policy 按 category 分支，而重试与压缩逻辑从不关心另一端是谁。

- **厂商机制从不进入核心。** Anthropic 的 cache 断点只作用于出站的线上主体，永不到达账本。扩展思考的往返、按模型的视觉门控、推理努力层级，全部生活在各自的适配器内部。

- **连定价也是中立的。** 一行 `ModelSpec` 描述一个模型，与厂商无关——上下文窗口、输出上限、每 MTok 的价格，其中 cache 读和写分别计价。任何厂商的线上键都不会成为字段名；每个适配器把自己的用量映射进中立的 `Usage`，再由 `CATALOG` 对其计价。

- **流式传输是一个可选能力，不是第二个契约。** 能流式传输的适配器实现 `StreamingProvider.complete_streaming(request, on_delta, request_headers=None)`，它仍然返回完整的 `LLMResponse`；那些 delta 是从不接触账本的瞬时副作用。运行时用 `isinstance` 探测（streaming → 感知 header → 普通的 `complete`），因此没有该能力的 provider 也能原样工作，两种方式记录下来的交换都完全相同。

## 由架构强制，而不是靠自觉

中立性是靠一条 import 规则钉死的，而不是靠一个约定。

适配器住在 `noeta.builtins.providers.impl` 中，而 `.importlinter` 的 `sdk-core-not-builtins` 契约禁止内核与 SDK 核心对 `noeta.builtins` 做**任何**静态 import。内核在物理上无法依赖某个厂商；插件 loader 的动态 `ref` 解析是唯一的门。

就连 `noeta.sdk.providers` 也通过模块的 `__getattr__` 惰性地重新导出这三个类，因此导入 SDK 从不会连带拖进一个 HTTP 客户端——只有真正构建网络 provider 的调用方才为 `httpx` 付费。

## 事件溯源的系统为什么格外在意

写入 EventLog 的事件是中立形态的，因此记录本身就不含厂商。一个针对 Anthropic 运行过的 Task，可以在一个从不导入 Anthropic 适配器的进程里被 fold、检查和审计（见[事件溯源](event-sourcing.md)）。

cache 断点这类线上级制品被刻意排除在日志之外，因此厂商细节永远不会被焊进本应长期存在的事实来源里。

代价是诚实的：每个厂商都要建一个适配器层并维护它。回报是能超越任何厂商关系的记录，以及一个可证明——而不只是约定俗成地——对厂商无知的 Engine。

## 下一步

- [配置 Provider](../how-to/configure-provider.md) —— 凭据、base URL 和模型别名。
- [切换 Provider](../how-to/swap-providers.md) —— 把一个已有的 agent 迁到另一个厂商。
- [Composer 与上下文缓存](composer-and-cache.md) —— 适配器在每次调用中收到的东西。
- [SDK Options](../reference/sdk-options.md) —— `provider` 在其他字段中的位置。
