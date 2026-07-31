# 配置 Provider

**目标：** 让你的 SDK Agent 指向真实的 LLM —— Anthropic，或任一 OpenAI 兼容 / OpenAI-Responses 端点。

**开始之前：** 你已经跑过离线、零凭证的示例（见[你的第一个 Agent](../tutorials/first-agent.md)），并且手上有一个 Provider 端点和一个 API key。

## Provider 是接线，不是身份

`compile_options` 从不读取 `Options.provider`，这个字段也被排除在相等性判断之外。两份只在 Provider 上不同的配方会编译成同一个 Agent 身份，所以换厂商不会动到 Agent 代码、工具，以及已记录的历史。

各适配器位于 `noeta.sdk.providers` 这个子模块里 —— 之所以做成子模块，是为了让导入 SDK 时不会拉进 `httpx`，除非你真的要构建一个网络 Provider：

```python
from noeta.sdk import Options
from noeta.sdk.providers import AnthropicProvider

options = Options(
    system_prompt="You are a helpful assistant.",
    name="my-agent",
    provider=AnthropicProvider(api_key="sk-ant-…"),
)
```

省略 `api_key` 时，`AnthropicProvider` 会回退到 `ANTHROPIC_API_KEY` 环境变量；它会直接抛错，而不是构造出一个在首次调用时以一个不透明的 401 失败的客户端。

对接 OpenAI 风格的 `/chat/completions` 端点，用 `OpenAICompatProvider`；对接 Responses API，用 `OpenAIResponsesProvider`，它的 `base_url` 是**完整的** responses 端点（原样 POST，不会再追加任何路径）：

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

`OpenAICompatProvider` 需要 `base_url`，凭证回退到 `OPENAI_API_KEY`，并以 `Authorization: Bearer …` 的形式发送。`OpenAIResponsesProvider` 需要同时给出 `base_url` 和 `api_key`，并以 `api-key` 头的形式发送凭证。三者都接受 `extra_headers`，用于网关或代理专用的头。

Provider 构造一次即可复用：每个都持有一个共享的 `httpx.Client`，而模型是每次调用时从请求里选定的。

## 把它传给客户端

要么如上设置 `Options.provider`，要么直接交给 `Client` / `query` —— 显式参数优先；两者都不给时，`Client` 构造函数会抛 `ValueError`：

```python
from pathlib import Path

from noeta.sdk import Client

client = Client(
    options,
    provider=chat,
    workspace_dir=Path("./workspace"),
    model="gpt-4o",
)
```

`workspace_dir` 和 `model` 都是可选的。`workspace_dir` 回退到 `Options.cwd`，再回退到进程工作目录；`model` 回退到 `Options.model`，再回退到 `"sonnet"`。生产环境请显式传 `model` —— 那个回退值只有在你的端点碰巧提供该 id 时才有用。

## 离线测试

确定性替身位于 `noeta.sdk.testing`，特意放在 `noeta.sdk` 根之外，好让生产代码的导入永远不会意外拉进测试材料：

```python
from noeta.sdk.testing import FakeLLMProvider
```

用公共消息类型（`LLMResponse`、`TextBlock`、`Usage` —— 都在 `noeta.sdk` 上）脚本化它的 `responses`，一整次运行就无需网络。`examples/` 的 smoke 测试就是这么跑的。

## 故障排查

- **401 / 认证错误** —— 检查你传给适配器的 key，或它回退到的环境变量。适配器使用 `httpx`，所以环境里的 `HTTPS_PROXY` 会被尊重，可用于公司代理。
- **模型未找到** —— 你传的 `model` 必须是端点提供的 id。Anthropic 的 id 带日期后缀，例如 `claude-sonnet-4-5-20250929`。
- **上下文一直增长、没有 compaction** —— compaction 的各项参数是从模型目录推导出来的，而目录里没有描述的模型会被关闭 compaction。注册你自己的一行：

  ```python
  from noeta.sdk.providers import CATALOG, ModelSpec

  CATALOG["my-gateway-model"] = ModelSpec(
      real_model_id="my-gateway-model",
      context_window=200_000,
      max_output_tokens=8_192,
      input_price_per_mtok=3.0,
      output_price_per_mtok=15.0,
      cache_read_price_per_mtok=0.3,
      cache_write_price_per_mtok=3.75,
  )
  ```

  同一行也驱动按运行计的成本核算；如果你不需要，把价格留在 `0.0` 即可。

## 另请参阅

- [Provider 中立](../concepts/provider-neutrality.md) —— 为什么内部协议与厂商无关
- [SDK 参考](../reference/sdk.md) —— 完整的 `Options` 面
- [切换 Provider](swap-providers.md) —— 前后对比的代码示例
