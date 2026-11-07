# provider 与模型路由

这一页讲 noeta 怎么和模型接口打交道：唯一的 provider 接口；自带的三种请求格式；给每次调用定尺寸的模型目录；配了好几个 provider 时，一个模型怎么被分到其中一个，并且每台工人都发到同一个地方；以及调用失败时会怎样。怎么配置 provider 见[模型与 provider](../guides/models.md)，配置项见[配置项](../reference/config.md)。

## provider 接口

provider 就是一个异步调用：给一个请求，拿回一个回复（`noeta/core/provider.py`）：

```python
class Provider(Protocol):
    family: str          # 它讲哪种格式：anthropic、openai_compat、openai_responses
    async def complete(self, request: ModelRequest,
                       on_delta: OnDelta | None = None) -> ModelResponse: ...
    # 可选：count_tokens(model, text)、complete_whole(request)、aclose()
```

请求和回复都是 noeta 自己的类型（`noeta/core/types.py`）：`ModelRequest` 装着系统提示的几块、消息、工具、`max_tokens`、推理强度、是否推理、缓存标记、请求头，以及接口代码可能用到的少量厂商开关（`betas`、`vendor_fields`）；`ModelResponse` 装着回复消息、停止原因、模型名和用量 `Usage`（`uncached`、`cache_read`、`cache_write`、`output`、`reasoning`）。各家厂商的细节全部关在各自的接口代码里，所以内核、日志和所有插件都不依赖具体厂商，一个任务也能在对话中途换 provider。

接口代码自己从不重试。它把每种失败归成一个中立的错误（`noeta/core/errors.py`）：`Transient`（网络、5xx、过载）、`RateLimited`（429，带 `Retry-After`）、`StreamBroken`、`TimedOut`、`MalformedToolArguments`、`ContextOverflow`，或者 `Fatal`（鉴权、请求本身有错、拒绝）。拿这些错误怎么办，只由一处代码决定（见[下文](#retries-and-timeouts)）。

provider 按类型通过入口点组 `noeta.providers` 找到，所以支持一种新的模型接口，就是写一个注册了它的包。provider 不能来自插件文件：插件版本记录不管 provider，而只装在某一台工人上的 provider 会破坏"任何工人都能接着跑任何任务"。

## 三种请求格式

| 类型 | 地址 | 接口代码做什么 |
|---|---|---|
| `anthropic`（`noeta/backends/providers/anthropic.py`） | `<base_url>/v1/messages`（base URL 只写到主机） | 发的就是 Claude Code 给 API key 用户发的那种请求：它那一组 beta 开关、自适应推理、推理强度、`context_management`、`metadata.user_id`、三个缓存标记、宿主附加的提示作为对话中间的 `system` 轮和 `<system-reminder>` 块。每次调用都走流式；流开始之后又断了，就把同一个请求改成一次性返回再发（`complete_whole`）。通过 `/v1/messages/count_tokens` 数 token。支持 `prompt_cache_ttl`。 |
| `openai_compat`（`openai_compat.py`） | `<base_url>/chat/completions`（base URL 以 `/v1` 结尾） | Chat Completions 格式，用于 OpenAI、vLLM、OpenRouter、LiteLLM、DeepSeek 和大多数网关。宿主附加的文字作为一条 `system` 消息发出，并说明它不是用户的话。工具名超过这种格式允许的长度时缩短，回来时再还原。每次调用都走流式，把流拼回完整结果后只解析一次。 |
| `openai_responses`（`openai_responses.py`） | 完整的 `…/v1/responses` 地址，原样 POST | Responses API 的 `input` / `output` 条目。每个请求都带 `store: false`，网关上不留任何东西，恢复的任务也不依赖它；加密的推理条目按接口要求原样发回去，好让推理接得上。 |

## 模型目录

`noeta/core/models.py` 里每个模型一条 `ModelSpec`：上下文窗口、输出上限、默认发的 `max_tokens`、价格（输入、输出、缓存读、缓存写）、是否推理、能否看图、显示名、训练数据截止时间、默认推理强度。别名 `opus`、`sonnet`、`haiku`、`fable` 指向对应的 Claude 模型。

目录放在内核里，是因为内核每次调用都要用它：给每次调用设输出上限（`Runtime.output_ceiling`，总是发出去，因为曾有网关在不传时默认只给 1,000 个 token），压缩插件从它算阈值，接口代码据此决定推理参数和图片能不能发，`price()` 把记下的用量换算成花费。插件和 provider 读的是同一张表，一个模型在哪都只有一行数据。

**目录里没有的模型**会给出警告，绝不悄悄套个默认值了事：按保守的 128,000 窗口、16,384 输出上限算（照样会压缩），图片放行交给 provider 去判断，价格未知：花费按 0 美元算并给出警告，所以美元预算会少算。运行时可以用 `register_models` 加行，来源可以是模型文件（`NOETA_MODELS_FILE`）、`[providers.<name>]` 表里 `models` 写的行，或者 provider 自己的 `/models` 列表（如果它公布了上限）。

## 配了好几个 provider 时怎么路由 {#routing-with-several-providers}

除了默认的那个，更多 provider 写成 `config.toml` 里的表：

```toml
[providers.claude]
family = "anthropic"
api_key = "{env:ANTHROPIC_API_KEY}"
models = ["claude-opus-5-5"]
prompt_cache_ttl = "1h"

[providers.deepseek]
family = "openai_compat"
base_url = "https://api.deepseek.com/v1"
api_key = "{env:DEEPSEEK_API_KEY}"
models = ["deepseek-flash", "deepseek-v4-pro"]
list_models = true            # 读它的 /models，拿到更多模型和它们的窗口
```

`ProviderRouter`（`noeta/backends/providers/routing.py`）把每次调用发给提供这个模型的 provider。一个模型 id 按这个顺序找归属：

1. 哪个 provider 的表（或模型文件里某行的 `"provider"`）点了它的名，就是哪个；同一个模型被点给两个 provider 算配置错误；
2. 否则，`/models` 列表里有它的第一个 provider，先看默认的那个；
3. 再否则，默认 provider。

模型的上限依次从模型目录、模型文件或表里的行、provider 公布的列表里取，都没有就用默认值并警告。列表只在需要它做决定时才去读，从配置的地址、用它自己的 key 读（`noeta/backends/providers/listing.py`），在 `~/.noeta/cache/models` 里存一天。返回 404 或 405 说明这个地址没有列表；其他失败则保留旧的那份，隔一段时间再试，间隔从 60 秒开始翻倍。

<Diagram name="model-routing" caption="模型走哪个 provider 只决定一次并记下，每台工人都照着记录发" />

**决定会被记下来。** 上面的解析过程里，只有配置那部分在每台机器上都一样：列表在不同机器上可能不同，也可能在某台上读失败。所以当任务用某个模型启动、或者切换到某个模型时（`/model`、无界面协议的 `set_model`、编辑器里选的），以及派出子代理时（用父任务的模型或它自己的），服务端只解析一次，把结果作为 `model_route` 记进任务配置：provider 的名字，以及列表给出的上限，和模型 id 放在一起（`ProviderRouter.route_record`，由 `LocalApp` 的客户端和 `Runtime.prepare_child` 调用）。之后每台工人都按记录把这个模型发给那个 provider、按那个上限发（`task_provider`），不管自己的列表怎么说。如果这台工人没配那个 provider，调用直接失败（`Fatal`），不会改发别处。

- 只有服务端能记路由：客户端（HTTP、stream-json、编辑器）不能设置 `model_route`（`noeta/host/_http_guard.py`、`noeta/host/_stream_json_control.py`）。
- 只有一个 provider 时什么都不记，日志和以前一样；每个进程自己查模型在列表里的上限。
- **旁路模型在实际跑它的工人上现解析**：插件在对话之外调用的模型（`compaction_model`、标题或权限分类器用的模型、`webfetch_model`、技能的 `model`），以及编辑器选了"默认"之后代理自己的模型。只有配置表或模型文件点了这个模型的名，它们才在各处一致。
- 不带参数的 `/model` 会列出所有 provider 的模型。

## 跨 provider 时的推理内容和调用编号

对话可以在 provider 之间换来换去，所以某个 provider 写下的两样东西，不能发给会拒收它们的另一个：

- **带签名的推理。** `Thinking` 块的签名或加密数据必须逐字节原样回传，而且只发回给签发它的那一类（`Thinking.family`）；Anthropic 的接口代码会丢掉别的类型写的推理。配了好几个地址时，路由还会在收到时把每块标成 `<family>@<endpoint>`，发出时只对那个地址还原类型，这样同一类型的两个地址也不会拿到对方签名的推理。
- **工具调用编号。** 运行器保证每个工具调用的编号在这个任务里没出现过（`noeta/core/runner/model.py` 里的 `unique_call_ids`）：答复、审批和子任务都按调用编号对应，网关不给编号或者重复用编号时，不能让一个调用拿到另一个调用的答复。Anthropic 的接口代码把它不接受的编号（别的 provider 写的）改写成它允许的字符，再接上原编号的哈希，每次改写结果都一样。

## 重试与超时 {#retries-and-timeouts}

<Diagram name="model-retry" caption="complete_with_retries：决定要不要再试的唯一一处" />

`complete_with_retries`（`noeta/core/retry.py`）包在任务发出的每一次模型调用外面：这一轮自己的，以及每个插件的 `view.complete` / `ctx.complete`：

- **临时错误和限流**按指数退避重试：1、2、4……最多 30 秒，一半固定一半随机（这样两个同时碰上 429 的子代理不会步调一致地一起重试），或者完全按 provider 给的 `Retry-After`。一共九次，大约等两分钟。
- **流开始之后又断了**（`StreamBroken`），在 provider 支持时改成一次性返回再发同一个请求（Anthropic 的 `complete_whole`，输出上限 64,000，300 秒），和 Claude Code 在流失败后的退路一样。一次性返回的调用超时了，就退回流式，之后不再切换。
- **工具调用的参数解析不了**（`MalformedToolArguments`，几乎总是流在调用中间断了），再要一次。
- **写明了数字的超长错误**，调小输出上限再发一次（见[上下文](./context.md#staying-inside-the-window)）；其余的 `ContextOverflow` 交给插件的 `on_context_overflow`；`Fatal` 让这一轮以 `llm_error` 失败。

每次重试都记一条内核的 `retry` 记录（第几次、等了多久、什么错误），前端据此显示"正在重试"，并丢掉失败那次已经流出来的文字，日志里也说得清发生了什么。运行器自己还有一道保险：一次模型调用开始了三次都没有记下回复（每次都是工人在调用中途崩溃），这一轮就失败，而不是无限循环。

**超时。** 每次调用都走流式，所以超时量的是"多久没有动静"，不是回答有多长：

| 限制 | 值 |
|---|---|
| 等响应头 | 600 秒（Claude Code 的请求超时；有的网关要等整个回答写完才开始发，在那之前什么都不发） |
| 流中间没有动静 | Anthropic 60 秒，OpenAI 两种格式 300 秒（`timeout_seconds`） |
| 一次性返回的调用 | 整个调用 300 秒 |
| 数 token | 10 秒；超时就用估算代替 |

取消一轮会立刻取消正在进行的调用，不管是在请求中还是在退避等待中。

**用不了的回复。** 因为到了输出上限被截断的回复，会让模型接着写，连续最多三次；还在推理、一个字都没写出来就被截断的回复不记下（只记它的用量）。以 `end_turn` 结束却什么都没有的回复，交给插件的 `on_empty_reply`（`empty_reply` 插件会让模型继续）。

## 对话之外的调用

插件或工具用 `view.complete` / `ctx.complete` 调用模型：走任务的 provider，和这一轮的调用一样会重试，但不算对话里的一步。这类调用的花费记成内核的 `spent` 记录（带用途，比如 `compaction`），所以任务的总花费和美元预算都会算上它。结果会影响之后请求的调用，经收件箱回来（[ADR-031](https://github.com/initxy/noeta/blob/main/docs/adr/031-background-plugin-work-returns-through-the-inbox.md)）。

花费上限见[上下文那一页](./context.md#budgets)。
