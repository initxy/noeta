# 配置 provider

**目标：** 让你的 SDK agent 指向真实的 LLM —— Anthropic，或任一 OpenAI 兼容 /
OpenAI-Responses 端点。

**开始之前：** 你已经跑过离线、零凭证的示例（见[你的第一个代理](../tutorials/first-agent.md)），手上有 provider 端点和 API key。

## provider 是 `Options` 的一个字段

在 `noeta.sdk` 上构建 agent 时，provider 是**接线，不是身份** —— 它把 agent
挂载到 host 上，并留在记录之外。各适配器通过 `noeta.sdk.providers` 导出：

```python
from noeta.sdk import Options
from noeta.sdk.providers import AnthropicProvider

options = Options(
    system_prompt="You are a helpful assistant.",
    name="my-agent",
    provider=AnthropicProvider(api_key="sk-ant-…"),
)
```

对接 OpenAI 兼容的 chat-completions 端点用 `OpenAICompatProvider`；对接
Responses API 用 `OpenAIResponsesProvider`（它的 `base_url` 是**完整的**
responses 端点）：

```python
from noeta.sdk.providers import OpenAICompatProvider, OpenAIResponsesProvider

chat = OpenAICompatProvider(
    base_url="https://api.openai.com/v1",
    api_key="sk-…",
)
responses = OpenAIResponsesProvider(
    base_url="https://api.openai.com/v1/responses",
    api_key="sk-…",
)
```

把 provider 传给 `query` / `Client`（或设在 `Options.provider` 上）；同时必须
一起给出 `workspace_dir` 和 `model`。

## 离线测试

离线测试与演示使用 `noeta.sdk.testing` 提供的确定性替身：

```python
from noeta.sdk.testing import FakeLLMProvider
```

用公共消息类型（`LLMResponse` / `TextBlock` / `Usage`，都在 `noeta.sdk` 上）
脚本化它的 `responses`，让一整次运行都无网络 —— 这正是
[`examples/`](https://github.com/initxy/noeta/tree/main/examples) 的 smoke
测试的跑法。

## 切换 provider

provider 是**接线，不是身份**：换掉实例，其余一切不变 —— agent 代码、工具和
已记录的历史都能在厂商之间搬移。前后对比示例见[切换 provider](swap-providers.md)。

## 故障排查

- **401 / 认证错误** —— 检查传给适配器的 key；若走公司代理，在环境里设置
  `HTTPS_PROXY`。
- **模型未找到** —— 你传的 `model` 必须是端点提供的 id（Anthropic 的 id 带
  日期后缀，例如 `claude-sonnet-4-5-20250929`）。
- **自定义模型的上下文一直增长、没有 compaction** —— SDK 目录不认识的模型，需要
  补上 `context_window` / `max_output_tokens`，上下文 compaction 才能生效。

## 另请参阅

- [Provider 中立](../concepts/provider-neutrality.md) —— 为什么内部协议与厂商无关
- [SDK 参考](../reference/sdk.md) —— 完整的 `Options` 面
- [切换 provider](swap-providers.md) —— 前后代码对比示例
