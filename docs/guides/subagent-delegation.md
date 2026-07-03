# Sub-agent delegation

Declare child agents in `Options.agents` and open the parent's `delegation`
capability. The SDK exposes a model-visible `spawn_subagent` control
surface — when the parent model calls it, the runtime spawns a child Task
built from the named child's *own* config (its own prompt, tools, model),
runs it to terminal, folds its result back, and resumes the parent.

## Example: main + researcher

```python
import tempfile
from pathlib import Path

from noeta.client import AgentDefinition, Client, Options
from noeta.policies.react import SPAWN_SUBAGENT_TOOL
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.testing.fake_llm import FakeLLMProvider

# --- 1. Declare child agents -----------------------------------------------
#
# AgentDefinition is a flat, non-recursive recipe for a child agent.
# It has no agents/subagents field — deep trees must be expressed by
# declaring every agent at the top level.

main = Options(
    system_prompt="Delegate research to your sub-agent, then summarise.",
    name="main",
    agents={
        "researcher": AgentDefinition(
            description="Read-only researcher that returns a finding.",
            prompt="You are a researcher. Investigate and report back.",
            tools=["read", "glob", "grep"],
        ),
    },
    permission_mode="bypassPermissions",
)

# When Options.agents is non-empty, the compiler automatically opens
# delegation capability and adds the child names to spawnable.
# You can also set capabilities explicitly:
#
#   from noeta.agent.spec import Capabilities
#   options = Options(
#       ...,
#       capabilities=Capabilities(
#           delegation=True,
#           spawnable=("researcher",),
#       ),
#   )

# --- 2. Scripted provider (for demo) ---------------------------------------
#
# In a real deployment, the live model decides when to delegate.
# Here we script three turns:
#   Turn 1: parent calls spawn_subagent("researcher", "find the answer")
#   Turn 2: child finishes with "the answer is 42"
#   Turn 3: parent finishes with a summary

def spawn_call(agent: str, goal: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="spawn-1",
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": agent, "goal": goal},
            )
        ],
        usage=Usage(uncached=1, output=1),
    )

def finish(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )

provider = FakeLLMProvider(
    responses=[
        spawn_call("researcher", "find the answer"),
        finish("researcher: the answer is 42"),
        finish("Done — the researcher reported 42."),
    ]
)

# --- 3. Run with Client (multi-turn) ---------------------------------------
#
# We use Client (not query) because we need the parent's task id to
# inspect the spawned child's stream.

with tempfile.TemporaryDirectory(prefix="noeta-spawn-") as tmp:
    client = Client(
        main,
        provider=provider,
        workspace_dir=Path(tmp),
        model="stub-model",
        multi_turn=False,
    )
    try:
        outcome = client.start(goal="Find the answer and tell me.")
        parent_id = outcome.task_id

        # Inspect the parent's event stream for the spawn.
        parent_events = client.events(parent_id)
        spawned = [e for e in parent_events if e.type == "SubtaskSpawned"]
        child_id = spawned[0].payload.subtask_id if spawned else None

        print(f"parent task: {parent_id}")
        print(f"spawned child task: {child_id}")
        # → parent task: <uuid>
        # → spawned child task: <uuid>
    finally:
        client.shutdown()
```

## Using the official presets

Noeta ships four official agents aligned with Claude Code's roster. The
`main` agent can delegate to `general-purpose`, `explore`, and `plan`:

```python
from noeta import presets
from noeta.sdk import Client

# Build the main agent with all three sub-agents available.
options = presets.main_options()

client = Client(
    options,
    provider=my_provider,
    workspace_dir=Path("./my-project"),
    model="gpt-5.5",
)
```

See [Agent Presets](../reference/presets.md) for the full quartet.

## Custom agents with delegation

```python
from noeta.sdk import Options, AgentDefinition

options = Options(
    system_prompt="You are a team lead. Delegate to specialists.",
    name="lead",
    agents={
        "coder": AgentDefinition(
            description="Writes and edits code.",
            prompt="You are a senior engineer. Write clean, tested code.",
            tools=["read", "edit", "write", "apply_patch", "shell_run"],
        ),
        "reviewer": AgentDefinition(
            description="Reviews code for bugs and style.",
            prompt="You are a code reviewer. Flag issues clearly.",
            tools=["read", "grep", "glob"],
        ),
    },
    permission_mode="bypassPermissions",
)
```

## Key points

- **`AgentDefinition` is flat, non-recursive.** A child cannot declare its
  own children. Deep trees require every agent declared at the top level
  with `Capabilities.spawnable` wiring the delegation paths.
- **`description` is required.** It's shown to the parent model in the
  `spawn_subagent` tool schema so the model knows which agent to pick.
- **Child agents run in their own Task.** They get their own EventLog,
  their own View, and their own tool surface. The parent resumes only
  after the child reaches a terminal state.
- **Results fold back automatically.** The child's return value is
  recorded in the parent's EventLog as a `SubtaskCompleted` event, and
  the parent model sees it in the next View.

## Source

- `examples/spawn_subtask.py` — full runnable demo
- `Options.agents` / `AgentDefinition` — `packages/noeta-sdk/noeta/client/options.py`
- `SPAWN_SUBAGENT_TOOL` — `packages/noeta-runtime/noeta/policies/react.py`
- Official presets: `packages/noeta-runtime/noeta/presets/__init__.py`
- See also: [Agent Presets](../reference/presets.md),
  [ADR: Subtask fan-out and durable wake](../adr/subtask-fanout-and-durable-wake.md),
  [ADR: Subtask parallel execution](../adr/subtask-parallel-execution.md)
