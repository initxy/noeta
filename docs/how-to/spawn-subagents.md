# Spawn subagents

This guide shows you how to define child agents in `Options.agents` and let the
parent fan work out to them. You need the SDK basics from
[Your first agent](../tutorials/first-agent.md).

## 1. Define child agents

Child agents are declared as `AgentDefinition` entries in `Options.agents`.
Each child is a flat recipe — its own prompt, tools, and model. Children are
leaves: an `AgentDefinition` has no `agents` field and compiles with an empty
`spawnable`, so it cannot delegate further.

```python
from noeta.sdk import Options, AgentDefinition

researcher = AgentDefinition(
    description="Read-only researcher that finds and reports facts.",
    prompt="You are a researcher. Read files and report what you find. Do not edit anything.",
    tools=("Read", "Glob", "Grep", "Bash"),  # read-only subset
    model=None,  # inherits the host default
)

options = Options(
    system_prompt="You are a lead engineer. Delegate research to the researcher subagent.",
    name="lead",
    agents={"researcher": researcher},
)
```

That is the whole opt-in: populating `agents` makes `compile_options` fold
`"delegation"` into the parent's identity and union the child names into its
`spawnable`, which mounts the `Task` control tool with `researcher`
in its schema enum.

## 2. Understand what a spawn records

`Task` takes a required `spawns` array of `{agent, goal}` entries.
One entry is a single delegate-and-wait; several entries in the same call are a
concurrent fan-out — the batch array is the fan-out shape, not repeated tool
calls.

When the parent model calls it, the runtime:

1. Creates one child Task per entry, each configured from its named agent
   definition, and records a `SubtaskSpawned` event on the parent's stream.
2. Suspends the parent on a barrier (`TaskSuspended`).
3. Runs the children to a terminal state (completed, failed, or cancelled),
   recording a `SubtaskCompleted` on the parent's stream for each.
4. Wakes the parent (`TaskWoken`) with the children's results attached.

So a two-entry fan-out records `SubtaskSpawned`, `SubtaskSpawned`,
`TaskSuspended`, `SubtaskCompleted`, `SubtaskCompleted`, `TaskWoken`. Each
child is an independent event-sourced task with its own trace, tool calls, and
LLM turns; the parent sees only the final results.

Children in one batch run concurrently on a bounded in-process pool
(`min(8, CPU count)`). Two environment variables tune it:
`NOETA_MAX_SUBTASK_CONCURRENCY` overrides the cap outright, and setting
`NOETA_SUBTASK_CONCURRENCY` to `0`, `false`, `off`, or `no` forces a sequential
drain instead.

A call may also pass `background=true` with a single spawn: the parent is not
suspended, it gets a "started" receipt, and the child's result arrives later as
a notice — see [Background subagents](https://github.com/initxy/noeta/blob/main/docs/adr/background-subagent.md).

## 3. Inspect a child's stream

```python
from noeta.sdk import Client

client = Client(options, provider=my_provider, workspace_dir="./")
outcome = client.start(goal="Analyze the codebase and report findings.")

# Child task ids come off the envelope stream: SubtaskSpawned carries the
# child's id in payload.subtask_id; SubtaskCompleted carries its result.
envelopes = client.events(outcome.task_id)
child_ids = [e.payload.subtask_id for e in envelopes if e.type == "SubtaskSpawned"]
print(child_ids)

# A child's own stream reads like any other task's.
for item in client.messages(child_ids[0]):
    print(item)
```

```
['task-3f9c1a7e4b2d4c8f9a1e6b0d5c7a2e13']
UserMessage(text='analyze the auth module')
AssistantMessage(text='researcher: auth looks fine')
Result(answer='researcher: auth looks fine', status='completed')
```

## 4. Test it offline

There is one provider per host and a child is an ordinary Task on it, so a
scripted test needs no second provider — script the parent's and the children's
turns into one `responses` list, in the order they are consumed:

```python
from noeta.sdk import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.sdk.testing import FakeLLMProvider
from noeta.policies.control_semantics import SPAWN_SUBAGENT_TOOL


def _finish(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


provider = FakeLLMProvider(
    responses=[
        # 1. the parent fans out to two children in one call
        LLMResponse(
            stop_reason="tool_use",
            content=[ToolUseBlock(
                call_id="spawn-1",
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"spawns": [
                    {"agent": "researcher", "goal": "analyze the auth module"},
                    {"agent": "researcher", "goal": "analyze the billing module"},
                ]},
            )],
            usage=Usage(uncached=1, output=1),
        ),
        # 2. and 3. the two children's turns
        _finish("researcher: auth looks fine"),
        _finish("researcher: billing looks fine"),
        # 4. the parent resumes with both results
        _finish("Both modules reviewed."),
    ]
)
```

`SPAWN_SUBAGENT_TOOL` is the one deep import here: the control tool's wire name
is runtime vocabulary, not part of the recipe surface, so only a script that
fakes a model's tool call needs it.

See `examples/spawn_subtask.py` for a runnable single-spawn version.

## Next steps

- [Task model](../concepts/task-model.md) — parent-child task relationships
- [Wake & resume](../concepts/wake-resume.md) — how `SubtaskCompleted` wakes the
  parent
- [Extension planes](../architecture/extension-planes.md) — why `delegation` is
  an activation
- [SDK reference](../reference/sdk.md) — `AgentDefinition`, `Options.agents`
