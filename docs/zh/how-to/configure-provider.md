# 配置 Provider

本指南教你把一个 agent 指向真实的 LLM —— Anthropic，或任一 OpenAI 兼容 / OpenAI-Responses 端点。你需要一套可用的 `noeta.sdk` 环境、一个端点和一个 API key。

<p align="center">
  <img src="../../assets/diagrams/provider-neutrality.svg" alt="Provider 中立 —— Engine 只讲一种 LLM 协议；三个适配器在边缘做翻译" width="820">
</p>

## Provider 是接线，不是身份

`compile_options` 从不读取 `Options.provider`，这个字段也被排除在相等性判断之外。两份只在 provider 上不同的配方会编译成*同一个* agent 身份，所以换厂商不会动到你的 agent 代码、工具，以及已记录的历史。

这也是为什么它是一篇操作指南而不是一份迁移文档：挑一个适配器，交给客户端，完事。

## 1. 构建一个适配器

各适配器住在 `noeta.sdk.providers` 这个惰性子模块里 —— 导入 `noeta.sdk` 并不会把 `httpx` 拉进来，除非你真的构建一个网络 provider。

**Anthropic。** 省略 `api_key` 时回退到 `ANTHROPIC_API_KEY` 环境变量，并且会立即抛错，而不是交还一个在首次调用时以一个不透明的 401 失败的客户端。

```python
from noeta.sdk.providers import AnthropicProvider

anthropic = AnthropicProvider(api_key="sk-ant-…")
```

**OpenAI 兼容的 `/chat/completions`。** `base_url` 是必填的（一个*兼容*端点没有约定俗成的默认值）；key 回退到 `OPENAI_API_KEY`，并以 `Authorization: Bearer …` 的形式发送。

```python
from noeta.sdk.providers import OpenAICompatProvider

chat = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="sk-…")
```

**OpenAI Responses API。** 需要同时给出 `base_url` 和 `api_key`，并以 `api-key` 头的形式发送凭证。它的 `base_url` 是**完整的** responses 端点 —— 原样 POST，不会再追加任何路径。

```python
from noeta.sdk.providers import OpenAIResponsesProvider

responses = OpenAIResponsesProvider(
    base_url="https://api.openai.com/v1/responses",
    api_key="sk-…",
)
```

三者都接受 `extra_headers`，用于网关或代理专用的头。适配器**构造一次就反复复用**：每个都持有一个共享的 `httpx.Client`，而模型是每次调用时选定的，不是每个适配器绑定一个。

## 2. 把它交给客户端

要么设置 `Options.provider`，要么直接传给 `Client` / `query`。显式参数优先；两者都不给时，`Client` 构造函数会抛 `ValueError`。

```python
from pathlib import Path
from noeta.sdk import Client, Options

options = Options(system_prompt="You are a helpful assistant.", name="my-agent")

client = Client(
    options,
    provider=chat,
    workspace_dir=Path("./workspace"),
    model="gpt-4o",
)
```

`workspace_dir` 和 `model` 都是可选的。`workspace_dir` 回退到 `Options.cwd`，再回退到进程工作目录；`model` 回退到 `Options.model`，再回退到 `"sonnet"`。**生产环境请显式传 `model`** —— 那个回退值只有在你的端点碰巧提供该 id 时才有用。

## 3. 验证它

驱动一轮用完即弃的对话，把答案打印出来：

```python
from noeta.sdk import query

result = query(options, goal="Reply with the word OK.", provider=chat,
               model="gpt-4o")
print(result.answer())
```

```
OK
```

如果你拿到的是异常，看下面的故障排查表。

## 4. 注册目录里没有的模型

compaction 的各项参数和成本核算都是从模型目录推导出来的。目录未描述的模型会回退到保守的 compaction 参数（128,000 token 窗口），价格记为 `0.0` —— 每种退化都有一条 warn-once 日志，绝不抛异常。为任何网关模型、微调模型或自托管 id 注册一行 —— 声明式地写在 host config 上：

```python
from noeta.sdk import Client, HostConfig
from noeta.sdk.providers import ModelSpec

client = Client(options, provider=chat, host_config=HostConfig(
    extra_models={
        "my-gateway-model": ModelSpec(
            real_model_id="my-gateway-model",
            context_window=200_000,
            max_output_tokens=8_192,
            input_price_per_mtok=3.0,
            output_price_per_mtok=15.0,
            cache_read_price_per_mtok=0.3,
            cache_write_price_per_mtok=3.75,
            provider_family="anthropic",   # 可选：声明这个 id 背后实际说话的是谁
        ),
    },
))
```

或者在进程启动时、构建任何 Client 之前命令式注册：

```python
from noeta.sdk.providers import ModelSpec, register_models

register_models({"my-gateway-model": ModelSpec(...)})
```

网关不公布价目表就把价格留为 `None` —— 那是"价格未知"（警告一次、按 `0.0` 记账），与真正免费的 `0.0` 是两种状态。不要直接改 `CATALOG`：它只是出厂表，注册才是碰撞规则的执行点（与出厂行撞名会让构建失败，而不是悄悄覆盖），也是合并视图 `noeta.sdk.providers.catalog_models()` 保持一致的前提。每次启动注册同样的行：compaction 推导参与组装出的 prompt 字节，恢复的会话必须看到同一张目录。

## 故障排查

| 现象 | 修法 |
| --- | --- |
| 构造时抛 `ValueError` | 适配器没找到 key。传 `api_key=`，或设置它回退到的那个环境变量。 |
| 401 / 认证错误 | key 不对或已过期。适配器使用 `httpx`，所以企业代理可以通过 `HTTPS_PROXY` 生效。 |
| 模型未找到 | `model` 必须是端点提供的 id。Anthropic 的 id 带日期后缀，例如 `claude-sonnet-4-5-20250929`。 |
| 上下文一直增长，成本却停在 `$0.00` | 该模型不在 `CATALOG` 里 —— 见第 4 步。 |

## 下一步

- [切换 Provider](swap-providers.md) —— 把已有的 agent 迁到另一个厂商
- [Provider 中立](../concepts/provider-neutrality.md) —— 为什么内部协议与厂商无关
- [SDK 参考](../reference/sdk.md) —— 完整的 `Options` 面
- [故障排查](../operations/troubleshooting.md) —— provider 错误的上下文
