# Spawn sub-agents

**Goal:** define child agents in `Options.agents`, enable delegation, and
let the parent agent fan out work to sub-agents in parallel.

**Before you start:** you are comfortable with the SDK from [Your first
agent](../tutorials/first-agent.md).

## Define child agents

Child agents are declared as `AgentDefinition` entries in
`Options.agents`. Each child is a flat recipe — its own prompt, tools,
and model. Children are leaves; they cannot nest further agents.

```python
from noeta.sdk import Options, AgentDefinition

researcher = AgentDefinition(
    description="Read-only researcher that finds and reports facts.",
    prompt="You are a researcher. Read files and report what you find. Do not edit anything.",
    tools=("read", "glob", "grep", "shell_run"),  # read-only subset
    model=None,  # inherits parent's model
)

options = Options(
    system_prompt="You are a lead engineer. Delegate research to the researcher sub-agent.",
    name="lead",
    agents={"researcher": researcher},
    capabilities={"delegation": True},  # opens the spawn_subagent surface
)
```

Setting `capabilities={"delegation": True}` on the parent tells the SDK
to expose the `spawn_subagent` control tool to the model. Without it,
the parent cannot spawn children even if `agents` is populated.

## How spawning works

When the parent model calls `spawn_subagent(agent="researcher",
goal="...")`, the runtime:

1. Creates a child Task with its own EventLog, configured from the
   `researcher` agent definition.
2. Runs the child to a terminal state (completed, failed, or cancelled).
3. Records the child's result into the parent's log as a
   `SubtaskCompleted` event.
4. Wakes the suspended parent with the child's result attached.

The child is an independent event-sourced task — it has its own trace,
its own tool calls, and its own LLM turns. The parent only sees the
final result.

## Fan out in parallel

A real LLM (not the scripted provider) can call `spawn_subagent`
multiple times in the same turn to fan out work:

```python
# The model might produce this in a single turn:
spawn_subagent(agent="researcher", goal="Analyze the auth module")
spawn_subagent(agent="researcher", goal="Analyze the billing module")
spawn_subagent(agent="researcher", goal="Analyze the API module")
```

All three children run concurrently. The parent suspends until all
spawned children from that turn have completed, then resumes with each
child's result available via the `SubtaskCompleted` wake events.

## Inspect the child's stream

After a run, you can inspect the child's event stream separately:

```python
from noeta.sdk import Client

client = Client(options, provider=my_provider, workspace_dir="./")
outcome = client.start(goal="Analyze the codebase and report findings.")

# The parent's messages
parent_msgs = client.messages(outcome.task_id)

# Find child task IDs from the envelope stream
envelopes = client.events(outcome.task_id)
# SubtaskStarted / SubtaskCompleted envelopes carry the child's task_id
```

Each child has its own trace in the web UI — look for the subtask links
in the parent's session view.

## Offline test with FakeLLMProvider

To test spawning without a real API key, script the parent's turns:

```python
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.protocols.messages import (
    LLMResponse, TextBlock, ToolUseBlock, Usage,
)
from noeta.policies.react import SPAWN_SUBAGENT_TOOL

provider = FakeLLMProvider(
    responses=[
        LLMResponse(
            stop_reason="tool_use",
            content=[ToolUseBlock(
                call_id="spawn-1",
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": "researcher", "goal": "find the bug"},
            )],
            usage=Usage(uncached=1, output=1),
        ),
        # After the child finishes, the parent resumes with this turn:
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="The researcher found the bug.")],
            usage=Usage(uncached=1, output=1),
        ),
    ]
)
```

The child agent also needs a provider. By default it inherits the
parent's; for the `FakeLLMProvider`, you need to give the child its own
scripted response sequence (set `model` on the `AgentDefinition` to a
named model that maps to a child-specific provider).

## See also

- [Task model](../concepts/task-model.md) — parent-child task relationships
- [Wake & resume](../concepts/wake-resume.md) — how `SubtaskCompleted`
  wakes the parent
- [SDK reference](../reference/sdk.md) — `AgentDefinition`, `Options.agents`
- `examples/spawn_subtask.py` — full runnable example
