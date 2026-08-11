# Options and host wiring

`Options` is the recipe for an agent: what it is told, what tools it may call,
what it may spend, who it may delegate to. `HostConfig` is the other half —
everything the *deployment* supplies, such as durable storage or a sandbox
container. Keeping them apart is deliberate: two hosts running the same
`Options` compile byte-identical agent identity, whatever their wiring.

Source: `packages/noeta-sdk/noeta/client/options.py` and
`client/host_config.py`.

## Identity vs. wiring

`Options` fields fall into two buckets, and the split decides two things at
once: what enters the recorded `AgentSpec`, and what counts when two `Options`
are compared for equality.

- **Identity** fields are compiled into the `AgentSpec` and are part of what the
  event log says this agent *was*. Change one and you have a different agent.
- **Wiring** fields are mount points. `compile_options` ignores them and
  `Options.__eq__` excludes them, so swapping a provider or a working directory
  never rewrites identity.

### Identity fields

| Field | Type / default | Notes |
| --- | --- | --- |
| `system_prompt` | `str \| SystemPromptPreset` — **required** | a verbatim string, or a named preset resolved at compile time |
| `name` | `str = "main"` | a name that collides with an `agents` key raises `ValueError` |
| `agents` | `Mapping[str, AgentDefinition] = {}` | a **flat** dict, never nested |
| `allowed_tools` | `tuple[str \| ToolLike, ...] \| None = None` | a **replacement** allowlist: a tuple means *only* those tools. `None` = all 10 built-ins; `()` = no tools |
| `disallowed_tools` | `tuple[str, ...] = ()` | subtracted from whichever base list applies; absent names are ignored |
| `permission_mode` | `"default"` \| `"acceptEdits"` \| `"bypassPermissions"` | validated at compile time |
| `max_turns` | `int \| None` | sugar for `budget.max_iterations`; setting both raises `ValueError` |
| `skills` | `tuple[str, ...] = ()` | declaratively activated skills |
| `plugins` | `tuple[str, ...] = DEFAULT_PLUGINS` | per-agent activation — see [below](#plugin-activation) |
| `budget` | `BudgetSpec \| None` | `None` ⇒ a default with `max_subtask_depth=3`, the runaway-recursion guard |
| `policy` | callable `(llm) -> Policy` carrying a `.ref` | `None` ⇒ the built-in ReAct policy |
| `mcp_servers` | `tuple[SdkMcpServer, ...] = ()` | in-process servers; their tools enter identity |

### Wiring fields

| Field | Type / default | Notes |
| --- | --- | --- |
| `provider` | `LLMProvider \| None` | the LLM adapter; a `Client(provider=…)` kwarg takes precedence |
| `cwd` | `str \| Path \| None` | working-directory hint |
| `model` | `str \| None` | routing hint; excluded from identity and from equality |
| `metadata` | `Mapping[str, str] = {}` | observational labels; excluded from identity |
| `can_use_tool` | `(tool_name, arguments) -> bool` | programmatic approval; its ruling is recorded as an ordinary approval event with `resolver="can_use_tool"` |
| `output_schema` | `Mapping \| None` | JSON Schema for the final answer; instructs the model natively and deserializes `FinishDecision.answer`. It does **not** mount the `structured_output` control tool — that one is gated on the per-helper schema a subtask / workflow helper is spawned with |
| `thinking` | `"adaptive"` \| `"disabled"` \| `None` | invalid values raise `ValueError` at construction |
| `effort` | `"low"` \| `"medium"` \| `"high"` \| `"xhigh"` \| `"max"` \| `None` | same |
| `guards` | `tuple[Guard, ...] = ()` | pre-act interception |
| `observers` | `tuple[Observer, ...] = ()` | post-commit event subscribers |
| `content_channels` | `tuple[ContentKindSpec, ...] = ()` | the only composer seam open to a host |

## Permission modes

`permission_mode` picks how a high-risk tool call is approved.

| Mode | Which tools require approval |
| --- | --- |
| `"default"` | every tool whose declared `risk_level` is not `low` |
| `"acceptEdits"` | the same rule, minus the three edit-class tools `Edit` / `Write` |
| `"bypassPermissions"` | none — for trusted, non-interactive runs |

The mode only chooses the gated set. A `Guard` may still deny, and
`Options.can_use_tool` still resolves whatever the gate stops.

Read the legal values at runtime rather than hard-coding them:

```python
from noeta.sdk import effort_modes, model_capabilities, permission_modes

print(permission_modes())
# → ('default', 'acceptEdits', 'bypassPermissions')   # widening trust
print(effort_modes())
# → ('low', 'medium', 'high', 'xhigh', 'max')          # increasing intensity
print(model_capabilities(["claude-sonnet-4-6", "gpt-4o-mini"]))
# → {'claude-sonnet-4-6': {'supports_vision': True},
#    'gpt-4o-mini': {'supports_vision': False}}
```

Both mode tuples come back in the order a picker should show them, not sorted.
`model_capabilities` returns exactly one key per model, `supports_vision` — the
same name the provider's own vision guard uses — and an uncatalogued selector
reports `True`: the adapter admits its images and defers to the provider, so
the gate must not block what the request would accept.

## Plugin activation

`Options.plugins` names the loaded plugins *this agent* uses. Activation enters
identity: every recognised name folds into the `AgentSpec.plugins` tuple, and
capability gating is a membership test on that tuple.

`DEFAULT_PLUGINS` is `("fs", "web")`. Both are **identity-inert** in the sense
that activating them turns on no capability flag and changes no tool set — the
default 10 tools are read from the `fs` and `web` manifests either way. They do
still appear in the compiled `AgentSpec.plugins` tuple, so dropping them is a
real identity change:

```python
compile_options(Options(system_prompt="x"))[0].plugins             # → ('fs', 'web')
compile_options(Options(system_prompt="x", plugins=()))[0].plugins # → ()
```

A name must be one of three things, or compilation fails loudly:

- a **built-in feature bundle** that carries identity — `memory`, `browser`,
  `mcp`, `todo_write`, `ask_user_question`, `skill_invocation`, `delegation`;
- an **identity-inert built-in** name, recognised so a typo still fails —
  `app`, `fs`, `governance`, `presets`, `providers`, `react`, `reminders`,
  `sandbox`, `skills`, `storage`, `web`, `workspace`;
- the **name of a plugin** in the `PluginSet` handed to `Client`.

```python
from noeta.sdk import DEFAULT_PLUGINS, Options

options = Options(
    system_prompt="You are a coding agent.",
    plugins=DEFAULT_PLUGINS + ("memory", "todo_write"),
)
# a typo fails the build, naming both the bad name and where it appeared:
#   ValueError: unknown plugin activation 'memry' on Options.plugins — not a
#   built-in activation (app, ask_user_question, …) and not in the loaded
#   plugin set (<none loaded>). Load it before activating, or fix the name.
```

`delegation` is the one activation that overlaps a structural capability: it is
derived automatically for an agent with an `agents` roster, and naming it
explicitly only ever turns it **on** — which is how a flat child agent is
granted the right to spawn. Full contract in [Plugins](plugins.md).

## `AgentDefinition`

The flat child-agent recipe. Children are leaves — `AgentDefinition` cannot
nest, so deep trees are declared flat at the top level and wired through the
compiled `AgentSpec.spawnable`.

| Field | Notes |
| --- | --- |
| `description` | **required, non-blank** — it is rendered into the `Task` schema so the model knows who to hand work to |
| `prompt` | required |
| `tools` | `None` ⇒ all built-ins |
| `model` | routing hint |
| `plugins` | per-agent activation, default `()` — no `fs`/`web`; `("delegation",)` grants the right to spawn |
| `metadata` | observational labels |

## `SystemPromptPreset`

`preset: str = "main"`, `append: str | None = None`. Resolves a registered
preset prompt at compile time, optionally appending a suffix.
`register_preset_prompt(name, prompt)` adds one (last writer wins). The official
presets `main` and `main-web` are registered for you — see
[Presets](presets.md).

## `compile_options` and `BudgetSpec`

```python
compile_options(options, *, plugins=None, preset_prompts=None)
    -> (AgentSpec, tuple[AgentSpec, ...])
```

A pure compile of the recipe into `(main_spec, descendant_specs)` — referentially
transparent, so equal `Options` produce equal `AgentSpec`s. `plugins` is a
`Mapping[str, PluginActivation]`; `Client` builds it from the `PluginSet`.

`BudgetSpec` (`noeta/agent/spec.py`) carries the caps on `Options.budget`:
`max_iterations`, `max_tool_calls`, `max_cost_usd`, `max_spawned_subtasks`,
`max_subtask_depth`. `None` on a field means no cap on that dimension.

## `HostConfig`

A frozen dataclass passed as `Client(..., host_config=…)`. It is **never** part
of agent identity, so two clients differing only here compile byte-identical
specs. Every field defaults to "absent", so a bare `HostConfig()` reproduces the
in-memory, no-preview, no-MCP behaviour.

**Storage.** `storage_triple()` returns the resolved triple or `None`.

| Field | Default | Purpose |
| --- | --- | --- |
| `storage_path` | `None` | one string — a sqlite file path, a `postgresql://` DSN, or `":memory:"` — resolved through `noeta.sdk.storage.open_storage_stack` |
| `event_log` / `content_store` / `dispatcher` | `None` | the explicit triple, all-or-none |

Supplying both forms raises `ValueError`, as does a partial explicit triple. All
`None` means in-memory.

**Runtime injections.**

| Field | Default | Purpose |
| --- | --- | --- |
| `app_gateway` | `None` | `AppPreviewGateway`; `None` ⇒ no `open_app` tool |
| `write_roots` | `None` | `(task_id) -> Sequence[str]` extra write roots |
| `mcp_server_resolver` | `None` | `(alias) -> McpAnyServerSpec \| None`, resolved per turn |
| `mcp_http_post` | `None` | injectable HTTP transport (`HttpPostFn`) for remote MCP |
| `delta_sink` | `None` | `(StepContext, call_id, StreamDelta) -> None` — ephemeral token deltas; never persisted |
| `otlp_traces` / `otlp_http_post` | `None` | `OtlpTraceConfig` export config plus transport |
| `provider_headers` | `None` | `(StepContext) -> Mapping[str, str]` per-request headers |

**Sandbox and execution environment.**

| Field | Default | Purpose |
| --- | --- | --- |
| `exec_env` | `None` | `SandboxExecEnvConfig` — **attach** one shared container |
| `sandbox_provider` | `None` | `SandboxProvider` — provision a fresh container per session; takes precedence over `exec_env` |
| `sandbox_spec` | `None` | the deployment-fixed half of the per-session `SandboxSpec` |
| `sandbox_exec_preamble` | `None` | `(exec_env_ref, argv) -> prefix`, re-invoked per container command |
| `sandbox_backend_factory` / `sandbox_browser_factory` | `None` | swap the sandbox wire without touching the seam |
| `sandbox_policy` | `None` | `(root_task_id, workspace_dir) -> bool` per-session opt-out |

**Memory.** Precedence is `memory_root_resolver` > `memory_dir` >
`global_memory_dir` > `~/.noeta/memories`. See
[Multi-tenant memory](../how-to/multi-tenant-memory.md).

| Field | Default | Purpose |
| --- | --- | --- |
| `memory_dir` / `global_memory_dir` | `None` | host-level store roots |
| `memory_root_resolver` | `None` | `(task_id) -> Path \| None` per-task root |

**Plugin operator config.**

| Field | Default | Purpose |
| --- | --- | --- |
| `plugin_config` | `{}` | `plugin name -> {key: value}`, read by a session pack as `ctx.config("<name>")`. A third-party name passes through verbatim; for the four the SDK derives itself (`fs` / `skills` / `workspace` / `memory`) the host's keys are overlaid **per key**. See [Write a plugin](../how-to/write-a-plugin.md) |

**Kill-switches and policy.**

| Field | Default | Purpose |
| --- | --- | --- |
| `workflow_allowed` | `False` | expose `run_workflow` (also requires delegation) |
| `max_background_jobs_per_root_task` | `8` | over the cap a background `Bash` is rejected, not queued |
| `max_background_subagents_per_root_task` | `8` | the same for `Task(background=True)` |
| `instructions_enabled` | `False` | load the workspace-root `NOETA.md`, else `AGENTS.md`, else `CLAUDE.md` |
| `instructions_file` | `None` | read only this path instead of searching |
| `instructions_discovery` | `False` | `Read`-triggered discovery of subdirectory instruction files |
| `write_mode` | `"dry_run"` | `"apply"` performs real writes |
| `extra_models` | `{}` | operator `ModelSpec` rows joining the shipped catalog (internal gateway names, self-hosted models); collisions with shipped rows fail the build. Register the same rows every run — the catalog feeds compaction derivation |

## Sandbox and storage wiring types

| Symbol | Role |
| --- | --- |
| `SandboxProvider` | the `allocate` / `release` / `attach` Protocol a product implements |
| `SandboxSpec` / `MountSpec` | the `allocate` input: image, mounts, resources, env; a mount's `kind` is `local-path` / `nas` / `volume` / `pvc` |
| `SandboxHandle` | a live binding: `base_url`, `sandbox_id`, `auth`, `workdir` |
| `SandboxAuth` / `StaticApiKeyAuth` | the `connect_headers()` Protocol and its env-var implementation; never serialized |
| `encode_exec_env_ref` / `decode_exec_env_ref` | the flat durable `exec_env_ref` codec |
| `SandboxExecEnvConfig` | attach-mode config: `base_url`, `api_key_env`, `workdir` |
| `ExecEnv` / `BrowserBackend` | the container-execution and browser-wire Protocols |
| `BackendFactory` / `BrowserBackendFactory` / `BoundPreamble` | the callable aliases the two `HostConfig` factory fields are written against |
| `McpServerSpec` / `McpHttpServerSpec` / `McpAnyServerSpec` / `McpError` / `McpConfigError` / `HttpPostFn` | the MCP vocabulary a resolver returns |
| `path_within(resolved, root) -> bool` | the containment predicate the write fence uses — component-wise, never string-prefix, so `/srv/app-old` is not inside `/srv/app` |

`noeta.sdk.storage` is the durable-backend doorway. `open_storage_stack(path)`
builds the whole `(event_log, content_store, dispatcher)` triple from one
string; `build_storage_stack`, `is_memory_path` and `is_postgres_url` are the
finer-grained entries, and the sqlite and postgres adapters (plus their
read-only variants and schema-version errors) are exported from the same module.

## Next

- [query / Client](sdk-client.md) — the verbs that run this recipe
- [Plugins](plugins.md) — what an activation name can refer to
- [Presets](presets.md) — the four official `Options` recipes
- [Built-in tools](tools.md) — what `allowed_tools=None` actually selects
