# 切换 provider

**目标：** 将代理从一个 LLM provider 切换到另一个，而无需重写任何代理代码。

**开始之前：** 你有一个使用某个 provider 运行的代理（参见[配置 provider](configure-provider.md)）。

## 相同配方，不同接线

Provider 中立性意味着你的代理身份——system prompt、工具、权限模式、子代理——从不依赖于哪个 provider 在为它服务。provider 是**接线**，在 `Client` 或 `query` 时注入。更换它，相同的 `Options` 会编译为相同的 `AgentSpec`。

## 之前：Anthropic

```python
from noeta.sdk import Client, Options, query
from noeta.llm.anthropic import AnthropicProvider

options = Options(
    system_prompt="You are a concise assistant.",
    name="concise-bot",
    allowed_tools=None,
)

anthropic = AnthropicProvider(
    model="claude-sonnet-4-5-20250929",
    api_key="sk-ant-…",
)

client = Client(options, provider=anthropic, workspace_dir="./")
```

## 之后：OpenAI 兼容

```python
from noeta.llm.openai_compat import OpenAICompatProvider

openai = OpenAICompatProvider(
    model="gpt-5.5",
    base_url="https://api.openai.com/v1",
    api_key="sk-…",
)

# 相同的 options，相同的 client 构造——只有 provider 变了
client = Client(options, provider=openai, workspace_dir="./")
```

其他什么都不变：相同的 `Options`、相同的工具、相同的 `Client` 用法。你的历史记录也是可移植的——EventLog 条目与 provider 无关，因此用 Anthropic 开始的会话可以用 OpenAI 恢复。

## 通过 `query()`（一次性调用）

`query()` 便捷函数也接受 `provider` 关键字参数：

```python
from noeta.sdk import query

result = query(
    options,
    goal="What is the capital of France?",
    provider=openai,  # 或 anthropic，或任何 provider
    workspace_dir="./",
)
print(result.answer())
```

## 验证切换

对两个 provider 运行相同的目标，确认两者都能产生终止回答：

```python
for name, prov in [("anthropic", anthropic), ("openai", openai)]:
    result = query(options, goal="Say hello.", provider=prov, workspace_dir="./")
    print(f"{name}: {result.answer()}")
```

两者都应返回成功的回答。确切文本会有所不同（不同模型），但两者都能到达终止状态。

## 什么不变

切换 provider 时：

- **工具定义** — 相同的 `@tool` 函数、相同的名称、相同的 schema。
- **代理身份** — 编译后的 `AgentSpec` 完全相同，因为 `compile_options` 从不接触 provider。
- **EventLog 格式** — 记录的事件与供应商无关。用 Anthropic 写入的日志可以在激活 OpenAI provider 时进行 fold。
- **权限模型** — 相同的 `permission_mode`、相同的 Guard。

## 什么可能变化

- **工具调用格式** — 内部协议对此做了规范化，但边缘情况（例如并行工具调用）在不同 provider 之间可能行为略有不同。
- **推理 / 扩展思考** — 支持扩展思考的 provider（Anthropic）与不支持的 provider 可能产生不同的内部 trace。
- **Token 数量和定价** — 显然因 provider 而异。

## 另请参阅

- [Provider 中立性](../concepts/provider-neutrality.md) — 这背后的设计
- [配置 provider](configure-provider.md) — 每个 provider 的设置
- [SDK 参考](../reference/sdk.md) — `Options`、`Client`、`query` 签名
- `examples/swap_provider.py` — 可运行演示
