# Agent Presets

Noeta ships four official agents aligned with Claude Code's roster.
The agent is chosen **per task** in the `POST /tasks` body
(`{"goal": …, "agent": …}`), not at process launch. Custom agents go
through the flat `Options.agents` dict.

## The quartet

| Agent | Role | Tools | Capabilities |
| --- | --- | --- | --- |
| `main` | Default coding agent: full built-in tool surface + can spawn the three subagents + all capabilities. | Full built-in set (all fs + web + app + memory tools) | `todo_write`, `ask_user_question`, `delegation`, `skill_invocation`, `memory`, `mcp` |
| `general-purpose` | Self-contained coding worker: full read/write/edit/shell set, no delegation. | `apply_patch`, `edit`, `glob`, `grep`, `read`, `shell_kill`, `shell_poll`, `shell_run`, `web_search`, `webfetch`, `write` | `skill_invocation`, `mcp` |
| `explore` | Read-only scout: glob/grep/read + read-only shell, fans out to report facts, never edits. | `glob`, `grep`, `read`, `shell_kill`, `shell_poll`, `shell_run`, `webfetch` | `skill_invocation` |
| `plan` | Read-only architect: reads the code and returns a concrete ordered implementation plan, never writes. | `glob`, `grep`, `read`, `shell_kill`, `shell_poll`, `shell_run`, `webfetch` | `ask_user_question` |

## Capability flags

| Flag | What it enables |
| --- | --- |
| `todo_write` | The `todo_write` control tool (state-patch based progress tracking). |
| `ask_user_question` | The model can yield for human input via `ask_user_question` control tool. |
| `delegation` | Can spawn subtasks via the three subagents (`general-purpose`, `explore`, `plan`). |
| `skill_invocation` | The `skill` control tool for model-driven skill selection. |
| `memory` | Cross-task memory: the `memory_write` / `memory_read` / `memory_search` / `memory_archive` tools + auto-recall at the user-message seam. Only `main` enables this; its system prompt carries the memory-policy fragment (exported as `MEMORY_POLICY_PROMPT`) telling the model what to save, what not to, and the write hygiene. |
| `mcp` | MCP tool inheritance: subtasks whose own spec also opens `mcp` inherit the parent's enabled MCP servers. |

## Sub-agent fan-out

`main` can spawn the three subagents in parallel; the result is the
subagent's return value, recorded into the EventLog so the whole tree
folds back into state. See
[ADR: Subtask fan-out and durable wake](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-fanout-and-durable-wake.md)
and [ADR: Subtask parallel execution](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-parallel-execution.md).

## Using presets programmatically

```python
from noeta import presets
from noeta.sdk import Client, query

# Build the main agent's Options
options = presets.main_options()

# Run an agent in-process
result = query(options, goal="Refactor module X to use Y")
```

Or compile all four agents as specs:

```python
from noeta.presets import official_specs
specs = official_specs()
# → {"main": AgentSpec, "general-purpose": AgentSpec, "explore": AgentSpec, "plan": AgentSpec}
```

## Custom agents

Define custom agents via the flat `Options.agents` dict:

```python
from noeta.sdk import Options, AgentDefinition

options = Options(
    system_prompt="You are a docs writer.",
    agents={
        "reviewer": AgentDefinition(
            description="Reviews docs for accuracy and clarity.",
            prompt="...",
            tools=["read", "grep", "glob"],
        ),
    },
)
```

## Source

- Presets: `packages/noeta-runtime/noeta/presets/__init__.py`
- Options / AgentDefinition: `packages/noeta-sdk/noeta/client/options.py`
- Tool catalog: `packages/noeta-runtime/noeta/tools/`
- See also: [ADR: Tool and agent catalog](https://github.com/initxy/noeta/blob/main/docs/adr/tool-and-agent-catalog.md), [ADR: Library-SDK architecture](https://github.com/initxy/noeta/blob/main/docs/adr/library-sdk-architecture.md)
