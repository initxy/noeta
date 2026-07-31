# Types & testing

This page is for code that Noeta calls back into, or that reads what Noeta
recorded. It covers the extension interfaces you implement, the event and
message types you receive, the `@tool` authoring API, and the test doubles in
`noeta.sdk.testing`.

If you only want to run an agent, [query / Client](sdk-client.md) is enough.

## Extension interfaces

Implement one of these and mount it through the matching `Options` field.

| Interface | Mount via | Defined in |
| --- | --- | --- |
| `Tool` — metadata plus `invoke(arguments, ctx) -> ToolResult` | `allowed_tools` | `noeta/protocols/tool.py` |
| `ToolContext` / `ToolResult` | a tool's inputs and outputs | `noeta/protocols/tool.py` |
| `LLMProvider` — `complete(request) -> LLMResponse` | `provider` | `noeta/protocols/messages.py` |
| `StreamingProvider` / `StreamDelta` | implement alongside `LLMProvider`; consumed via `HostConfig.delta_sink` | `noeta/protocols/messages.py` |
| `Policy` — `decide(ctx, view) -> Decision` | `policy` | `noeta/protocols/policy.py` |
| `Guard` / `GuardContext` / `VerdictResult` | `guards` | `noeta/protocols/hooks.py` |
| `ProposedAction` and its members `ProposedToolCall` / `ProposedSpawnSubtask` / `ProposedFinish` | passed to `Guard.check` | `noeta/protocols/hooks.py` |
| `Observer` (an alias for `Subscriber`, i.e. `Callable[[EventEnvelope], None]`) | `observers` | `noeta/protocols/event_log.py` |
| `ContentKindSpec` | `content_channels` | `noeta/context/content_channel.py` |
| `Decision` — the union a custom `Policy` returns | returned by `Policy.decide` | `noeta/protocols/decisions.py` |
| `StepContext` / `View` | passed to a custom `Policy` | `noeta/protocols/step_context.py`, `view.py` |

`ToolResult` carries `success`, `output`, `summary`, `artifacts`, `images`,
`side_effects`, `output_ref` and `file_changes`. A guard dispatches on the
`ProposedAction` members with `isinstance`, which is why all three are exported
rather than just the union.

`MemoryStore` (`noeta.builtins.memory.impl`, lazily re-exported from
`noeta.sdk`) is the file-per-memory store behind the memory tools. A host that
manages memory pools opens the same store the agent writes, so both sides agree
on slugs and frontmatter.

## Authoring tools

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

Wraps `fn(arguments, ctx) -> ToolResult` as a `DecoratedTool`. `name`,
`version` and `input_schema` are **required** keywords — omitting `version`
raises `TypeError`, because the version feeds the identity fingerprint.
`risk_level` defaults to `"low"`.

`input_schema` is LLM-facing metadata, not a runtime validator, and
`description` is the model's single source of truth for what the tool does —
never repeat it in the system prompt. The decorator is also callable directly:
`tool(fn, name=..., version=..., input_schema=...)`.

### `create_sdk_mcp_server`

```python
create_sdk_mcp_server(name, version="1.0.0", tools=()) -> SdkMcpServer
```

Bundles `@tool` functions into an in-process (`"sdk"` transport) MCP server for
`Options.mcp_servers`. An empty `name` raises `ValueError`; a non-`DecoratedTool`
entry raises `TypeError`. `SdkMcpServer` is frozen, carrying `name`, `version`
and `tools`.

Its tools keep their bare `@tool` names. The `mcp__{alias}__{tool}` prefix
applies to remote servers only — see [Connect MCP](../how-to/connect-mcp.md).

## Events and envelopes

An `EventEnvelope` is one record on a task's stream. The envelope carries
`seq` / `type` / `actor` / `origin` / `trace_id` / `causation_id` — `seq` is
assigned by the log on append — and the payload is a typed dataclass selected by
`type`.

`envelope_to_dict(env) -> dict` (`client/wire.py`) produces the canonical
JSON-ready form, which is the shape an SSE stream consumes.

```python
from noeta.sdk import envelope_to_dict

for env in client.events(task_id):
    print(env.seq, env.type)
# → 1 TaskCreated
# → 2 ContextPlanComposed
# → 3 MessagesAppended
```

## Message projection

`as_messages(envelopes, content_store) -> list[ViewItem]` (`client/messages.py`)
is a pure projection of an envelope stream into the human-readable view. The
`content_store` must be the one **paired with** that stream, because the
projection dereferences large bodies through it.

`ViewItem` is the union of five frozen types:

| Type | Fields |
| --- | --- |
| `AssistantMessage` | `text` |
| `UserMessage` | `text` |
| `ToolUse` | `call_id`, `tool_name`, `arguments` |
| `ToolResultView` | `call_id`, `tool_name`, `success`, `output: str \| None` |
| `Result` | `answer`, `status` — on `"failed"`, `answer` holds the failure reason |

`Client.messages(task_id)` and `QueryResult.messages()` call this for you
against the right store, so reach for `as_messages` only when you hold the
envelopes and the store yourself.

## Content blocks

| Type | Shape | Notes |
| --- | --- | --- |
| `ContentRef` | `hash`, `size`, `media_type` | a reference into the ContentStore; lookup is by `hash` alone |
| `ImageBlock` | `source: ContentRef` | an image input block for `start` / `send_goal` / `query(images=…)` |
| `TextBlock` | `text` | plain assistant or user text |
| `ToolUseBlock` | `call_id`, `tool_name`, `arguments` | the model asking for a tool |
| `ToolResultBlock` | `call_id`, `output`, `success`, `error=None`, `images=None` | the answer to one `ToolUseBlock` |

A `Message` is `role` (`"system"` / `"user"` / `"assistant"` / `"tool"`),
`content: list[Block]`, and an optional `origin` (`"human"` / `"system"` /
`"memory"`). Only the Engine's recording path may write `origin`; a marker
forged in model or tool output is just text.

## Provider request and response

An `LLMProvider` implementation consumes an `LLMRequest` and returns an
`LLMResponse`.

**`LLMRequest`** — `model`, `messages`, `tools` (provider-shaped schema dicts),
`system`, `temperature`, `max_tokens`, `metadata`, `output_schema`, `thinking`,
`effort`.

**`LLMResponse`** — `stop_reason` (`"tool_use"` / `"end_turn"` /
`"max_tokens"` / `"error"`), `content: list[Block]`, `usage`, and an optional
`raw` dict for the untouched vendor payload.

**`Usage`** — the token counters the governance fold accumulates:

| Field | Meaning |
| --- | --- |
| `uncached` | input tokens billed at full rate |
| `cache_read` | input tokens served from the provider's KV cache |
| `cache_write` | input tokens written into that cache |
| `output` | generated tokens |
| `reasoning_tokens` | thinking tokens, where the provider reports them |
| `.input` (property) | `uncached + cache_read + cache_write` |
| `.visible_output` (property) | `max(0, output - reasoning_tokens)` — the user-facing answer size |

Splitting cached from uncached input is what makes the stable-prefix cache
measurable — see [Composer & cache](../concepts/composer-and-cache.md).

## Test doubles

`noeta.sdk.testing` holds the deterministic, network-free doubles a product
drives in its offline suite. They sit in a submodule so a production import can
never pull test material in by accident.

### `FakeLLMProvider`

A dataclass with three fields: `responses` (a scripted list of `LLMResponse`,
iterated in order), `received_requests` (every `LLMRequest` it saw), and
`responder` (an optional `(request) -> LLMResponse` callable).

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

An exhausted script raises `IndexError` from `complete`, so a runaway test fails
loudly instead of looping on the last response. `complete` is thread-safe, but
the positional cursor is order-dependent and therefore unusable under
concurrency: a test that drives a concurrent group passes a `responder` that
routes by request *content* instead. The responder runs outside the lock, so a
deliberately blocking responder cannot serialise its own callers.

## Next

- [Build custom tools](../how-to/build-custom-tools.md) — the task-oriented guide
- [Configure a provider](../how-to/configure-provider.md) — wiring a real adapter
- [Options](sdk-options.md) — where each interface mounts
- [Guard vs Observer](../concepts/guard-observer.md) — which hook to reach for
