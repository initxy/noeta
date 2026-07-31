# Your first agent

**What you'll build:** a custom tool defined with `@tool`, an `Options` recipe
that mounts it, and a `Client` that drives one turn — then you read back the
folded message view. Everything runs in-process against the offline
`FakeLLMProvider`, so there is no server and no API key.

Every symbol comes from `noeta.sdk`, the SDK's single public import surface.

## Prerequisites

- Python 3.11+
- `pip install noeta-sdk`

## 1. Define a tool

A tool is a plain function `fn(arguments, ctx) -> ToolResult` wrapped with
`@tool`. `version` is required: `(name, version, risk_level)` is the tool's
declared identity inside the compiled agent spec, so two behaviourally
different tools can never share one.

```python
from noeta.sdk import ToolContext, ToolResult, tool

_WORD_COUNT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


@tool(
    name="word_count",
    version="1",
    risk_level="low",
    input_schema=_WORD_COUNT_SCHEMA,
    description="Count the whitespace-separated words in `text`.",
)
def word_count(arguments: dict, ctx: ToolContext) -> ToolResult:
    text = str(arguments.get("text", ""))
    return ToolResult(success=True, output=f"{len(text.split())} words")
```

`input_schema` and `description` are LLM-facing metadata rendered into the
provider's tool schema. `arguments` is never validated against the schema at
call time — the function handles bad input itself.

`@tool` returns a `DecoratedTool`: one object that is both the runnable tool
and the carrier of its identity ref, so the two cannot drift apart.

## 2. Assemble the Options

`Options` is the frozen recipe: the system prompt, the tool list, the
permission mode, and any child agent definitions.

```python
from noeta.sdk import Options

options = Options(
    system_prompt="You count words. Use the word_count tool.",
    name="word-counter",
    allowed_tools=(word_count,),
    permission_mode="bypassPermissions",
)
```

`allowed_tools` is replacement, not addition. A tuple means *exactly* those
entries — decorated tools by value, built-in tools by name. `None` selects the
full built-in set: 11 names (`read`, `glob`, `grep`, `edit`, `write`,
`apply_patch`, `shell_run`, `shell_poll`, `shell_kill`, `webfetch`,
`web_search`), of which 10 mount with no extra configuration —
`web_search` is built only when `NOETA_WEB_SEARCH_API_KEY` is set. Memory,
browser, `open_app`, `run_skill_script` and MCP tools are not in that set; they
mount on an activation or a host injection. See the
[tool catalog](../reference/tools.md).

`permission_mode` decides which calls stop for approval:

| Mode | Gated calls |
| --- | --- |
| `default` | every tool whose `risk_level` is not `low` |
| `acceptEdits` | same, minus `edit` / `write` / `apply_patch` |
| `bypassPermissions` | none |

`word_count` is `low`, so it runs unattended under all three; we set
`bypassPermissions` to keep the walkthrough free of an approval round-trip.

## 3. Script a provider

`FakeLLMProvider` returns a pre-scripted sequence of `LLMResponse` objects and
records every request it received. A real deployment supplies a live provider
instead — see [Configure a provider](../how-to/configure-provider.md).

```python
from noeta.sdk import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.sdk.testing import FakeLLMProvider

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

The script calls `word_count` once, then finishes.

## 4. Drive the agent

```python
import tempfile
from pathlib import Path

from noeta.sdk import Client

with tempfile.TemporaryDirectory() as tmp:
    with Client(
        options,
        provider=provider,
        workspace_dir=Path(tmp),
        model="stub-model",
        multi_turn=False,
    ) as client:
        outcome = client.start(goal="How many words are in 'hello world from noeta'?")
        for msg in client.messages(outcome.task_id):
            print(msg)
```

`Client` is the same driver an embedding host uses. As a context manager it
guarantees `shutdown()` (observer teardown, worker stop, sandbox reap).
`multi_turn=False` lets the turn reach a real `TaskCompleted` terminal instead
of suspending to wait for the next goal.

Output:

```
UserMessage(text="How many words are in 'hello world from noeta'?")
ToolUse(call_id='wc-1', tool_name='word_count', arguments={'text': 'hello world from noeta'})
ToolResultView(call_id='wc-1', tool_name='', success=True, output='"4 words"')
AssistantMessage(text="That's 4 words.")
Result(answer="That's 4 words.", status='completed')
```

`ToolResultView.tool_name` is empty because the recorded result payload carries
only the `call_id`; pair it with the preceding `ToolUse` to recover the name.

### Or: one call instead of four lines

`query` is the same thing with the client lifecycle folded in. Swap it for the
whole block above (a scripted provider is single-use, so build a fresh one):

```python
from noeta.sdk import query

with tempfile.TemporaryDirectory() as tmp:
    result = query(
        options,
        "How many words are in 'hello world from noeta'?",
        provider=provider,
        workspace_dir=Path(tmp),
        model="stub-model",
    )

print(result.answer())     # raises QueryFailedError if the task did not complete
print(result.messages())   # the same folded view
```

`QueryResult` *is* the full `list[EventEnvelope]`, with `messages()` and
`answer()` folded and dereferenced before the temporary client is torn down.

## 5. What just happened

Every step — user message, tool call, tool result, assistant reply — was
appended to an event log as an immutable envelope; `messages()` folded that log
into the human-readable view. Storage defaults to in-memory. Point the client
at SQLite instead and the same fold recovers the same state in a fresh process:

```python
from noeta.sdk import HostConfig

client = Client(options, provider=provider, workspace_dir=Path(tmp),
                host_config=HostConfig(storage_path="noeta.sqlite"))
```

That is [event sourcing](../concepts/event-sourcing.md) at work. Gating, when
a mode enables it, happens in a [Guard](../concepts/guard-observer.md) that
suspends the turn until `client.approve()` or `client.deny()` resolves the
pending call.

## Next steps

- **Connect a real model** — [Configure a provider](../how-to/configure-provider.md).
- **Build more tools** — [Build custom tools](../how-to/build-custom-tools.md)
  covers risk levels, versioning, and bundling tools into an MCP server.
- **Fan out to subagents** — [Spawn subagents](../how-to/spawn-subagents.md).
- **Look up the full surface** — [SDK reference](../reference/sdk.md).
- **Read runnable code** — `examples/sdk_minimal.py`, `examples/custom_tool.py`
  and `examples/permission_gate.py` extend this pattern.
