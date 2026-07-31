# 类型与测试

本页面向的是那些 Noeta 会回调进来的代码，或者读取 Noeta 记录内容的代码。它涵盖你要实现的扩展接口、你会收到的事件与消息类型、`@tool` 编写 API，以及 `noeta.sdk.testing` 里的测试替身。

如果你只想跑一个 agent，[query / Client](sdk-client.md) 就够了。

## 扩展接口

实现其中之一，再通过对应的 `Options` 字段挂载它。

| 接口 | 挂载方式 | 定义于 |
| --- | --- | --- |
| `Tool` —— 元数据加上 `invoke(arguments, ctx) -> ToolResult` | `allowed_tools` | `noeta/protocols/tool.py` |
| `ToolContext` / `ToolResult` | 一个工具的输入与输出 | `noeta/protocols/tool.py` |
| `LLMProvider` —— `complete(request) -> LLMResponse` | `provider` | `noeta/protocols/messages.py` |
| `StreamingProvider` / `StreamDelta` | 与 `LLMProvider` 一并实现；通过 `HostConfig.delta_sink` 消费 | `noeta/protocols/messages.py` |
| `Policy` —— `decide(ctx, view) -> Decision` | `policy` | `noeta/protocols/policy.py` |
| `Guard` / `GuardContext` / `VerdictResult` | `guards` | `noeta/protocols/hooks.py` |
| `ProposedAction` 及其成员 `ProposedToolCall` / `ProposedSpawnSubtask` / `ProposedFinish` | 传给 `Guard.check` | `noeta/protocols/hooks.py` |
| `Observer`（`Subscriber` 的别名，即 `Callable[[EventEnvelope], None]`） | `observers` | `noeta/protocols/event_log.py` |
| `ContentKindSpec` | `content_channels` | `noeta/context/content_channel.py` |
| `Decision` —— 一个自定义 `Policy` 返回的联合类型 | 由 `Policy.decide` 返回 | `noeta/protocols/decisions.py` |
| `StepContext` / `View` | 传给一个自定义 `Policy` | `noeta/protocols/step_context.py`、`view.py` |

`ToolResult` 携带 `success`、`output`、`summary`、`artifacts`、`images`、`side_effects`、`output_ref` 和 `file_changes`。一个 guard 用 `isinstance` 在 `ProposedAction` 的各个成员上分派，这也是三者都被导出、而不只导出联合类型的原因。

`MemoryStore`（`noeta.builtins.memory.impl`，从 `noeta.sdk` 惰性重新导出）是记忆工具背后那个"一条记忆一个文件"的存储。管理记忆池的宿主打开的是 agent 写入的同一个存储，因此两边对 slug 和 frontmatter 的理解一致。

## 编写工具

### `@tool`

```python
from noeta.sdk import ToolResult, tool

@tool(
    name="word_count",
    version="1",
    risk_level="low",
    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    description="Count the words in a string.",
)
def word_count(arguments, ctx):
    return ToolResult(success=True, output=str(len(arguments["text"].split())))

print(word_count.name, word_count.risk_level)
# → word_count low
```

它把 `fn(arguments, ctx) -> ToolResult` 包装成一个 `DecoratedTool`。`name`、`version` 和 `input_schema` 是**必填**关键字参数——省略 `version` 会抛出 `TypeError`，因为版本会喂进身份指纹。`risk_level` 默认为 `"low"`。

`input_schema` 是面向 LLM 的元数据，不是运行时校验器；而 `description` 是"这个工具做什么"对模型而言的唯一事实来源——绝不要在系统 prompt 里重复它。这个装饰器也可以直接调用：`tool(fn, name=..., version=..., input_schema=...)`。

### `create_sdk_mcp_server`

```python
create_sdk_mcp_server(name, version="1.0.0", tools=()) -> SdkMcpServer
```

把若干 `@tool` 函数打包成一个进程内（`"sdk"` 传输）MCP 服务器，供 `Options.mcp_servers` 使用。空的 `name` 会抛出 `ValueError`；非 `DecoratedTool` 的条目会抛出 `TypeError`。`SdkMcpServer` 是冻结的，携带 `name`、`version` 和 `tools`。

它的工具保留自己裸的 `@tool` 名字。`mcp__{alias}__{tool}` 前缀只适用于远程服务器——见[连接 MCP 服务器](../how-to/connect-mcp.md)。

## 事件与信封

一个 `EventEnvelope` 是某个任务流上的一条记录。信封携带 `seq` / `type` / `actor` / `origin` / `trace_id` / `causation_id`——`seq` 由日志在追加时分配——而负载是一个按 `type` 选定的类型化 dataclass。

`envelope_to_dict(env) -> dict`（`client/wire.py`）产出规范化的、可直接 JSON 序列化的形态，也就是一条 SSE 流所消费的形状。

```python
from noeta.sdk import envelope_to_dict

for env in client.events(task_id):
    print(env.seq, env.type)
# → 1 TaskCreated
# → 2 ContextPlanComposed
# → 3 MessagesAppended
```

## 消息投影

`as_messages(envelopes, content_store) -> list[ViewItem]`（`client/messages.py`）是把一条信封流纯粹地投影成人类可读视图。`content_store` 必须是与那条流**配对的**那一个，因为投影会经由它解引用大体积正文。

`ViewItem` 是五个冻结类型的联合：

| 类型 | 字段 |
| --- | --- |
| `AssistantMessage` | `text` |
| `UserMessage` | `text` |
| `ToolUse` | `call_id`、`tool_name`、`arguments` |
| `ToolResultView` | `call_id`、`tool_name`、`success`、`output: str \| None` |
| `Result` | `answer`、`status` —— 为 `"failed"` 时，`answer` 里放的是失败原因 |

`Client.messages(task_id)` 和 `QueryResult.messages()` 已经替你对着正确的存储调用了它，因此只有当你自己同时握着信封和存储时，才需要动用 `as_messages`。

## 内容块

| 类型 | 形状 | 说明 |
| --- | --- | --- |
| `ContentRef` | `hash`、`size`、`media_type` | 指向 ContentStore 的引用；查找只按 `hash` |
| `ImageBlock` | `source: ContentRef` | 供 `start` / `send_goal` / `query(images=…)` 使用的图像输入块 |
| `TextBlock` | `text` | 普通的 assistant 或 user 文本 |
| `ToolUseBlock` | `call_id`、`tool_name`、`arguments` | 模型请求调用一个工具 |
| `ToolResultBlock` | `call_id`、`output`、`success`、`error=None`、`images=None` | 对一个 `ToolUseBlock` 的答复 |

一个 `Message` 由 `role`（`"system"` / `"user"` / `"assistant"` / `"tool"`）、`content: list[Block]` 和一个可选的 `origin`（`"human"` / `"system"` / `"memory"`）组成。只有 Engine 的记录路径才可以写 `origin`；在模型或工具输出里伪造出来的标记只是文本。

## Provider 的请求与响应

一个 `LLMProvider` 实现消费一个 `LLMRequest` 并返回一个 `LLMResponse`。

**`LLMRequest`** —— `model`、`messages`、`tools`（provider 形状的 schema dict）、`system`、`temperature`、`max_tokens`、`metadata`、`output_schema`、`thinking`、`effort`。

**`LLMResponse`** —— `stop_reason`（`"tool_use"` / `"end_turn"` / `"max_tokens"` / `"error"`）、`content: list[Block]`、`usage`，以及一个可选的 `raw` dict，放原封不动的厂商负载。

**`Usage`** —— 治理 fold 所累计的 token 计数器：

| 字段 | 含义 |
| --- | --- |
| `uncached` | 按全价计费的输入 token |
| `cache_read` | 由 provider 的 KV 缓存供给的输入 token |
| `cache_write` | 写入那个缓存的输入 token |
| `output` | 生成的 token |
| `reasoning_tokens` | thinking token，在 provider 会上报时 |
| `.input`（属性） | `uncached + cache_read + cache_write` |
| `.visible_output`（属性） | `max(0, output - reasoning_tokens)` —— 面向用户的答案大小 |

把已缓存和未缓存的输入分开统计，正是让稳定前缀缓存变得可度量的原因——见 [Composer 与缓存](../concepts/composer-and-cache.md)。

## 测试替身

`noeta.sdk.testing` 放的是产品在离线测试套件里驱动的那些确定性、无网络的替身。它们待在一个子模块里，因此一次生产导入绝不会不小心把测试用料拖进来。

### `FakeLLMProvider`

一个带三个字段的 dataclass：`responses`（一个脚本化的 `LLMResponse` 列表，按顺序取用）、`received_requests`（它见过的每个 `LLMRequest`），以及 `responder`（一个可选的 `(request) -> LLMResponse` 可调用对象）。

```python
from noeta.sdk import LLMResponse, Options, TextBlock, query
from noeta.sdk.testing import FakeLLMProvider

provider = FakeLLMProvider(responses=[
    LLMResponse(stop_reason="end_turn", content=[TextBlock(text="42")]),
])

result = query(Options(system_prompt="Be terse."), goal="What is 6 times 7?",
               provider=provider, workspace_dir=".")

print(result.answer())                  # → '42'
print(len(provider.received_requests))  # → 1
```

脚本耗尽后 `complete` 会抛出 `IndexError`，因此一个跑飞的测试会大声失败，而不是在最后一条响应上打转。`complete` 是线程安全的，但那个位置游标依赖顺序，因此在并发下不可用：驱动一组并发任务的测试要传一个按请求*内容*路由的 `responder`。responder 在锁之外运行，因此一个故意阻塞的 responder 不会把它自己的调用方串行化。

## 下一步

- [构建自定义工具](../how-to/build-custom-tools.md) —— 面向任务的指南
- [配置 Provider](../how-to/configure-provider.md) —— 接上一个真实适配器
- [Options](sdk-options.md) —— 每个接口挂在哪里
- [Guard 与 Observer](../concepts/guard-observer.md) —— 该用哪种钩子
