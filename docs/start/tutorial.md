# Build a real agent

This tutorial builds an agent with a tool you write, an allowlist that fixes
what it may call, an approval gate that stops the call until you say yes, a
multi-turn conversation, and storage that outlives the process. About 60 lines
of Python, against a real Claude model.

**You need:** Python 3.11+, `uv pip install noeta-sdk`, and `ANTHROPIC_API_KEY`
set in your environment. The [quickstart](quickstart.md) is a good warm-up.

::: tip Output varies
The agent talks to a live model, so the wording and the `call_id`s you see will
differ from the output shown here. The statuses and the shape of each step will
not.
:::

## 1. Define a tool

A tool is a plain function `fn(arguments, ctx) -> ToolResult` wrapped in `@tool`:

```python
from noeta.sdk import ToolContext, ToolResult, tool


@tool(
    name="word_count",
    version="1",
    risk_level="high",
    description="Count the whitespace-separated words in `text`.",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
)
def word_count(arguments: dict, ctx: ToolContext) -> ToolResult:
    text = str(arguments.get("text", ""))
    return ToolResult(success=True, output=f"{len(text.split())} words")
```

- `description` and `input_schema` are what the model sees. Nothing validates
  `arguments` against the schema, so your function handles bad input.
- `version` is required; `(name, version, risk_level)` is the tool's identity.
- Counting words is harmless, but `risk_level="high"` gives step 3 an approval
  gate to show.

## 2. Choose what the agent may call

```python
from noeta.sdk import Options

options = Options(
    system_prompt="You count words. Always use the word_count tool.",
    name="word-counter",
    allowed_tools=(word_count,),
    permission_mode="default",
)
```

`allowed_tools` is the whole list, not an addition: this agent has
`word_count` and nothing else. Leave it out to get every built-in tool, or
mix both: `("Read", "Grep", word_count)`.

`permission_mode` decides which calls wait for a human:

| Mode | Waits for approval |
| --- | --- |
| `default` | every tool whose `risk_level` is not `low` |
| `acceptEdits` | same, except the built-in `Edit` and `Write` |
| `bypassPermissions` | nothing |

## 3. Run a turn and hit the gate

```python
from noeta.sdk import Client
from noeta.sdk.providers import AnthropicProvider

with Client(options, provider=AnthropicProvider(), model="claude-sonnet-5") as client:
    turn = client.start(goal="How many words are in 'hello world from noeta'?")
    print(turn.status, turn.wake_handle)
```

```
suspended approval-call_ln6dcrmn0w80aqou15fyyryt
```

The turn did not fail. The model asked for `word_count`, the permission check
saw a `high`-risk call, and the task **suspended**. `wake_handle` says what it
is waiting for: approval of that call. A suspended task holds no thread and
costs nothing while it waits.

## 4. Approve the call

Still inside the `with` block:

```python
    call_id = turn.wake_handle.removeprefix("approval-")
    turn = client.approve(turn.task_id, call_id=call_id)
    print(turn.status, turn.wake_handle)
    for item in client.messages(turn.task_id):
        print(item)
```

```
suspended noeta-code-next-goal
UserMessage(text="How many words are in 'hello world from noeta'?")
ToolUse(call_id='call_ln6dcrmn0w80aqou15fyyryt', tool_name='word_count', arguments={'text': 'hello world from noeta'})
ToolResultView(call_id='call_ln6dcrmn0w80aqou15fyyryt', tool_name='', success=True, output='"4 words"')
AssistantMessage(text='There are **4 words** in "hello world from noeta".')
```

The tool ran and the model answered. The task is suspended again, now on
`noeta-code-next-goal` (the constant `NEXT_GOAL_WAKE_HANDLE`): the conversation
is idle and waiting for your next message.

- `client.deny(task_id, call_id=..., reason=...)` refuses instead; the model is
  told and carries on.
- To decide in code, set `Options(can_use_tool=lambda name, args: ...)`. It
  returns `True` to allow, `False` to deny, and the decision is logged like a
  manual one.

## 5. Keep the conversation going

```python
    turn = client.send_goal(turn.task_id, goal="What did I just ask you?")
    print(client.messages(turn.task_id)[-1])
```

```
AssistantMessage(text='You just asked: "How many words are in \'hello world from noeta\'?" — and the answer was 4 words.')
```

A conversation is one task that keeps receiving messages. `task_id` is the only
handle you carry between turns.

## 6. Make it durable

By default everything lives in memory and dies with the process. Point
`HostConfig.storage_path` at a SQLite file:

```python
from noeta.sdk import HostConfig

db = HostConfig(storage_path="./noeta.sqlite")

with Client(options, provider=AnthropicProvider(), model="claude-sonnet-5",
            host_config=db) as client:
    ...  # steps 3–5 unchanged; print turn.task_id at the end
```

Now a different process, started later, picks up the same conversation from
nothing but the file and the `task_id`:

```python
with Client(options, provider=AnthropicProvider(), model="claude-sonnet-5",
            host_config=db) as client:
    print(client.task_status(task_id))
    turn = client.send_goal(task_id, goal="Count the words in 'one two three'.")
    print(turn.status, turn.wake_handle)
```

```
TaskStatus(task_id='task-53b3…', status='suspended', closed=False, wake_handle='noeta-code-next-goal', parent_task_id=None)
suspended approval-call_xwfag88siz0teynq8on9bi57
```

The new client was never told what happened. It rebuilt the task from the
event log and carried on, gate included. The same `storage_path` accepts a
`postgresql://` DSN when several hosts share one store.

## Next

- [Custom tools](../guides/tools.md): results, failures, risk levels, bundling
- [Connect a model](../guides/models.md): OpenAI-compatible gateways and your own model ids
- [Test offline](../guides/testing.md): script the model and assert on the event log
