# 类型、`@tool` 与测试替身

你要实现的接口、要读的事件和消息类型、写工具的 API，以及离线测试用的 provider。没特别说明的都从 `noeta.sdk` 导入。

## 扩展接口

实现其中一个，再通过对应的 `Options` 字段挂上去。

| 接口 | 形状 | 挂到 |
| --- | --- | --- |
| `Tool` | `name`、`description`、`risk_level`、`input_schema`、`invoke(arguments, ctx) -> ToolResult` | `allowed_tools`（或者用 `@tool`） |
| `LLMProvider` | `complete(request: LLMRequest) -> LLMResponse` | `provider` |
| `StreamingProvider` | `complete_streaming(request, on_delta, request_headers=None, should_abort=None) -> LLMResponse` | 和 provider 是同一个对象；增量送到 `HostConfig.delta_sink` |
| `Policy` | `decide(ctx: StepContext, view: View) -> Decision` | `policy`（一个带 `.ref` 的 `(llm) -> Policy` 工厂） |
| `Guard` | `name`、`priority`、`check(action: ProposedAction, ctx: GuardContext) -> VerdictResult` | `guards` |
| `Observer` | `Callable[[EventEnvelope], None]` | `observers` |
| `ContentKindSpec` | `kind`、`renderer`、`hashes=None`、`policy="pinned"` | `content_channels` |
| `MemoryStore` | 记忆工具背后一页一个文件的存储；宿主要管理 agent 用的记忆时直接打开它 | — |

### 工具相关类型

| 类型 | 字段 |
| --- | --- |
| `ToolResult` | `success`、`output=None`、`summary=""`、`artifacts=[]`、`images=[]`、`side_effects=[]`、`output_ref=None`、`file_changes=None` |
| `ToolContext` | `artifact_store`、`metadata={}`（带 `task_id` / `trace_id`）、`background_runner=None`、`file_read_registry=None` |
| `FileReadRegistry` | `record(path, digest)`、`digest(path)`，用来检查“先读再改” |

### Guard 相关类型

| 类型 | 说明 |
| --- | --- |
| `ProposedAction` | `ProposedToolCall(call)`、`ProposedSpawnSubtask(decision)`、`ProposedFinish(answer)` 三者之一；用 `isinstance` 区分 |
| `GuardContext` | `task_id`、`governance`、`metadata`、`active_skills`、`subtask_depth`、`recent_tool_calls` |
| `VerdictResult` | 用 `VerdictResult.allow()`、`.deny(reason)`、`.require_approval(reason)` 构造 |

### Policy 相关类型

| 类型 | 说明 |
| --- | --- |
| `Decision` | policy 的返回值：完成、失败、调用工具、派一个或多个子任务、等人、等定时器或外部事件、改状态、请求压缩 |
| `StepContext` | 传给 `decide` 的单步上下文 |
| `View` | 组装好的提示词：`plan_ref`、`segments`、`provider_tool_schemas` |

## `@tool`

```python
from noeta.sdk import ToolResult, tool

@tool(
    name="word_count",
    version="1",
    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    description="Count the words in a string.",
)
def word_count(arguments, ctx):
    return ToolResult(success=True, output=str(len(arguments["text"].split())))
```

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `name` | `str` | 必填 | 模型看到的工具名 |
| `version` | `str` | 必填 | 算进 agent 身份；不写会抛 `TypeError` |
| `input_schema` | `dict` | 必填 | 给模型看的 JSON Schema；运行时不会拿它校验参数 |
| `risk_level` | `str` | `"low"` | 不是 `low` 的话，`default` 模式下要审批 |
| `description` | `str` | `""` | 模型对这个工具的全部了解，一定要写 |

返回 `DecoratedTool`（带 `.ref`）。也可以直接调用：`tool(fn, name=..., version=..., input_schema=...)`。

### `create_sdk_mcp_server`

```python
create_sdk_mcp_server(name, version="1.0.0", tools=()) -> SdkMcpServer
```

把 `@tool` 函数打包成一个进程内 server，放进 `Options.mcp_servers`。`name` 为空抛 `ValueError`；混进不是 `DecoratedTool` 的东西抛 `TypeError`。工具保持原名，`mcp__{alias}__{tool}` 前缀只用于远程 server（见 [MCP server](../guides/mcp.md)）。

## 事件

`EventEnvelope` 是任务事件流里的一条记录。

| 字段 | 说明 |
| --- | --- |
| `id`、`task_id`、`seq` | 标识；`seq` 由日志分配 |
| `type` | 事件名，比如 `TaskCreated`、`ToolCallApprovalRequested`、`TaskSuspended` |
| `payload` | 对应 `type` 的 dataclass |
| `occurred_at`、`actor`、`origin`、`schema_version` | 什么时候、谁写的 |
| `trace_id`、`correlation_id`、`causation_id` | 链路追踪 |

`envelope_to_dict(env)` 转成可以直接序列化成 JSON 的形式（SSE 推送用的就是它）。

```python
for env in client.events(task_id):
    print(env.seq, env.type)
# 1 TaskCreated
# 2 AgentBound
# 3 ModelBound
# ...
```

相关导出：`TaskStreamSummary`（`Client.task_streams()` 的一行）、`TaskSuspendedPayload`、`SuspendReason(kind, detail)`、`parse_suspend_reason(reason)`，以及 `SUSPEND_REASON_WAITING_HUMAN`、`SUSPEND_REASON_INTERRUPTED`、`SUSPEND_REASON_TURN_FAILED`。

## 对话记录

`as_messages(envelopes, content_store) -> list[ViewItem]` 把事件流转成可读的对话记录。store 必须是写这条流时用的那个。`Client.messages()` 和 `QueryResult.messages()` 已经替你调好了。

| `ViewItem` 类型 | 字段 |
| --- | --- |
| `UserMessage` | `text`：用户自己输入的 |
| `AssistantMessage` | `text` |
| `InjectedMessage` | `text`、`origin`（宿主上下文是 `"system"`，记忆召回是 `"memory"`），不会被当成用户说的话 |
| `ToolUse` | `call_id`、`tool_name`、`arguments` |
| `ToolResultView` | `call_id`、`tool_name`、`success`、`output: str \| None` |
| `Result` | `answer`（字符串形式）、`status`；`"failed"` 时 answer 是失败原因 |

## 消息与内容

| 类型 | 字段 | 说明 |
| --- | --- | --- |
| `Message` | `role`（`system` / `user` / `assistant` / `tool`）、`content: list[Block]`、`origin=None`（`human` / `system` / `memory`） | 发给模型的一条消息；`origin` 只有引擎能设 |
| `TextBlock` | `text` | 文字 |
| `ImageBlock` | `source: ContentRef` | `start` / `send_goal` / `query(images=...)` 的图片输入 |
| `ToolUseBlock` | `call_id`、`tool_name`、`arguments` | 模型发起一次工具调用 |
| `ToolResultBlock` | `call_id`、`output`、`success`、`error=None`、`images=None` | 一次调用的结果 |
| `ContentRef` | `hash`、`size`、`media_type` | 一块存储内容，按 `hash` 查找 |

## Provider 的请求与响应

| 类型 | 字段 |
| --- | --- |
| `LLMRequest` | `model`、`messages`、`tools=[]`、`system=None`、`temperature=None`、`max_tokens=None`、`metadata={}`、`output_schema=None`、`thinking=None`、`effort=None` |
| `LLMResponse` | `stop_reason`（`tool_use` / `end_turn` / `max_tokens` / `error`）、`content: list[Block]`、`usage=Usage()`、`raw=None` |
| `StreamDelta` | `kind`（`text` / `thinking`）、`text`、`index` |
| `Usage` | `uncached`、`cache_read`、`cache_write`、`output`、`reasoning_tokens`（都默认 `0`）；属性 `.input` 是三项输入之和，`.visible_output` 是 `max(0, output - reasoning_tokens)` |

## 测试替身

放在 `noeta.sdk.testing` 里，不从根模块导出，生产代码不会误用。

| 类 | 字段 | 说明 |
| --- | --- | --- |
| `FakeLLMProvider` | `responses=[]`、`received_requests=[]`、`responder=None` | 按顺序返回预设的响应；用完了抛 `IndexError` |
| `FakeStreamingLLMProvider` | `responses=[]`、`deltas=[]`、`received_requests=[]`、`streamed_headers`、`streamed_calls`、`batch_calls` | 同上，另外会推送预设的 `StreamDelta` |

```python
from noeta.sdk import LLMResponse, Options, TextBlock, query
from noeta.sdk.testing import FakeLLMProvider

provider = FakeLLMProvider(responses=[
    LLMResponse(stop_reason="end_turn", content=[TextBlock(text="42")]),
])
result = query(Options(system_prompt="Be terse."), goal="What is 6 times 7?",
               provider=provider, workspace_dir=".")
print(result.answer())                  # 42
print(len(provider.received_requests))  # 1
```

测试里并发调用 provider 时，传 `responder=lambda request: ...`，按请求内容决定返回什么；按顺序取的脚本分不清是哪个调用方。

## 下一步

- [自定义工具](../guides/tools.md)：实际怎么写工具
- [离线测试](../guides/testing.md)：在 CI 里用 `FakeLLMProvider`
- [引擎](../how-it-works/engine.md)：guard 和 observer 什么时候运行
