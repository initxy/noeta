# 切换 provider

**目标：** 把代理从一个 LLM provider 切换到另一个，而不改写任何代理代码。

**开始之前：** 你有一个使用某个 provider 运行的代理（参见[配置 provider](configure-provider.md)）。

## 相同配方，不同接线

代理的身份——system prompt、工具、权限模式、子代理——不依赖于哪个 provider 在为它服务。provider 是**接线**，在 `Client` 或 `query` 时注入（或设在 `Options.provider` 上，显式的关键字参数会覆盖它）。更换它，相同的 `Options` 会编译成相同的 `AgentSpec`。

## Anthropic

```python
from noeta.sdk import Client, Options
from noeta.sdk.providers import AnthropicProvider

options = Options(
    system_prompt="You are a concise assistant.",
    name="concise-bot",
    allowed_tools=None,
)

anthropic = AnthropicProvider(api_key="sk-ant-…")

client = Client(
    options,
    provider=anthropic,
    workspace_dir="./",
    model="claude-sonnet-4-6",
)
```

provider 是某个厂商 wire 协议的适配器，而不是与某个模型的绑定，所以它不接受 `model` 参数。模型是按会话在 `Client(model=…)` / `query(model=…)` 上选定的——这让一个 provider 实例可以服务多个模型。

## OpenAI 兼容

```python
from noeta.sdk.providers import OpenAICompatProvider

openai = OpenAICompatProvider(
    base_url="https://api.openai.com/v1",
    api_key="sk-…",
)

# Same options, same client construction — only the provider changes
client = Client(
    options, provider=openai, workspace_dir="./", model="gpt-5.5-2026-04-24"
)
```

`noeta.sdk.providers` 还提供 `OpenAIResponsesProvider`，用于 OpenAI Responses API，它接受同样的 `base_url` / `api_key` 组合。

## 通过 `query()`（一次性调用）

```python
from noeta.sdk import query

result = query(
    options,
    goal="What is the capital of France?",
    provider=openai,  # or anthropic, or any provider
    workspace_dir="./",
    model="gpt-5.5-2026-04-24",
)
print(result.answer())
```

## 验证切换

对两个 provider 运行相同的目标，确认两者都能产生终止回答：

```python
runs = [
    ("anthropic", anthropic, "claude-sonnet-4-6"),
    ("openai", openai, "gpt-5.5-2026-04-24"),
]
for name, prov, model in runs:
    result = query(
        options, goal="Say hello.", provider=prov, workspace_dir="./", model=model
    )
    print(f"{name}: {result.answer()}")
```

确切文本会不同，但两者都能到达终止状态。

## 什么不变

- **工具定义** —— 相同的 `@tool` 函数、相同的名称、相同的 schema。
- **代理身份** —— 编译出的 `AgentSpec` 完全相同，因为 `compile_options` 从不接触 provider。
- **EventLog 格式** —— 记录下来的事件携带中立的消息形状，所以用某个厂商写下的日志，可以在没有安装该厂商适配器的情况下 fold，一个会话也可以在另一个 provider 下恢复。
- **权限模型** —— 相同的 `permission_mode`、相同的 Guard。

## 什么可能变化

- **工具调用格式** —— 内部协议对此做了规范化，但边缘情况（例如并行工具调用）在不同 provider 之间可能行为略有不同。
- **推理续接** —— `OpenAICompatProvider` 会丢弃重新附上的 thinking block，除非你用 `reasoning_continuation="chat"` 构造它；`OpenAIResponsesProvider` 默认会回显 Responses API 所要求的那段加密续接。因此不同厂商之间 trace 会有差异。
- **Token 数量与定价** —— 因 provider 而异。

## 另请参阅

- [Provider 中立性](../concepts/provider-neutrality.md) —— 这背后的设计
- [配置 provider](configure-provider.md) —— 每个 provider 的设置
- [SDK 参考](../reference/sdk.md) —— `Options`、`Client`、`query` 签名
- `examples/swap_provider.py` —— 可运行演示
