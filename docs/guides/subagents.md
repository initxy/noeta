# Delegate to subagents

Declare child agents in `Options.agents` and the parent gets a `Task` tool to
hand work to them — one at a time, several in parallel, or in the background.
Each child is its own durable task with its own event log.

## Define a child agent

```python
from noeta.sdk import AgentDefinition, Client, Options
from noeta.sdk.providers import AnthropicProvider

researcher = AgentDefinition(
    description="Read-only researcher that finds and reports facts.",
    prompt="You are a researcher. Read files and report what you find. Do not edit anything.",
    tools=("Read", "Glob", "Grep", "Bash"),
)

options = Options(
    system_prompt="You are a lead engineer. Delegate research to the researcher subagent.",
    name="lead",
    agents={"researcher": researcher},
)

client = Client(options, provider=AnthropicProvider(), model="claude-sonnet-5", workspace_dir=".")
outcome = client.start(goal="Review the auth and billing modules.")
```

Filling `agents` is the whole opt-in: the parent gets the `Task` tool with
`researcher` as an allowed `subagent_type`.

| `AgentDefinition` field | Default | Meaning |
| --- | --- | --- |
| `description` | required | shown to the parent model in the agent roster |
| `prompt` | required | the child's system prompt |
| `tools` | `None` = every built-in tool | built-in names or `@tool` functions |
| `model` | `None` = host default | model for this child |
| `plugins` | `()` | plugins this child activates (e.g. `"memory"`, `"mcp"`) |

A child has no `agents` field of its own. For a deeper tree, declare every agent
at the top level.

## How the parent calls it

The model calls `Task` with `{description, prompt, subagent_type}` — one child
per call:

| The model emits | What happens |
| --- | --- |
| one `Task` call | the parent waits for that child |
| several `Task` calls in one response | the children run in parallel; the parent waits for all |
| one `Task` call with `background=true` | the parent keeps going; the result arrives later as a notice |

While waiting, the parent is suspended, not blocked: a two-child fan-out records
`SubtaskSpawned` ×2, `TaskSuspended`, `SubtaskCompleted` ×2, `TaskWoken`. If the
worker dies in between, another one resumes the parent when the children finish.

A response that mixes `Task` with other tool calls is refused and the model is
told to retry — the task keeps running.

## Tune parallelism

| Knob | Default | Effect |
| --- | --- | --- |
| `NOETA_MAX_SUBTASK_CONCURRENCY` (env) | `min(8, CPU count)` | max children running at once in one fan-out |
| `NOETA_SUBTASK_CONCURRENCY` (env) | on | `0` / `false` / `off` / `no` runs a fan-out one child at a time |
| `HostConfig.max_background_subagents_per_root_task` | `8` | background children per root task; over the cap the call is rejected |

## Read a child's history

```python
envelopes = client.events(outcome.task_id)
child_ids = [e.payload.subtask_id for e in envelopes if e.type == "SubtaskSpawned"]

for item in client.messages(child_ids[0]):
    print(item)
```

```
UserMessage(text='Review the auth module ...')
AssistantMessage(text='...')
Result(answer='...', status='completed')
```

A child's stream reads like any other task's. The parent sees only each child's
final result.

## Test it offline

Script the parent's and the children's turns in one fake provider — see
[Testing](testing.md). `examples/spawn_subtask.py` is a runnable single-spawn
example.

## Next

- [Tasks and waking](../how-it-works/tasks-and-waking.md) — how a finished child wakes its parent
- [Per-tenant memory](multi-tenant-memory.md) — children resolve memory with their own task ids
- [ADR: background subagents](https://github.com/initxy/noeta/blob/main/docs/adr/background-subagent.md)
