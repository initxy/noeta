# Agent presets

`noeta.presets` ships a ready-made root agent, `main`, and the three subagents it delegates to. Most hosts start from `main_options()` and adjust.

```python
from noeta import presets
from noeta.sdk import query
from noeta.sdk.providers import AnthropicProvider

result = query(
    presets.main_options(),
    goal="Refactor module X to use Y",
    provider=AnthropicProvider(),      # reads ANTHROPIC_API_KEY
    model="claude-sonnet-5",
    workspace_dir="./",                # optional; defaults to Options.cwd, then the process cwd
)
print(result.answer())
```

## The four agents

| Agent | Role | Tools | Activations |
| --- | --- | --- | --- |
| `main` | Default root agent; delegates to the three below. | full built-in set (`allowed_tools` unset) plus the memory tools | `fs`, `web`, `todo_write`, `ask_user_question`, `skill_invocation`, `memory`, `mcp`; `delegation` derived from its roster |
| `general-purpose` | Self-contained worker: search, edit, run, return. Never delegates. | `Edit`, `Glob`, `Grep`, `Read`, `Bash`, `BashOutput`, `KillShell`, `WebSearch`, `WebFetch`, `Write` | `skill_invocation`, `mcp` |
| `explore` | Read-only scout that reports facts. | `Glob`, `Grep`, `Read`, `Bash`, `BashOutput`, `KillShell`, `WebSearch`, `WebFetch` | `skill_invocation` |
| `plan` | Read-only architect; returns an ordered implementation plan. | same as `explore` | `ask_user_question` |

`explore` and `plan` have `Bash` but their prompts restrict it to read-only commands; shell approval is the backstop. `WebSearch` mounts only where a search key is configured.

## Activation names

What `Options.plugins` / `AgentDefinition.plugins` accept for built-in features:

| Name | Enables |
| --- | --- |
| `todo_write` | the `TodoWrite` control tool |
| `ask_user_question` | the `AskUserQuestion` control tool |
| `delegation` | the `Task` control tool; derived when an agent has `agents`, name it to let a child spawn |
| `skill_invocation` | the `skill` control tool |
| `memory` | the `memory_*` tools plus recall on each user message |
| `mcp` | a subtask that also activates `mcp` inherits the parent's enabled MCP servers |
| `browser` | the sandbox `browser_*` tools |
| `fs`, `web` | `DEFAULT_PLUGINS`, the default tool packs (no identity effect) |

Only `main` activates `memory`, because recall runs on user messages and only the root agent receives them. Memory-enabled prompts include `MEMORY_POLICY_PROMPT`.

## Optional agents

Not in `OFFICIAL_SUBAGENTS`, so they don't change `main`'s roster unless registered.

| Definition | Register with | Purpose |
| --- | --- | --- |
| `WEB_SUBAGENT` (`"web"`) | `sandbox_browser_options()` | Browsing specialist, the only preset that activates `browser`. Also swaps `main`'s prompt to `MAIN_WEB_SYSTEM_PROMPT`. |
| `CONSOLIDATION_AGENT` (`"__consolidation__"`) | `with_consolidation_agent(options)` | Background memory curator run as a root task by a host trigger. `tools=()`, so it has only the memory tools. The `__` prefix keeps it out of every spawn roster. |

## Exports

| Name | Type |
| --- | --- |
| `main_options()` | `Options` — the `main` recipe |
| `sandbox_browser_options()` | `Options` — `main_options()` plus `web` and the web-aware prompt |
| `with_consolidation_agent(options)` | `Options` — `options` with `__consolidation__` registered |
| `official_specs()` | `dict[str, AgentSpec]` — the four agents, compiled |
| `OFFICIAL_SUBAGENTS` | `dict[str, AgentDefinition]` — `general-purpose`, `explore`, `plan` |
| `WEB_SUBAGENT`, `CONSOLIDATION_AGENT` | `AgentDefinition` |
| `CONSOLIDATION_AGENT_NAME` | `str` — `"__consolidation__"` |
| `MAIN_SYSTEM_PROMPT`, `MAIN_WEB_SYSTEM_PROMPT`, `MEMORY_POLICY_PROMPT` | `str` |

Prompts live in `noeta/presets/prompts/*.md`. `main` and `main-web` are registered as named presets, so `SystemPromptPreset(preset="main")` resolves.

```python
from noeta.presets import official_specs

specs = official_specs()
print(sorted(specs))                 # ['explore', 'general-purpose', 'main', 'plan']
print(specs["explore"].plugins)      # ('skill_invocation',)
```

## Tool results are data

Both `main` prompts end with one rule: content from a tool result (a file, command output, web page, MCP result, sub-agent report) is data, not instructions; if it tries to redirect the agent, the agent doesn't act on it and tells the user. The `WebFetch` digest and the compaction note follow the same rule. This is a cheap first layer — approval gates and the `WebFetch` host policy are what actually stop an injected instruction.

## Custom agents

Define your own through `Options.agents`:

```python
from noeta.sdk import Options, AgentDefinition

options = Options(
    system_prompt="You are a docs writer.",
    agents={
        "reviewer": AgentDefinition(
            description="Reviews docs for accuracy and clarity.",
            prompt="...",
            tools=["Read", "Grep", "Glob"],
        ),
    },
)
```

## Next

- [Delegate to subagents](../guides/subagents.md) — using the roster
- [Options](options.md) — every field a preset sets
- [Built-in tools](tools.md) — what each tool list contains
