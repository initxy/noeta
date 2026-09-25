# Test offline

Test your agent without an API key or network: `FakeLLMProvider` replays
scripted model responses, and every run hands back its full event log to
assert on. The same tests run in CI on every push.

## Run one turn offline

`FakeLLMProvider` returns the `LLMResponse`s you give it, in order:

<!-- runnable: smoke -->
```python
from noeta.sdk import LLMResponse, Options, TextBlock, Usage, query
from noeta.sdk.testing import FakeLLMProvider

provider = FakeLLMProvider(responses=[
    LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="Hello from Noeta.")],
        usage=Usage(uncached=1, output=1),
    ),
])

result = query(
    Options(system_prompt="You are concise.", allowed_tools=()),
    goal="Say hello.",
    provider=provider,
)

for env in result:                      # the raw event stream
    print(f"{env.seq:>3}  {env.type:<22}  actor={env.actor}")
for item in result.messages():          # the folded conversation
    print(item)
print(result.answer())                  # just the answer

types = [env.type for env in result]
assert types[0] == "TaskCreated" and types[-1] == "TaskCompleted"
assert result.answer() == "Hello from Noeta."
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

`query` returns a `QueryResult`, which is the list of event envelopes plus two
views of it:

| Read it as | You get | Use it for |
| --- | --- | --- |
| iterate `result` | `EventEnvelope`s in `seq` order | asserting on what actually ran |
| `result.messages()` | the conversation | showing a user what happened |
| `result.answer()` | the final answer | the common case |

`answer()` raises `QueryFailedError` if the task did not complete, so a failed
run can never pass as a good answer.

## Assert a tool ran

Script a tool call, then check the `ToolCallStarted` events:

<!-- runnable: smoke -->
```python
from noeta.sdk import (
    LLMResponse, Options, TextBlock, ToolContext, ToolResult, ToolUseBlock,
    Usage, query, tool,
)
from noeta.sdk.testing import FakeLLMProvider


@tool(
    name="ping",
    version="1",
    risk_level="low",
    description="Return pong.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
def ping(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(success=True, output="pong")


provider = FakeLLMProvider(responses=[
    LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id="p1", tool_name="ping", arguments={})],
        usage=Usage(uncached=1, output=1),
    ),
    LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="Pinged.")],
        usage=Usage(uncached=1, output=1),
    ),
])

result = query(
    Options(system_prompt="Use the ping tool.", allowed_tools=(ping,)),
    goal="Ping.",
    provider=provider,
)

called = [e.payload.tool_name for e in result if e.type == "ToolCallStarted"]
assert called == ["ping"], called
assert result.answer() == "Pinged."
assert len(provider.received_requests) == 2
```

- Each scripted response is used once. Running out raises `IndexError`, which
  catches a model loop you did not expect.
- `provider.received_requests` holds every `LLMRequest` the agent sent, so you
  can check prompts and tool schemas too.

## Test an approval gate

Drive the turn with `Client` to check that a risky call waits and that a denied
call never runs:

<!-- runnable: smoke -->
```python
from noeta.sdk import (
    NEXT_GOAL_WAKE_HANDLE, Client, LLMResponse, Options, TextBlock, ToolContext,
    ToolResult, ToolUseBlock, Usage, tool,
)
from noeta.sdk.testing import FakeLLMProvider


@tool(
    name="delete_record",
    version="1",
    risk_level="high",
    description="Delete a record by id.",
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    },
)
def delete_record(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(success=True, output=f"deleted {arguments['id']}")


provider = FakeLLMProvider(responses=[
    LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id="d1", tool_name="delete_record",
                              arguments={"id": "42"})],
        usage=Usage(uncached=1, output=1),
    ),
    LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="Left it alone.")],
        usage=Usage(uncached=1, output=1),
    ),
])

options = Options(system_prompt="Manage records.", allowed_tools=(delete_record,))
with Client(options, provider=provider) as client:
    turn = client.start(goal="Delete record 42.")
    assert turn.status == "suspended" and turn.wake_handle == "approval-d1"

    turn = client.deny(turn.task_id, call_id="d1", reason="not today")
    assert turn.wake_handle == NEXT_GOAL_WAKE_HANDLE
    ran = [e for e in client.events(turn.task_id) if e.type == "ToolCallStarted"]
    assert ran == [], "a denied call must never run"
```

Here the `call_id` is yours, so the wake handle is predictable:
`approval-{call_id}`.

## Run it in CI

Save the tests under `tests/` and run them with pytest. The SDK runs
in-process, so the CI job is a plain Python job:

```yaml
  agent-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          enable-cache: true
      - run: uv sync --frozen
      - run: uv run pytest tests/ -v
```

## Add a live-model job

For prompt regressions you need a real model. Mark those tests so they run
only when asked for, and skip when no key is present:

```python
import os

import pytest

from noeta.sdk import Options, query
from noeta.sdk.providers import AnthropicProvider


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
def test_agent_answers():
    result = query(Options(system_prompt="Be brief.", allowed_tools=()),
                   goal="Reply with the word OK.",
                   provider=AnthropicProvider(), model="claude-sonnet-5")
    assert "OK" in str(result.answer())
```

Declare the marker in your `pyproject.toml` and exclude it by default
(`addopts = "-m 'not live'"`), then run `pytest -m live` in a separate CI job
with the key passed from a secret.

::: tip Streaming
`FakeStreamingLLMProvider`, also in `noeta.sdk.testing`, is the streaming twin
for testing code that consumes token deltas.
:::

## Next

- [Custom tools](tools.md)
- [Types and test doubles](../reference/types.md)
- [How the event log works](../how-it-works/event-log.md)
