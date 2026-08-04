# 快速上手

从一个空终端到跑完一轮 agent，只需五分钟 —— 不需要 API key，不需要网络，也不需要 server。你会装上一个包，用一个脚本化的离线模型跑一轮，读一读它产生的事件账本，最后换上真实的 provider。

你导入的一切都来自 `noeta.sdk`，也就是 SDK 唯一的公共面。

## 1. 安装

```bash
uv pip install noeta-sdk
```

`noeta-runtime` —— 那个纯内核 —— 会作为传递依赖一起装上，你从不直接导入它。需要 Python 3.11 或更高版本。

检查安装：

```bash
python -c "import noeta.sdk; print(noeta.sdk.Options)"
```

```
<class 'noeta.client.options.Options'>
```

## 2. 离线跑一轮

`FakeLLMProvider` 是一个脚本化、确定性的 LLM 替身。你把希望模型"返回"的响应交给它，它按顺序回放 —— 所以第一次运行既不需要凭证，也不碰网络。

`Options` 是 agent 的配方。`query` 构建一个用完即弃的客户端，把一轮驱动到终态，然后把发生的一切交还给你。

```python
from noeta.sdk import LLMResponse, Options, TextBlock, Usage, query
from noeta.sdk.testing import FakeLLMProvider

provider = FakeLLMProvider(
    responses=[
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="Hello from Noeta.")],
            usage=Usage(uncached=1, output=1),
        )
    ]
)

result = query(
    Options(
        system_prompt="You are concise.",
        allowed_tools=("Read",),
        permission_mode="bypassPermissions",
    ),
    goal="Say hello.",
    provider=provider,
    model="stub-model",
)

print(result.answer())
```

```
Hello from Noeta.
```

这就是完整的一轮 agent：目标被记录下来，模型被问了一次，Task 到达 `TaskCompleted` 终态。

## 3. 读账本

Noeta 把每一轮记录成一条 append-only 的事件信封流，而 Task 状态永远是通过 fold 这条流重新算出来的。`query` 返回一个 `QueryResult`，它**就是** `list[EventEnvelope]`，外加两个基于它的 fold 视图。

```python
from noeta.sdk import LLMResponse, Options, TextBlock, Usage, query
from noeta.sdk.testing import FakeLLMProvider

result = query(
    Options(
        system_prompt="You are concise.",
        allowed_tools=("Read",),
        permission_mode="bypassPermissions",
    ),
    goal="Say hello.",
    provider=FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="Hello from Noeta.")],
                usage=Usage(uncached=1, output=1),
            )
        ]
    ),
    model="stub-model",
)

# 1. The raw ledger — `result` IS a list of event envelopes.
for env in result:
    print(f"{env.seq:>3}  {env.type:<22}  actor={env.actor}")

# 2. The folded, human-readable projection.
print()
for item in result.messages():
    print(item)

# 3. Just the terminal answer.
print()
print(result.answer())
```

```
  0  TaskCreated             actor=engine
  1  AgentBound              actor=engine
  2  ModelBound              actor=engine
  3  ContextContentRecorded  actor=plugin:environment
  4  MessagesAppended        actor=engine
  5  TaskStarted             actor=engine
  6  ContextPlanComposed     actor=engine
  7  LLMRequestStarted       actor=llm
  8  LLMResponseRecorded     actor=llm
  9  LLMRequestFinished      actor=llm
 10  MessagesAppended        actor=engine
 11  TaskSnapshot            actor=engine
 12  TaskCompleted           actor=engine

UserMessage(text='Say hello.')
AssistantMessage(text='Hello from Noeta.')
Result(answer='Hello from Noeta.', status='completed')

Hello from Noeta.
```

同一次运行有三种读法，从生到熟：

| 调用 | 返回 | 用来做什么 |
| --- | --- | --- |
| 遍历 `result` | 按 `seq` 排序的 `EventEnvelope` 对象 | 审计、调试、对"实际跑了什么"下断言 |
| `result.messages()` | fold 出来的对话视图 | 把发生的事情展示给用户 |
| `result.answer()` | 只要终态答案 | 最常见的场景 |

`answer()` 是严格的：如果 Task 失败或从未到达终态，它会抛 `QueryFailedError`，所以一次失败永远不会被误当成一个成功的答案。

## 4. 换上真实的 provider

provider 是**接线**，不是身份 —— `compile_options` 从不读取它，它也被排除在相等性判断之外。同一份 `Options` 无论由哪个厂商提供服务都编译成同一个 agent，所以下面这两行就是全部改动。

Anthropic（省略 `api_key` 时回退到 `ANTHROPIC_API_KEY`）：

```python
from noeta.sdk.providers import AnthropicProvider

provider = AnthropicProvider(api_key="sk-ant-…")
result = query(options, goal="Say hello.", provider=provider,
               model="claude-sonnet-4-5-20250929")
```

任意 OpenAI 兼容的 `/chat/completions` 端点（回退到 `OPENAI_API_KEY`）：

```python
from noeta.sdk.providers import OpenAICompatProvider

provider = OpenAICompatProvider(base_url="https://api.openai.com/v1",
                                api_key="sk-…")
result = query(options, goal="Say hello.", provider=provider, model="gpt-4o")
```

各适配器住在 `noeta.sdk.providers` 这个惰性子模块里，因此导入 `noeta.sdk` 并不会把 `httpx` 拉进来，除非你真的构建了一个网络 provider。`model` 必须是你的端点确实提供的 id。

## 5. 接下来去哪

- **构建一个真实的 agent** —— [你的第一个 agent](first-agent.md) 会加上一个自定义 `@tool`、一道审批闸门，以及一个多轮的 `Client`。
- **指向你自己的网关** —— [配置 Provider](../how-to/configure-provider.md)。
- **理解这本账本** —— [核心概念](../concepts/index.md)，从[事件溯源](../concepts/event-sourcing.md)开始。
- **查一个签名** —— [SDK 参考](../reference/sdk.md)。
