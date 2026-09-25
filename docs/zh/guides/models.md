# 接入模型

把 agent 接到 Anthropic、任意 OpenAI 兼容的 `/chat/completions` 网关，或 OpenAI Responses API。provider 只是一个参数，换厂商时 `Options`、工具和已经记下的历史都不用动。

<NtProviders lang="zh" />

## 选适配器

三个适配器都在 `noeta.sdk.providers` 里：

| 适配器 | 对接的接口 | 密钥 | `base_url` |
| --- | --- | --- | --- |
| `AnthropicProvider` | Anthropic Messages | `api_key=` 或 `ANTHROPIC_API_KEY` | 可不填，默认 `https://api.anthropic.com` |
| `OpenAICompatProvider` | 任意 `/chat/completions` | `api_key=` 或 `OPENAI_API_KEY`，以 `Authorization: Bearer` 发送 | 必填 |
| `OpenAIResponsesProvider` | OpenAI Responses | `api_key=` 必填，以 `api-key` 请求头发送 | 必填；填完整的接口地址，原样 POST |

```python
from noeta.sdk.providers import (
    AnthropicProvider, OpenAICompatProvider, OpenAIResponsesProvider,
)

anthropic = AnthropicProvider()                      # reads ANTHROPIC_API_KEY
chat = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="sk-...")
responses = OpenAIResponsesProvider(
    base_url="https://api.openai.com/v1/responses", api_key="sk-...",
)
```

- 找不到密钥时，构造阶段就抛 `ValueError`，不会等到第一次调用才报 401。
- 三个都接受 `extra_headers={...}`（网关或代理要的额外请求头）和 `timeout_seconds`。
- 适配器建一次反复用。它内部共用一个 HTTP 客户端，什么模型都能服务；用哪个模型是在 client 上指定的，不绑在适配器上。

## 交给 agent

```python
from noeta.sdk import Client, Options, query

options = Options(system_prompt="You are a concise assistant.")

result = query(options, goal="Reply with the word OK.",
               provider=anthropic, model="claude-sonnet-5")
print(result.answer())

with Client(options, provider=chat, model="gpt-4o") as client:
    turn = client.start(goal="Reply with the word OK.")
```

- `Client` / `query` 上的 `provider=` 优先于 `Options.provider`。两处都没给，`Client` 抛 `ValueError`。
- `model` 没传时依次取 `Options.model`、`"sonnet"`。请传一个你的接口确实支持的模型 id。

## 换厂商

换 provider 和模型 id，别的都不用改。

```python
for provider, model in [(anthropic, "claude-sonnet-5"), (chat, "gpt-4o")]:
    print(query(options, goal="Say hello.", provider=provider, model=model).answer())
```

| 不变的 | 可能不同的 |
| --- | --- |
| `Options`、工具、权限模式、编译出来的 agent 身份 | 回答措辞、token 数、价格 |
| 事件日志格式：在一家厂商下写的日志，换一家也能读取、恢复 | 工具调用的边角情况，比如并行调用 |

`OpenAICompatProvider` 默认不回传之前的推理内容，要回传就传 `reasoning_continuation="chat"`；`OpenAIResponsesProvider` 默认会回传。可运行的演示在 `examples/swap_provider.py`。

## 注册自己的模型 id

Noeta 按模型目录来确定上下文窗口和每次调用的价格。目录里没有的 id（网关里的名字、微调模型、自己部署的模型）会按 128,000 token 窗口、价格 `0.0` 处理，并打印一次警告。在 host 配置里把它描述清楚：

```python
from noeta.sdk import Client, HostConfig
from noeta.sdk.providers import ModelSpec

host = HostConfig(extra_models={
    "my-gateway-model": ModelSpec(
        real_model_id="my-gateway-model",
        context_window=200_000,
        max_output_tokens=8_192,
        input_price_per_mtok=3.0,
        output_price_per_mtok=15.0,
        cache_read_price_per_mtok=0.3,
        cache_write_price_per_mtok=3.75,
        provider_family="anthropic",   # optional: what really answers behind the id
    ),
})
client = Client(options, provider=chat, model="my-gateway-model", host_config=host)
```

- 网关没公布价格就把价格留成 `None`，表示「不知道」（警告一次，按 `0.0` 记），和真正免费的 `0.0` 不是一回事。
- 也可以在进程启动时调 `noeta.sdk.providers` 里的 `register_models({...})`，效果相同。别直接改 `CATALOG`；走注册时，和自带条目重名会直接报错。
- 每次启动都注册同样的条目。模型目录会影响拼出来的提示词，恢复的任务必须看到同一份目录。

## 辅助调用用便宜模型

| `Options` 字段 | 作用 |
| --- | --- |
| `compaction_model` | 压缩长历史时做摘要用的模型；默认用主模型 |
| `webfetch_model` | `WebFetch` 消化网页内容用的模型；默认用主模型 |
| `recall_model` | 打开「用模型挑选相关记忆」；默认关闭 |
| `effort` | 推理力度：`low`、`medium`、`high`、`xhigh`、`max` |
| `thinking` | `"adaptive"` 或 `"disabled"` |

这些都只是运行配置，不改变 agent 的身份。

## 排查问题

| 现象 | 处理 |
| --- | --- |
| 构造适配器时抛 `ValueError` | 没找到密钥。传 `api_key=`，或设置上表里对应的环境变量。 |
| 401 / 认证失败 | 密钥错了或过期了。公司代理可以用 `HTTPS_PROXY`。 |
| 找不到模型 | `model` 必须是接口支持的 id。 |
| 费用一直是 `$0.00` | 模型不在目录里，按上面的方法注册。 |

## 下一步

- [离线测试](testing.md)：测试里换成脚本化的模型
- [Options 参考](../reference/options.md)
- [为什么要厂商中立](https://github.com/initxy/noeta/blob/main/docs/adr/provider-neutral.md)（ADR）
