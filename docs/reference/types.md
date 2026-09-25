# Types, `@tool` and test doubles

The interfaces you implement, the event and message types you read, the tool-authoring API, and the offline test providers. All are imported from `noeta.sdk` unless noted.

## Extension interfaces

Implement one and mount it through the matching `Options` field.

| Interface | Shape | Mount via |
| --- | --- | --- |
| `Tool` | `name`, `description`, `risk_level`, `input_schema`, `invoke(arguments, ctx) -> ToolResult` | `allowed_tools` (or use `@tool`) |
| `LLMProvider` | `complete(request: LLMRequest) -> LLMResponse` | `provider` |
| `StreamingProvider` | `complete_streaming(request, on_delta, request_headers=None, should_abort=None) -> LLMResponse` | same object as the provider; deltas go to `HostConfig.delta_sink` |
| `Policy` | `decide(ctx: StepContext, view: View) -> Decision` | `policy` (a `(llm) -> Policy` factory with `.ref`) |
| `Guard` | `name`, `priority`, `check(action: ProposedAction, ctx: GuardContext) -> VerdictResult` | `guards` |
| `Observer` | `Callable[[EventEnvelope], None]` | `observers` |
| `ContentKindSpec` | `kind`, `renderer`, `hashes=None`, `policy="pinned"` | `content_channels` |
| `MemoryStore` | the file-per-page store behind the memory tools; open it to manage a store the agent uses | — |

### Tool types

| Type | Fields |
| --- | --- |
| `ToolResult` | `success`, `output=None`, `summary=""`, `artifacts=[]`, `images=[]`, `side_effects=[]`, `output_ref=None`, `file_changes=None` |
| `ToolContext` | `artifact_store`, `metadata={}` (carries `task_id` / `trace_id`), `background_runner=None`, `file_read_registry=None` |
| `FileReadRegistry` | `record(path, digest)`, `digest(path)` — the read-before-edit check |

### Guard types

| Type | Meaning |
| --- | --- |
| `ProposedAction` | union of `ProposedToolCall(call)`, `ProposedSpawnSubtask(decision)`, `ProposedFinish(answer)`; dispatch with `isinstance` |
| `GuardContext` | `task_id`, `governance`, `metadata`, `active_skills`, `subtask_depth`, `recent_tool_calls` |
| `VerdictResult` | build with `VerdictResult.allow()`, `.deny(reason)`, `.require_approval(reason)` |

### Policy types

| Type | Meaning |
| --- | --- |
| `Decision` | union a policy returns: finish, fail, tool calls, spawn one or many subtasks, yield for a human, wait on a timer or external event, patch state, request compaction |
| `StepContext` | per-step context passed to `decide` |
| `View` | the composed prompt: `plan_ref`, `segments`, `provider_tool_schemas` |

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

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | `str` | required | tool name the model sees |
| `version` | `str` | required | part of the agent identity; omitting it raises `TypeError` |
| `input_schema` | `dict` | required | JSON Schema shown to the model; arguments are not validated against it |
| `risk_level` | `str` | `"low"` | anything else is gated under `default` permission mode |
| `description` | `str` | `""` | what the model reads about the tool — write one |

Returns a `DecoratedTool` (also has `.ref`). Also callable directly: `tool(fn, name=..., version=..., input_schema=...)`.

### `create_sdk_mcp_server`

```python
create_sdk_mcp_server(name, version="1.0.0", tools=()) -> SdkMcpServer
```

Bundles `@tool` functions into an in-process server for `Options.mcp_servers`. Empty `name` raises `ValueError`; a non-`DecoratedTool` entry raises `TypeError`. The tools keep their bare names — the `mcp__{alias}__{tool}` prefix applies only to remote servers ([MCP servers](../guides/mcp.md)).

## Events

`EventEnvelope` is one record on a task's stream.

| Field | Meaning |
| --- | --- |
| `id`, `task_id`, `seq` | identity; `seq` is assigned by the log |
| `type` | event name, e.g. `TaskCreated`, `ToolCallApprovalRequested`, `TaskSuspended` |
| `payload` | typed dataclass for that `type` |
| `occurred_at`, `actor`, `origin`, `schema_version` | when and who |
| `trace_id`, `correlation_id`, `causation_id` | tracing links |

`envelope_to_dict(env)` gives the JSON-ready form (what an SSE stream sends).

```python
for env in client.events(task_id):
    print(env.seq, env.type)
# 1 TaskCreated
# 2 AgentBound
# 3 ModelBound
# ...
```

Related exports: `TaskStreamSummary` (a row of `Client.task_streams()`), `TaskSuspendedPayload`, `SuspendReason(kind, detail)`, `parse_suspend_reason(reason)`, and the kinds `SUSPEND_REASON_WAITING_HUMAN`, `SUSPEND_REASON_INTERRUPTED`, `SUSPEND_REASON_TURN_FAILED`.

## Transcript view

`as_messages(envelopes, content_store) -> list[ViewItem]` turns a stream into a readable transcript. The store must be the one the stream was written with. `Client.messages()` and `QueryResult.messages()` call it for you.

| `ViewItem` type | Fields |
| --- | --- |
| `UserMessage` | `text` — what the person typed |
| `AssistantMessage` | `text` |
| `InjectedMessage` | `text`, `origin` (`"system"` for host context, `"memory"` for recall) — never shown as the person |
| `ToolUse` | `call_id`, `tool_name`, `arguments` |
| `ToolResultView` | `call_id`, `tool_name`, `success`, `output: str \| None` |
| `Result` | `answer` (as a string), `status`; on `"failed"` the answer is the reason |

## Messages and content

| Type | Fields | Meaning |
| --- | --- | --- |
| `Message` | `role` (`system` / `user` / `assistant` / `tool`), `content: list[Block]`, `origin=None` (`human` / `system` / `memory`) | one model message; only the engine sets `origin` |
| `TextBlock` | `text` | text |
| `ImageBlock` | `source: ContentRef` | image input for `start` / `send_goal` / `query(images=...)` |
| `ToolUseBlock` | `call_id`, `tool_name`, `arguments` | the model calls a tool |
| `ToolResultBlock` | `call_id`, `output`, `success`, `error=None`, `images=None` | the result of one call |
| `ContentRef` | `hash`, `size`, `media_type` | a stored blob; looked up by `hash` |

## Provider request and response

| Type | Fields |
| --- | --- |
| `LLMRequest` | `model`, `messages`, `tools=[]`, `system=None`, `temperature=None`, `max_tokens=None`, `metadata={}`, `output_schema=None`, `thinking=None`, `effort=None` |
| `LLMResponse` | `stop_reason` (`tool_use` / `end_turn` / `max_tokens` / `error`), `content: list[Block]`, `usage=Usage()`, `raw=None` |
| `StreamDelta` | `kind` (`text` / `thinking`), `text`, `index` |
| `Usage` | `uncached`, `cache_read`, `cache_write`, `output`, `reasoning_tokens` (all `0`); properties `.input` = the three input counts summed, `.visible_output` = `max(0, output - reasoning_tokens)` |

## Test doubles

In `noeta.sdk.testing`, kept out of the root import so production code can't pull them in.

| Class | Fields | Meaning |
| --- | --- | --- |
| `FakeLLMProvider` | `responses=[]`, `received_requests=[]`, `responder=None` | returns scripted responses in order; raises `IndexError` when they run out |
| `FakeStreamingLLMProvider` | `responses=[]`, `deltas=[]`, `received_requests=[]`, `streamed_headers`, `streamed_calls`, `batch_calls` | same, and also pushes scripted `StreamDelta`s |

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

For tests that call the provider concurrently, pass `responder=lambda request: ...` and route by request content — the position-based script can't tell concurrent callers apart.

## Next

- [Custom tools](../guides/tools.md) — writing tools in practice
- [Offline tests](../guides/testing.md) — `FakeLLMProvider` in CI
- [The engine](../how-it-works/engine.md) — when guards and observers run
