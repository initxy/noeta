# Agent Presets

`noeta.presets` ships four official agents: a conversational root, `main`, and
the three subagents it delegates to.

These are an **SDK-level** surface: you pick one by building its `Options`
(`presets.main_options()`) and handing that to a `Client` / `query`. Custom
agents go through the flat `Options.agents` dict.

## The quartet

| Agent | Role | Tools | Activation |
| --- | --- | --- | --- |
| `main` | Default coding agent: full built-in tool surface, spawns the three subagents. | Full built-in set (`allowed_tools` unset), plus the memory tools its `memory` activation opens | `fs`, `web`, `todo_write`, `ask_user_question`, `skill_invocation`, `memory`, `mcp`; `delegation` is derived from its `agents` roster |
| `general-purpose` | Self-contained coding worker: full read/write/edit/shell set, no delegation. | `apply_patch`, `edit`, `glob`, `grep`, `read`, `shell_kill`, `shell_poll`, `shell_run`, `web_search`, `webfetch`, `write` | `skill_invocation`, `mcp` |
| `explore` | Read-only scout: glob/grep/read + read-only shell, fans out to report facts, never edits. | `glob`, `grep`, `read`, `shell_kill`, `shell_poll`, `shell_run`, `webfetch` | `skill_invocation` |
| `plan` | Read-only architect: reads the code and returns a concrete ordered implementation plan, never writes. | `glob`, `grep`, `read`, `shell_kill`, `shell_poll`, `shell_run`, `webfetch` | `ask_user_question` |

`explore` and `plan` list `shell_run`, but their prompts restrict it to
read-only commands; the approval gate on high-risk shell is the backstop.
`general-purpose` is a leaf worker — it never spawns further, which bounds
fan-out.

## Activation names

| Name | What it enables |
| --- | --- |
| `todo_write` | The `todo_write` control tool (state-patch based progress tracking). |
| `ask_user_question` | The model can yield for human input via the `ask_user_question` control tool. |
| `delegation` | The `spawn_subagent` control tool. Derived for any agent with an `agents` roster; naming it explicitly grants a child the right to spawn. |
| `skill_invocation` | The `skill` control tool for model-driven skill selection. |
| `memory` | Cross-task memory: the `memory_write` / `memory_read` / `memory_search` / `memory_archive` tools plus auto-recall at the user-message seam. |
| `mcp` | MCP tool inheritance: subtasks whose own spec also opens `mcp` inherit the parent's enabled MCP servers. |
| `browser` | The sandbox-backed `browser_*` tool pack. Only the `web` specialist opens it. |
| `fs` / `web` | `DEFAULT_PLUGINS` — the default tool packs. Identity-inert. |

Only `main` activates `memory`: recall hooks into the user-message ingest seam,
and only the top-level conversational agent receives user messages. Every
memory-enabled preset's prompt carries the memory-policy fragment (exported as
`MEMORY_POLICY_PROMPT`), which tells the model what to save, what not to, and
the write hygiene.

## Optional agents

Two more `AgentDefinition`s ship alongside the quartet. Neither is in
`OFFICIAL_SUBAGENTS`, so neither changes `main`'s spawnable roster unless a
product registers it.

| Definition | Registered by | Purpose |
| --- | --- | --- |
| `WEB_SUBAGENT` (`"web"`) | `sandbox_browser_options()` | The browsing specialist — the sole identity that activates `browser`. Registering it swaps `main`'s prompt to `MAIN_WEB_SYSTEM_PROMPT` in lockstep with the roster, so the prompt never names a subagent that is not spawnable. `main` itself stays browser-free and delegates every page interaction. |
| `CONSOLIDATION_AGENT` (`"__consolidation__"`) | `with_consolidation_agent(options)` | The background memory curator, driven as an ordinary root task from a host trigger. `tools=()` empties the whitelist so its whole surface is the capability-gated memory pack. Its `__`-reserved name keeps it out of any parent's spawnable union. |

## Subagent fan-out

`main` can spawn the three subagents in parallel; the result is the subagent's
return value, recorded into the EventLog so the whole tree folds back into
state. See
[ADR: Subtask fan-out and durable wake](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-fanout-and-durable-wake.md)
and [ADR: Subtask parallel execution](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-parallel-execution.md).

## Exported surface

| Name | Shape |
| --- | --- |
| `main_options()` | `Options` — the official `main` recipe |
| `sandbox_browser_options()` | `Options` — `main_options()` plus the `web` subagent and the web-aware prompt |
| `with_consolidation_agent(options)` | `Options` — `options` with `__consolidation__` registered |
| `official_specs()` | `dict[str, AgentSpec]` — the four agents, compiled |
| `OFFICIAL_SUBAGENTS` | `dict[str, AgentDefinition]` — `general-purpose` / `explore` / `plan` |
| `WEB_SUBAGENT` / `CONSOLIDATION_AGENT` | `AgentDefinition` |
| `CONSOLIDATION_AGENT_NAME` | `str` — `"__consolidation__"` |
| `MAIN_SYSTEM_PROMPT` / `MAIN_WEB_SYSTEM_PROMPT` / `MEMORY_POLICY_PROMPT` | `str` |

Prompt text lives in `noeta/presets/prompts/*.md` and is loaded byte-faithfully,
so editing a prompt is a docs-shaped diff. `main` and `main-web` are also
registered as named presets, so `SystemPromptPreset(preset="main")` resolves.

## Using presets programmatically

```python
from noeta import presets
from noeta.sdk import query
from noeta.sdk.providers import AnthropicProvider

options = presets.main_options()

# `provider` and `workspace_dir` are required — without them the Client
# raises ValueError before any turn.
result = query(
    options,
    goal="Refactor module X to use Y",
    provider=AnthropicProvider(api_key="sk-ant-…"),
    workspace_dir="./",
    model="claude-sonnet-4-5-20250929",
)
print(result.answer())
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

- Presets: `packages/noeta-sdk/noeta/presets/__init__.py`
- Prompts: `packages/noeta-sdk/noeta/presets/prompts/`
- Options / AgentDefinition: `packages/noeta-sdk/noeta/client/options.py`
- Tool catalog: `packages/noeta-sdk/noeta/builtins/`
- See also: [ADR: Tool and agent catalog](https://github.com/initxy/noeta/blob/main/docs/adr/tool-and-agent-catalog.md), [ADR: Library-SDK architecture](https://github.com/initxy/noeta/blob/main/docs/adr/library-sdk-architecture.md)
