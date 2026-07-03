# Your first agent: build an SDK agent in 20 minutes

**What you'll do:** define a custom tool with `@tool`, assemble `Options`,
drive an agent with `Client`, and inspect the resulting message stream.
Everything runs in-process — no server, no API key. We use the
`FakeLLMProvider` so the example is fully offline and deterministic.

## Prerequisites

- Python 3.11+
- `noeta-sdk` installed (`uv pip install -e packages/noeta-sdk` from a
  local checkout, or via the git URL subdirectory)

## 1. Define a tool

Tools are plain functions wrapped with the `@tool` decorator. The
function takes `(arguments: dict, ctx: ToolContext)` and returns a
`ToolResult`. The `version` field is required — it feeds the tool's
identity fingerprint, so changing it tells the runtime the tool's
behavior may have changed.

```python
from noeta.sdk import tool
from noeta.protocols.tool import ToolContext, ToolResult

_WORD_COUNT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

@tool(name="word_count", version="1", risk_level="low",
      input_schema=_WORD_COUNT_SCHEMA)
def word_count(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Count whitespace-separated words."""
    text = str(arguments.get("text", ""))
    count = len(text.split())
    return ToolResult(success=True, output=f"{count} words")
```

The `input_schema` is LLM-facing metadata — it tells the model what
arguments the tool expects. It is not validated at call time; the
function itself is responsible for handling bad input.

## 2. Build the Options

`Options` is the frozen recipe for your agent. It holds the system
prompt, the tool allow-list, the permission mode, and any child agent
definitions.

```python
from noeta.sdk import Options

options = Options(
    system_prompt="You count words. Use the word_count tool.",
    name="word-counter",
    allowed_tools=(word_count,),
    permission_mode="bypassPermissions",
)
```

`allowed_tools` controls which tools the model can call. Pass `None` to
get all 13 built-in tools, or pass a tuple of `DecoratedTool` instances
(like our `word_count`) to restrict the surface.

`permission_mode="bypassPermissions"` means tool calls are not gated —
useful for a low-risk tool like `word_count`. For tools that write files
or run shell commands, use `"default"` (the user must approve each call)
or `"acceptEdits"` (edits are auto-approved, shell calls still need
approval).

## 3. Create a scripted provider

For this tutorial we use `FakeLLMProvider` — a deterministic double that
returns a scripted sequence of responses. In a real deployment you would
use `AnthropicProvider` or `OpenAICompatProvider` instead.

```python
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.protocols.messages import (
    LLMResponse, TextBlock, ToolUseBlock, Usage,
)

provider = FakeLLMProvider(
    responses=[
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="wc-1",
                    tool_name="word_count",
                    arguments={"text": "hello world from noeta"},
                )
            ],
            usage=Usage(uncached=1, output=1),
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="That's 4 words.")],
            usage=Usage(uncached=1, output=1),
        ),
    ]
)
```

The scripted provider calls `word_count` once (with "hello world from
noeta"), then finishes with "That's 4 words."

## 4. Drive the agent

```python
from pathlib import Path
import tempfile
from noeta.sdk import Client

with tempfile.TemporaryDirectory() as tmp:
    client = Client(
        options,
        provider=provider,
        workspace_dir=Path(tmp),
        model="stub-model",
        multi_turn=False,
    )
    try:
        outcome = client.start(goal="How many words are in 'hello world from noeta'?")
        messages = client.messages(outcome.task_id)
        for msg in messages:
            print(msg)
    finally:
        client.shutdown()
```

`Client` is the in-process equivalent of `python -m noeta.agent` — it
creates a temporary task, drives it to a terminal state, and shuts down.
`client.messages(task_id)` returns the folded human-readable view: user
message, tool use, tool result, assistant reply.

Run it and you should see something like:

```
UserMessage(text="How many words are in 'hello world from noeta'?")
ToolUse(call_id='wc-1', tool_name='word_count', arguments={'text': 'hello world from noeta'})
ToolResultView(call_id='wc-1', tool_name='word_count', success=True, output='4 words')
AssistantMessage(text="That's 4 words.")
Result(answer="That's 4 words.", status='completed')
```

## 5. What just happened

Every step — the user message, the tool call, the tool result, the
assistant reply — was appended to an in-memory EventLog. The `messages()`
call folded that log into the human view. If you had pointed the client
at a SQLite file instead of `:memory:`, you could shut down the process,
start a new one, and fold the same log to recover the exact same state.
That is [Event sourcing](../concepts/event-sourcing.md) in action.

The tool call was not gated because we set
`permission_mode="bypassPermissions"`. With `"default"`, a
`PermissionGuard` would have intercepted the call and required explicit
approval via `client.approve()`. See
[Guard vs Observer](../concepts/guard-observer.md) for how this works.

## Next steps

- **Connect a real model** — [Configure a provider](../how-to/configure-provider.md)
  shows how to wire Anthropic or OpenAI-compatible endpoints.
- **Build more tools** — [Build custom tools](../how-to/build-custom-tools.md)
  covers `risk_level`, tool versions, and bundling tools into an MCP server.
- **Fan out to sub-agents** — [Spawn subagents](../how-to/spawn-subagents.md)
  demonstrates parallel task execution.
- **Look up the full SDK surface** — [SDK reference](../reference/sdk.md)
  documents every symbol in `noeta.sdk`.
- **Run the examples** — `examples/sdk_minimal.py` and
  `examples/custom_tool.py` extend this pattern with more detail.
