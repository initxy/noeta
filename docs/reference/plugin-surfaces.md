# Plugin surfaces

A surface is one named extension point, and a contribution names exactly one of
them. There are sixteen standard surfaces. This page is the catalogue: what each
one takes, how contributions to it collide and order, and which built-in plugin
demonstrates it.

The loader is **surface-agnostic** — it consults one `SurfaceRegistry` and
nothing else — so adding a surface means registering one `SurfaceSpec`, never
editing the loader. Source:
`packages/noeta-sdk/noeta/client/surfaces.py` (`STANDARD_SURFACES`).

## How to read a section

Each section opens with `plane · scope · collision key · ordering`. The
**collision key** is the namespace two contributions clash in — `single-valued`
means at most one across the whole loaded set, `none` means the surface never
collides. **Ordering** `sorted` is `(plugin, name)`, so discovery order never
changes the result; `priority` reads an integer `priority` param first, with
ties broken by `(plugin, name)`.

## Identity plane

These enter `AgentSpec` identity and reach an agent only where
`Options.plugins` activates the contributing plugin.

### `tool`

identity · per-agent · collision `name` · sorted. A built-in tool name, or an
object exposing `.ref` — a `@tool`-decorated function or a Tool class. Built-in
corpus: `fs` declares nine (`read`, `glob`, `grep`, `edit`, `write`,
`apply_patch`, `shell_run`, `shell_poll`, `shell_kill`), `web` two, `memory`
four.

```toml
[[tool.noeta.contributions]]
surface = "tool"
ref     = "house_style.tools:LintTool"
```

### `agent`

identity · per-agent · collision `name` · sorted. A child agent the activating
agent may spawn; the `ref` must resolve to an `AgentDefinition`. Built-in
corpus: `presets` contributes the `web` browsing specialist and the internal
`__consolidation__` memory curator.

```toml
[[tool.noeta.contributions]]
surface = "agent"
ref     = "house_style.agents:REVIEWER"
```

### `content_kind`

identity · per-agent · collision `kind` · sorted. A resident content kind for the
semi-stable segment; the `ref` must resolve to a `ContentKindSpec`, and
registration order *is* the layout order. No built-in declares one here — the
four built-in kinds (`skill`, `memory`, `instructions`, `environment`) arrive
through their session packs instead.

```toml
[[tool.noeta.contributions]]
surface = "content_kind"
ref     = "house_style.content:RUNBOOK_KIND"
```

### `prompt_fragment`

identity · per-agent · collision `name` · sorted. A literal string appended after
the system prompt — declare it inline with `text`, or point `ref` at a
module-level string. Built-in corpus: `memory` contributes `memory-policy`, the
fragment telling the model what to save and what not to.

```toml
[[tool.noeta.contributions]]
surface = "prompt_fragment"
name    = "house-style"
text    = "Answer in at most three sentences."
```

### `policy`

identity · per-agent · collision **single-valued** · sorted. The decision brain:
an `(llm) -> Policy` factory carrying a `.ref` whose identity every compiled
`AgentSpec` pins. At most one across the loaded set — a base `Options.policy`
plus an active plugin, or two plugins, is an error. The default is
`("react", "1")` from the `react` built-in: replaceable here, never removable.

```toml
[[tool.noeta.contributions]]
surface = "policy"
ref     = "house_style.policy:build_fsm_policy"
```

### `control_tool`

identity · per-agent · collision `name` · **priority**. A model-facing schema
that translates into an engine decision instead of a `Tool.invoke`. The `ref` is
a `(ControlToolBuildContext) -> ControlToolMount | None` factory that
**self-gates**, returning `None` when it does not apply — mounting *is*
enablement. Built-in corpus, in schema render order (locked by byte-equality
goldens, because that order feeds the stable-prefix hash): `spawn_subagent`
(100, `delegation`), `todo_write` (200), `ask_user_question` (300),
`run_workflow` (500) and `structured_output` (600, both `react`).

```toml
[[tool.noeta.contributions]]
surface  = "control_tool"
ref      = "house_style.control:build_escalate_control_tool"
priority = 700
```

## Wiring plane

Behaviour, not identity. `guard` and `observer` are the only **process-wide**
channels; a process-scoped surface beyond those is refused rather than quietly
filed under one of them.

### `guard`

wiring · **process** · collision `none` · sorted. A synchronous pre-act check at
`before_tool_call`, `before_spawn_subtask` or `before_finish`, returning
`allow` / `deny` / `require_approval`. Loaded means in force for every agent in
the process — an agent author must not opt out of interception by omitting an
activation. Built-in corpus: `governance` contributes `permission`, `budget`,
`repetition` and `hook`.

```toml
[[tool.noeta.contributions]]
surface = "guard"
ref     = "house_style.guards:NoProdWritesGuard"
```

### `observer`

wiring · **process** · collision `none` · sorted. A post-commit
`Callable[[EventEnvelope], None]` subscribed to the EventLog. Its failure cannot
affect the task, and it may not mutate anything. Built-in corpus: `governance`
contributes `hook`, the user-facing post-tool and notification observer.

```toml
[[tool.noeta.contributions]]
surface = "observer"
ref     = "house_style.observers:ship_to_siem"
```

### `provider`

wiring · host-wired · collision **single-valued** · sorted. An `LLMProvider`
adapter; at most one across the loaded set, and it collides with
`Options.provider`. The official adapters are not declared here — they live in
the `providers` built-in, reached through `noeta.sdk.providers`.

```toml
[[tool.noeta.contributions]]
surface = "provider"
ref     = "house_style.provider:GatewayProvider"
```

### `reminder_provider`

wiring · per-agent · collision `name` · sorted. Track A: a provider at a named
intake seam (`turn_intake`, `task_seed`) that reads a narrow `RecallView` and
returns zero or more `Reminder`s. It may query an external system because its
output is **recorded** — resume folds the reminder back from the ledger and never
re-invokes the provider. A raise fails the turn loudly. Built-in corpus: `memory`
contributes `memory-recall` on `turn_intake`.

```toml
[[tool.noeta.contributions]]
surface = "reminder_provider"
ref     = "house_style.recall:ticket_reminder_provider"
seams   = ["turn_intake"]
```

### `reminder`

wiring · per-agent · collision `name` · **priority**. Track B: a
`render(view) -> str | None` that is a **pure** function of a folded projection,
rendered at the tail of the dynamic suffix. Never recorded and re-derived on
every compose, so the stable prefix is untouched by construction. Built-in
corpus: `reminders` contributes `unfinished-todos` (100), `delegation-nudge`
(200) and `read-suggestion` (300).

```toml
[[tool.noeta.contributions]]
surface  = "reminder"
ref      = "house_style.reminders:stay_brief"
priority = 500
```

### `tool_result_transform`

wiring · per-agent · collision `name` · **priority**. A ToolRuntime stage that
rewrites a tool result **before** it is recorded — redaction, truncation,
annotation. No built-in declares one; it exists for hosts with their own data
rules.

```toml
[[tool.noeta.contributions]]
surface  = "tool_result_transform"
ref      = "house_style.transforms:redact"
priority = 100
```

### `session_pack`

wiring · per-agent · collision `name` · **priority**. The session-construction
half of a capability: a `(SessionBuildContext) -> PackContribution` factory the
kernel builder runs in one priority-ordered loop. A pack **self-gates** on its
context — backend absent, flag off, no config — and returns the empty
contribution when it does not apply, so the kernel holds no `if` for any
feature. Built-in bands (byte-golden-locked, since tool insertion order feeds
the stable-prefix hash): `fs` 100, `web` 200, `memory` 300, `instructions` 400,
`environment` 500 (both `workspace`), `skills` 600, `browser` 700, `app` 1000.

```toml
[[tool.noeta.contributions]]
surface  = "session_pack"
ref      = "house_style.pack:build_runbook_session_pack"
priority = 1100
```

## Host plane

The host selects and binds these. They are never per-agent, and never part of
`AgentSpec` identity.

### `mcp_server`

host · host-wired · collision `alias` · sorted. A connectable MCP server spec,
keyed by the alias its tools are prefixed with (`mcp__{alias}__{tool}`). No
built-in declares one — the `mcp` built-in is declaration-only.

```toml
[[tool.noeta.contributions]]
surface = "mcp_server"
name    = "tickets"
ref     = "house_style.mcp:TICKETS_SERVER"
```

### `skills`

host · host-wired · collision `none` · sorted. A resource-only surface: a `path`
to a directory of skill packs, merged into the skill catalogue. No `ref`, because
nothing is imported.

```toml
[[tool.noeta.contributions]]
surface = "skills"
path    = "house_style/skills"
```

### `sandbox_provider`

host · host-wired · collision `name` · sorted. The container-execution adapters a
deployment can bind. Built-in corpus: `sandbox` declares the two AIO Sandbox
adapters, `aio-exec-env` (`AioSandboxExecEnv`) and `aio-browser`
(`AioBrowserBackend`).

```toml
[[tool.noeta.contributions]]
surface = "sandbox_provider"
ref     = "house_style.sandbox:K8sSandboxProvider"
```

## Registering your own surface

`SurfaceSpec` fully describes one surface, and every enum field is validated at
construction — so a mistyped value, or a positional argument in the wrong slot,
raises `PluginError` at the registration line rather than at projection.

| Field | Values |
| --- | --- |
| `name` | the surface name a manifest writes |
| `plane` | `identity` / `wiring` / `host` |
| `activation_scope` | `per-agent` / `process` / `host-wired` |
| `validator` | runs on a **resolved** value; listing and merge never call it |
| `collision_key` | `name` / `kind` / `alias` / `single-valued` / `none` |
| `ordering` | `sorted` (default) / `priority` |
| `activation_binding` | identity plane only: `tool` / `agent` / `content_kind` / `prompt_fragment` / `policy` / `elsewhere`. **Required** there, **rejected** elsewhere |

`activation_binding` keeps the identity projection table-driven: a surface
declares which channel it feeds and reaches `compile_options` with no loader
edit. An identity surface with no binding would vanish silently between resolve
and compile, so the constructor refuses it.

Register on a **copy** — `standard_registry()` returns a fresh one every call —
before loading, and the same validation, collision and ordering pipeline runs
over your surface unchanged:

```python
reg = standard_registry()
reg.register(SurfaceSpec("http_route", "host", "host-wired", _valid_route, "name"))
plugins = load_plugins(registry=reg)          # the host's surface is live
```

`SurfaceRegistry` methods: `register(spec)` (a duplicate name raises),
`get(name)`, `names()`, `__contains__`, `copy()`.

## The built-in corpus

Noeta's eighteen built-ins are the reference manifests, one directory each at
`packages/noeta-sdk/noeta/builtins/<name>/__init__.py`: `app`,
`ask_user_question`, `browser`, `delegation`, `fs`, `governance`, `mcp`,
`memory`, `presets`, `providers`, `react`, `reminders`, `sandbox`, `skills`,
`storage`, `todo_write`, `web`, `workspace`. Each section above names the ones
that demonstrate it; `mcp`, `providers` and `storage` are declaration-only, with
zero contributions. Adding a first-party capability is adding a directory there.

## Next

- [Plugin manifest](plugin-manifest.md) — declaring and loading contributions
- [Write a plugin](../how-to/write-a-plugin.md) — the task-oriented guide
- [Extension planes](../architecture/extension-planes.md) — why the planes fall where they do
- [Glossary](glossary.md) — Surface, Activation, Session pack, Control tool mount
