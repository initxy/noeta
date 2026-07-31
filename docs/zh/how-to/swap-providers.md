# 切换 Provider

本指南教你把一个已经跑起来的 agent 从一个 LLM provider 换到另一个，而不用改写任何 agent 代码。你需要一个已经跑在某个 provider 上的 agent —— 见[配置 Provider](configure-provider.md)。

## 同一份配方，不同的接线

一个 agent 的身份 —— 系统提示、工具、权限模式、子 agent —— 不取决于由哪个 provider 提供服务。provider 是**接线**，在 `Client` 或 `query` 时注入（或者设在 `Options.provider` 上，显式关键字参数会覆盖它）。换掉它，同一份 `Options` 仍然编译成同一个 `AgentSpec`。

## 1. 从 Anthropic 开始

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

provider 是某个厂商线上协议的适配器，而不是对某一个模型的绑定，因此它不接收 `model` 参数。模型是在 `Client(model=…)` / `query(model=…)` 上按会话选定的，这让一个 provider 实例可以服务许多模型。

## 2. 改一行换成 OpenAI 兼容

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

`noeta.sdk.providers` 还提供 `OpenAIResponsesProvider` 对接 OpenAI Responses API，它接收同样的 `base_url` / `api_key` 组合。

一次性的 `query` 以同样的方式接收 provider：

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

## 3. 验证这次切换

用同一个目标在两个 provider 上各跑一次，确认两者都到达终态答案：

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

```
anthropic: Hello! How can I help you today?
openai: Hi there — what can I do for you?
```

具体措辞当然会有差别。重点在于：两者都从同一份 `Options` 到达了终态，而中间你的代码没有任何改动。

## 什么不会变

- **工具定义** —— 同样的 `@tool` 函数、同样的名字、同样的 schema。
- **agent 身份** —— 编译出来的 `AgentSpec` 完全相同，因为 `compile_options` 根本看不到 provider。
- **EventLog 格式** —— 记录下来的事件携带的是中立的消息形状，因此一份对着某个厂商写下的日志，在没有安装该厂商适配器的环境下也能 fold，而一场会话可以在另一个 provider 下恢复。
- **权限模型** —— 同样的 `permission_mode`、同样的 Guard。

## 什么可能会变

- **工具调用格式** —— 内部协议会把它归一化，但边缘情况（比如并行工具调用）的表现可能略有不同。
- **推理续接** —— `OpenAICompatProvider` 会丢弃重新附上的 thinking 块，除非你用 `reasoning_continuation="chat"` 构造它；`OpenAIResponsesProvider` 默认回显 Responses API 所要求的加密续接内容。因此 trace 在不同厂商之间会有差异。
- **token 计数与定价** —— 各 provider 不同。

## 下一步

- [配置 Provider](configure-provider.md) —— 各适配器的配置与模型目录
- [Provider 中立](../concepts/provider-neutrality.md) —— 这背后的设计
- [SDK 参考](../reference/sdk.md) —— `Options`、`Client`、`query` 的签名

`examples/swap_provider.py` 是一个可运行的演示。
