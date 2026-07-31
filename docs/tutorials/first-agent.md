# Your first agent

The [quickstart](quickstart.md) ran one scripted turn. This tutorial builds a
real agent on top of it: a tool you write yourself, an allowlist that decides
what the agent may call, an approval gate that stops a risky call until you say
yes, and a `Client` that holds a multi-turn conversation. It still runs entirely
offline against `FakeLLMProvider`, so the whole tutorial reproduces with no
credentials, and every symbol comes from `noeta.sdk`. Steps 4–6 build one
program together — the indented blocks in steps 5 and 6 continue the `with`
block opened in step 4.

**Prerequisites:** Python 3.11+, `uv pip install noeta-sdk`, and the
[quickstart](quickstart.md) behind you.

## 1. Define a tool

A tool is a plain function `fn(arguments, ctx) -> ToolResult` wrapped with
`@tool`. The decorator's arguments are the tool's **declared identity** and its
**model-facing contract** — hand-written on purpose, never scraped from the
docstring.

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


print(word_count.ref)
```

```
ToolRef(name='word_count', version='1', risk_level='high')
```

Four things to know:

- `version` is **required**. `(name, version, risk_level)` is the identity
  recorded in the compiled agent spec, so two behaviourally different tools can
  never share one.
- `description` is the single source of truth for what the model thinks the tool
  does. It is rendered into the provider's tool schema and never repeated in the
  system prompt.
- `input_schema` is LLM-facing metadata, not a validator. Nothing checks
  `arguments` against it at call time — your function handles bad input.
- `risk_level` (`"low"` / `"medium"` / `"high"`) is what the permission system
  reads. Counting words is harmless; we mark it `"high"` anyway so step 4 has an
  approval gate to demonstrate.

`@tool` returns a `DecoratedTool` — both the runnable tool and the carrier of
its `.ref`, so the closure and its recorded identity cannot drift apart.

## 2. Assemble the Options

`Options` is the agent recipe: prompt, tool set, permission mode, child agents.

```python
from noeta.sdk import Options

options = Options(
    system_prompt="You count words. Use the word_count tool.",
    name="word-counter",
    allowed_tools=(word_count,),
    permission_mode="default",
)
```

`allowed_tools` is a **replacement** allowlist, not an addition. A tuple means
*exactly* those entries — decorated tools by value, built-in tools by name — so
this agent has `word_count` and nothing else. `None` (the default) selects the
full built-in set: `read`, `glob`, `grep`, `edit`, `write`, `apply_patch`,
`shell_run`, `shell_poll`, `shell_kill`, `webfetch`, `web_search`. `()` means no
tools. `disallowed_tools` subtracts from whichever base list applies; it never
adds.

`permission_mode` decides which calls stop for human approval:

| Mode | Gated calls |
| --- | --- |
| `default` | every tool whose `risk_level` is not `low` |
| `acceptEdits` | same, minus the built-in `edit` / `write` / `apply_patch` |
| `bypassPermissions` | none |

`word_count` is `high`, so under `default` it will suspend and wait for you.

## 3. Script the model

`FakeLLMProvider` replays a list of `LLMResponse` objects in order. Script the
two model rounds this turn needs — first the tool call, then the summary — plus
one more for the follow-up turn in step 6.

```python
from noeta.sdk import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.sdk.testing import FakeLLMProvider


def say(text: str) -> LLMResponse:
    return LLMResponse(stop_reason="end_turn", content=[TextBlock(text=text)],
                       usage=Usage(uncached=1, output=1))


provider = FakeLLMProvider(responses=[
    LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id="wc-1", tool_name="word_count",
                              arguments={"text": "hello world from noeta"})],
        usage=Usage(uncached=1, output=1),
    ),
    say("That's 4 words."),
    say("You asked me to count the words in 'hello world from noeta'."),
])
```

A scripted provider is single-use — each response is consumed once, in order.

## 4. Drive the turn, and approve the call

`Client` is the same driver an embedding host uses. As a context manager it
guarantees `shutdown()` — observer teardown, worker stop, sandbox reap.

```python
import tempfile
from pathlib import Path
from noeta.sdk import Client

with tempfile.TemporaryDirectory() as tmp:
    with Client(options, provider=provider, workspace_dir=Path(tmp),
                model="stub-model") as client:
        turn = client.start(goal="How many words are in 'hello world from noeta'?")
        print(turn.status, turn.wake_handle)
```

```
suspended approval-wc-1
```

The turn did not fail — it **suspended**. The permission guard saw a `high`-risk
call under `permission_mode="default"`, so the task parked itself on a wake
condition and is now waiting for a human. `wake_handle` names exactly what it
waits for: approval of call `wc-1`.

Resolve it and the turn resumes where it stopped:

```python
        turn = client.approve(turn.task_id, call_id="wc-1")
        print(turn.status, turn.wake_handle)
        for item in client.messages(turn.task_id):
            print(item)
```

```
suspended noeta-code-next-goal

UserMessage(text="How many words are in 'hello world from noeta'?")
ToolUse(call_id='wc-1', tool_name='word_count', arguments={'text': 'hello world from noeta'})
ToolResultView(call_id='wc-1', tool_name='', success=True, output='"4 words"')
AssistantMessage(text="That's 4 words.")
```

Still `suspended`, but on a different handle: `noeta-code-next-goal` (the
constant `NEXT_GOAL_WAKE_HANDLE`) means "this conversation is idle, waiting for
the next thing you say". That is the resting state of a healthy multi-turn agent
— a suspended task costs nothing while it sleeps. `client.deny(...)` is the
other half; the model is told the call was refused and decides what to do next.

(`ToolResultView.tool_name` is empty because the recorded result payload carries
only the `call_id`. Pair it with the preceding `ToolUse` to recover the name.)

## 5. What happened inside that turn

<p align="center">
  <img src="../assets/diagrams/turn-sequence.svg" alt="One Noeta turn — host code to Client to Engine to Provider to Tool to EventLog, and the return path" width="820">
</p>

One turn is the Engine's `run_one_step`, and it loops internally rather than
returning after every model round-trip:

1. **Compose** — fold the log into a `View`: prompt and tool schemas in the
   byte-stable `stable_prefix`, residents in `semi_stable`, the conversation in
   `dynamic_suffix`.
2. **Decide** — the policy asks the provider and gets a `Decision` (here,
   `tool_calls`).
3. **Dispatch** — guards run first (that is where the approval gate fired); an
   allowed call executes, its result is appended, and the loop returns to step 1.
4. **Settle** — a non-`tool_calls` decision ends the step at a suspend or a
   terminal.

Nothing is held in memory between steps: state is always `fold(events)`, which is
why an approval arriving minutes later resumes the same turn exactly where it
stopped. See [Engine & execution](../concepts/engine-execution.md).

## 6. Keep the conversation going

Because the task is resting on the next-goal handle, another turn is one call:

```python
        turn = client.send_goal(turn.task_id, goal="What did I just ask?")
        for item in client.messages(turn.task_id):
            print(item)
```

```
...                                    # the four items from the first turn
UserMessage(text='What did I just ask?')
AssistantMessage(text="You asked me to count the words in 'hello world from noeta'.")
```

There is no session object and no conversation handle — a multi-turn
conversation *is* one Task receiving user input repeatedly, and `task_id` is the
only identity you carry. `query` is the one-shot version of all of this, with the
client lifecycle folded in.

## 7. Make it durable

Storage defaults to in-memory, so the ledger dies with the process. Point
`HostConfig.storage_path` at a SQLite file and a *different* client — a later
process, a restarted container — folds the same conversation back:

```python
from noeta.sdk import Client, HostConfig

db = HostConfig(storage_path="./noeta.sqlite")

with Client(options, provider=provider, model="stub-model", host_config=db) as c:
    task_id = c.start(goal="How many words are in 'hello world'?").task_id

# ... later, a fresh process ...
with Client(options, provider=provider, model="stub-model", host_config=db) as c:
    for item in c.messages(task_id):
        print(item)
```

The second client was never told what happened; it folded the log and found out.
`storage_path` also accepts a `postgresql://` DSN, which is what a multi-host
deployment uses. That is [event sourcing](../concepts/event-sourcing.md) doing
the work.

## Next steps

- **Write more tools** — [Build custom tools](../how-to/build-custom-tools.md).
- **Connect a real model** — [Configure a provider](../how-to/configure-provider.md).
- **Delegate work** — [Spawn subagents](../how-to/spawn-subagents.md).
- **Look up the surface** — [SDK reference](../reference/sdk.md), or
  [Concepts](../concepts/index.md) for the model behind it.

The runnable `examples/sdk_minimal.py`, `examples/custom_tool.py`, and
`examples/permission_gate.py` extend this exact pattern.
