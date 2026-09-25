# Plugin surfaces

A surface is one named extension point; each contribution targets exactly one. There are sixteen standard surfaces (`STANDARD_SURFACES` in `packages/noeta-sdk/noeta/client/surfaces.py`).

## At a glance

| Surface | Plane | Scope | Collision key | Ordering | Value |
| --- | --- | --- | --- | --- | --- |
| [`tool`](#tool) | identity | per-agent | `name` | sorted | built-in tool name, `@tool` function or Tool class |
| [`agent`](#agent) | identity | per-agent | `name` | sorted | `AgentDefinition` |
| [`content_kind`](#content-kind) | identity | per-agent | `kind` | sorted | `ContentKindSpec` |
| [`prompt_fragment`](#prompt-fragment) | identity | per-agent | `name` | sorted | string (`text` or `ref`) |
| [`policy`](#policy) | identity | per-agent | single-valued | sorted | `(llm) -> Policy` factory with `.ref` |
| [`control_tool`](#control-tool) | identity | per-agent | `name` | priority | `(ControlToolBuildContext) -> ControlToolMount \| None` |
| [`guard`](#guard) | wiring | process | none | sorted | pre-act check |
| [`observer`](#observer) | wiring | process | none | sorted | `Callable[[EventEnvelope], None]` |
| [`provider`](#provider) | wiring | host-wired | single-valued | sorted | `LLMProvider` |
| [`reminder_provider`](#reminder-provider) | wiring | per-agent | `name` | sorted | callable at an intake seam |
| [`reminder`](#reminder) | wiring | per-agent | `name` | priority | `render(view) -> str \| None` |
| [`tool_result_transform`](#tool-result-transform) | wiring | per-agent | `name` | priority | callable |
| [`session_pack`](#session-pack) | wiring | per-agent | `name` | priority | `(SessionBuildContext) -> PackContribution` |
| [`mcp_server`](#mcp-server) | host | host-wired | `alias` | sorted | `SdkMcpServer` |
| [`skills`](#skills) | host | host-wired | none | sorted | absolute directory `path` |
| [`sandbox_provider`](#sandbox-provider) | host | host-wired | `name` | sorted | sandbox adapter |

- **Plane.** *Identity* surfaces enter `AgentSpec` identity and reach an agent only when `Options.plugins` activates the plugin. *Wiring* surfaces change behaviour, not identity. *Host* surfaces are bound by the host, never per agent.
- **Collision key.** The namespace two contributions clash in. `single-valued` = at most one across the loaded set; `none` = never collides.
- **Ordering.** `sorted` = by `(plugin, name)`. `priority` = integer `priority` param first (default `0`), ties by `(plugin, name)`.

## Identity surfaces

### `tool`

A tool the activating agent gets. Built-ins: `fs` (eight tools), `web` (two), `memory` (four).

```toml
[[tool.noeta.contributions]]
surface = "tool"
ref     = "house_style.tools:LintTool"
```

### `agent`

A child agent the activating agent may spawn. Built-ins: `presets` contributes `web` and `__consolidation__`.

```toml
[[tool.noeta.contributions]]
surface = "agent"
ref     = "house_style.agents:REVIEWER"
```

### `content_kind`

A resident content kind in the semi-stable part of the prompt; registration order is layout order. No built-in uses it — the built-in kinds (`skill`, `memory`, `instructions`, `environment`) come from `session_pack` contributions.

```toml
[[tool.noeta.contributions]]
surface = "content_kind"
ref     = "house_style.content:RUNBOOK_KIND"
```

### `prompt_fragment`

A string appended after the system prompt. Built-in: `memory` contributes `memory-policy`.

```toml
[[tool.noeta.contributions]]
surface = "prompt_fragment"
name    = "house-style"
text    = "Answer in at most three sentences."
```

### `policy`

The decision loop. At most one across the loaded set — a base `Options.policy` plus a plugin, or two plugins, is an error. Default: `("react", "1")` from the `react` built-in.

```toml
[[tool.noeta.contributions]]
surface = "policy"
ref     = "house_style.policy:build_fsm_policy"
```

### `control_tool`

A model-facing schema that becomes an engine decision instead of a `Tool.invoke`. The factory returns `None` when it doesn't apply. Built-ins, by priority: `Task` (100, `delegation`), `TodoWrite` (200, `todo_write`), `AskUserQuestion` (300, `ask_user_question`), `run_workflow` (500), `RecallHistory` (550), `structured_output` (600) — the last three from `react`. The order is locked by golden tests because it feeds the stable-prefix cache.

```toml
[[tool.noeta.contributions]]
surface  = "control_tool"
ref      = "house_style.control:build_escalate_control_tool"
priority = 700
```

## Wiring surfaces

### `guard`

A synchronous check at `before_tool_call`, `before_spawn_subtask` or `before_finish`, returning `allow` / `deny` / `require_approval`. Process-wide: loading it puts it in force for every agent. Built-ins: `governance` contributes `permission`, `budget`, `repetition`, `hook`.

```toml
[[tool.noeta.contributions]]
surface = "guard"
ref     = "house_style.guards:NoProdWritesGuard"
```

### `observer`

A post-commit subscriber to the event log. It cannot affect the task or mutate anything. Built-in: `governance` contributes `hook`.

```toml
[[tool.noeta.contributions]]
surface = "observer"
ref     = "house_style.observers:ship_to_siem"
```

### `provider`

An `LLMProvider` adapter, at most one. A host-resolved listing, never auto-consumed: it is listed for auditing, and the host passes its chosen adapter as `Client(provider=...)` or `Options.provider`. Official adapters live in `noeta.sdk.providers`, not here.

```toml
[[tool.noeta.contributions]]
surface = "provider"
ref     = "house_style.provider:GatewayProvider"
```

### `reminder_provider`

Runs at a named intake seam (`turn_intake`, `task_seed`) with a `RecallView` (incoming message, folded task state, workspace path, `visible_history`). Returns `Reminder`s (recorded as follow-up turns) and/or `ResidentActivation`s (recorded as residents, activate-once by default). Its output is recorded, so it may call external systems; resume never re-runs it. A raise fails the turn. Built-in: `memory` contributes `memory-recall` and `memory-index-delta` on `turn_intake` — the second records one line naming the pages created, re-described or removed since the task's memory index snapshot, because the index resident itself is frozen per task.

```toml
[[tool.noeta.contributions]]
surface = "reminder_provider"
ref     = "house_style.recall:ticket_reminder_provider"
seams   = ["turn_intake"]
```

### `reminder`

A pure `render(view) -> str | None` over folded state, rendered at the tail of the prompt on every compose and never recorded. Built-ins: `reminders` contributes `unfinished-todos` (100) and `read-suggestion` (300); `react` contributes `collapsed-context` (350).

```toml
[[tool.noeta.contributions]]
surface  = "reminder"
ref      = "house_style.reminders:stay_brief"
priority = 500
```

### `tool_result_transform`

Rewrites a tool result before it is recorded (redaction, truncation, annotation). No built-in uses it.

```toml
[[tool.noeta.contributions]]
surface  = "tool_result_transform"
ref      = "house_style.transforms:redact"
priority = 100
```

### `session_pack`

Builds a capability's per-task parts (tools, content kinds, named exports). The factory returns an empty contribution when it doesn't apply. Built-in priorities (golden-locked): `fs` 100, `web` 200, `memory` 300, `instructions` 400, `environment` 500 (both `workspace`), `skills` 600, `browser` 700, `app` 1000.

```toml
[[tool.noeta.contributions]]
surface  = "session_pack"
ref      = "house_style.pack:build_runbook_session_pack"
priority = 1100
```

## Host surfaces

`mcp_server` and `skills` take effect as soon as the plugin is loaded. `provider` and `sandbox_provider` are listings the host wires by hand.

### `mcp_server`

An in-process MCP server built with `create_sdk_mcp_server`. `Client` folds every loaded one into `Options.mcp_servers`; its tools join the agent's tool set (and so its identity). The contribution name is the alias and shares a namespace with `Options.mcp_servers`; a clash is a `PluginError`. Remote servers are not declared here — resolve them per turn through `HostConfig.mcp_server_resolver`.

```toml
[[tool.noeta.contributions]]
surface = "mcp_server"
name    = "tickets"                          # the alias
ref     = "house_style.mcp:TICKETS_SERVER"   # an SdkMcpServer
```

### `skills`

A directory of skill packs; no `ref`. The path **must be absolute** (build it from `Path(__file__).parent`); a missing directory is an empty tier. Plugin dirs are the lowest tier but one:

```
built-in < plugin < extra_skill_dirs < ~/.agents/skills < ~/.noeta/skills < workspace .agents/skills < workspace .noeta/skills
```

| `plugin_config["skills"]` key | Meaning |
| --- | --- |
| `extra_skill_dirs` | extra directories, e.g. `~/.claude/skills` (opt-in) |
| `global_agents_skills_dir` | `~/.agents/skills` tier (opt-in) |
| `skills_dir` | override the workspace set (`.agents/skills` then doesn't mount) |
| `workspace_skills_trust` | `"trust-store"` gates both workspace tiers on the trust store |
| `menu_budget_tokens` | cap for the `skill` roster; default 1% of the model's context window, at most 4,096 tokens |
| `menu_rank` | `skill name → score`, the keep order when the roster is over budget |
| `allow_skill_scripts` | mount `run_skill_script` |

`global_skills_dir` is a host field. Only the workspace tiers mount by default. Over budget, the roster shortens every summary (first sentence, ≤ 24 tokens), then drops the lowest-ranked to name only; each summary is capped at 384 tokens. Without `menu_rank` or `HostConfig.skill_menu_rank_resolver`, skills rank by recorded usage.

```toml
[[tool.noeta.contributions]]
surface = "skills"
path    = "/opt/house-style/skills"   # absolute
```

### `sandbox_provider`

Container-execution adapters. A host-resolved listing, never auto-consumed: listed and collision-checked, and the host picks one (`pset.get("...").resolve(registry)`). Built-ins: `sandbox` declares `aio-exec-env` (`AioSandboxExecEnv`) and `aio-browser` (`AioBrowserBackend`).

```toml
[[tool.noeta.contributions]]
surface = "sandbox_provider"
ref     = "house_style.sandbox:K8sSandboxProvider"
```

## Register your own surface

```python
from noeta.sdk import SurfaceSpec, load_plugins, standard_registry

reg = standard_registry()                     # a fresh copy each call
reg.register(SurfaceSpec("http_route", "host", "host-wired", _valid_route, "name"))
plugins = load_plugins(registry=reg)
```

| `SurfaceSpec` field | Values |
| --- | --- |
| `name` | surface name used in manifests |
| `plane` | `identity` / `wiring` / `host` |
| `activation_scope` | `per-agent` / `process` / `host-wired` |
| `validator` | called on a resolved value; never during listing or merge |
| `collision_key` | `name` / `kind` / `alias` / `single-valued` / `none` |
| `ordering` | `sorted` (default) / `priority` |
| `activation_binding` | identity only, and required there: `tool` / `agent` / `content_kind` / `prompt_fragment` / `policy` / `elsewhere` |

Invalid values raise `PluginError` at construction. `SurfaceRegistry`: `register(spec)` (duplicate raises), `get(name)`, `names()`, `__contains__`, `copy()`.

## Built-in plugins

Eighteen, one directory each under `packages/noeta-sdk/noeta/builtins/`: `app`, `ask_user_question`, `browser`, `delegation`, `fs`, `governance`, `mcp`, `memory`, `presets`, `providers`, `react`, `reminders`, `sandbox`, `skills`, `storage`, `todo_write`, `web`, `workspace`. `mcp`, `providers` and `storage` declare no contributions.

## Next

- [Plugin manifest](plugin-manifest.md) — declaring and loading
- [Write a plugin](../guides/plugins.md) — the task guide
- [Plugin system](../how-it-works/plugin-system.md) — why the planes fall where they do
