# SDK reference (`noeta.sdk`)

`noeta.sdk` is the single public import surface of the SDK. Everything below
is re-exported from it — users never import `noeta.client` or runtime
internals directly. Source of truth: the `__all__` list in
`packages/noeta-sdk/noeta/sdk/__init__.py`.

```python
from noeta.sdk import query, Client, Options, tool
```

## Client verbs

### `query(options, goal, *, provider=None, workspace_dir=None, model=None, images=(), plugins=None) → QueryResult`

One-shot query: drives a single turn to a genuine terminal and returns the
full envelope stream with pre-folded projections
(`packages/noeta-sdk/noeta/client/client.py`). Creates a temporary
`Client(multi_turn=False)` and shuts it down before returning. Use `Client`
directly for multi-turn work. `plugins` is a loaded `PluginSet` (see [Plugin
mechanism](#plugin-mechanism)); its plugins take effect only where
`Options.plugins` activates them.

### `QueryResult` — `client/client.py`

A `list[EventEnvelope]` subclass (iteration/indexing behave like a list) plus:

| Member | Returns | Notes |
| --- | --- | --- |
| `.task_id` | `str` | the driven task |
| `.messages()` | `list[ViewItem]` | pre-folded human view; every `ContentRef` already dereferenced |
| `.answer()` | `Any` | the terminal answer; **raises `QueryFailedError`** on a failed or non-terminal task |

The projections are materialized against the temporary Client's ContentStore
before teardown — do not re-project raw envelopes with a fresh store.

### `Client` — `client/client.py`

```python
Client(options, *, provider=None, workspace_dir=None, model=None,
       multi_turn=True, host_config=None, allowed_models=None, plugins=None)
```

A provider must come from the `provider` kwarg or `Options.provider`, and a
workspace from `workspace_dir` or `Options.cwd` — otherwise `ValueError`.
Storage defaults to in-memory; pass a `HostConfig` to inject a durable triple.
`allowed_models` is the per-turn model-selector allowlist: `None` keeps the
driver's stub default, and an explicitly **empty** sequence authorizes no
selector at all (`model_selector=None` still binds the host default). `plugins`
is a loaded `PluginSet` (see [Plugin mechanism](#plugin-mechanism)): its
identity-plane contributions reach an agent only where `Options.plugins`
activates the plugin, while its governance guards/observers apply process-wide.
An activation name absent from the loaded set fails the build.

> Line numbers are deliberately omitted throughout this page — they drift on
> every edit. The module path plus the member name is the stable coordinate.

**Turn-driving verbs.** Each runs the whole turn on the calling thread and
returns a drive outcome. All of them drain through `Options.can_use_tool` when
one is configured, so a gated tool call is auto-resolved no matter which verb
resumed the session.

| Method | Signature (keyword-only after `task_id`) |
| --- | --- |
| `start` | `(*, goal, agent=None, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), workspace_dir=None, effort=None, activations=())` |
| `send_goal` | `(task_id, *, goal, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), effort=None, activations=())` |
| `approve` | `(task_id, *, call_id, reason=None, resolver="client")` |
| `deny` | `(task_id, *, call_id, reason=None, resolver="client")` |
| `answer` | `(task_id, *, question_id, answers, answered_by="client")` |
| `deliver_event` | `(task_id, *, event_kind, payload=None)` — wake a `wait_external` suspend; matching is exact on `event_kind`, an optional `payload` is recorded as an `origin="system"` message on the resumed turn |

`workspace_dir` at `start` is welded into the durable `TaskHostBound` once;
later turns fold-resolve it. `permission_mode` / `enabled_mcp` / `effort` /
`activations` are per-turn, non-durable host knobs. `activations` pins built-in
skills pre-loop — the channel a `/skill-name` slash command rides.

**Seed / drive split** (async transports). `seed_*` performs every durable,
validated step on the request thread — so a typed rejection (`ModelSelectorError`,
`NotResumableError`) still surfaces as a synchronous 4xx — and returns a seeded
turn you hand to `drive_seeded` on a background thread.

| Method | Signature |
| --- | --- |
| `seed_start` | same as `start` |
| `seed_send_goal` | same as `send_goal` |
| `seed_approve` / `seed_deny` | same as `approve` / `deny` |
| `seed_answer` | same as `answer` |
| `seed_deliver_event` | same as `deliver_event` |
| `drive_seeded` | `(seeded)` — run the seeded turn to its next boundary |

**Resident worker pool.** With workers running, the background-drive path
yields the seed's lease back to the ready queue instead of spawning a one-off
thread, giving true concurrency across sessions.

| Method | Signature |
| --- | --- |
| `start_workers` | `(num_workers=1, *, poll_interval=0.1, heartbeat_interval=30.0, stale_sweep_interval=10.0, timer_poll_interval=1.0, lease_seconds=600.0, shutdown_grace_s=10.0)`; raises `RuntimeError` if called twice |
| `stop_workers` | `(timeout=None)` → `bool` — `False` when a worker did not exit in time, and the pool stays tracked so a retry can finish the job |

**Lifecycle, inspection, storage.**

| Method | Signature |
| --- | --- |
| `cancel` | `(task_id, *, reason="cancelled", cascade=False)` |
| `close` | `(task_id, *, closed_by="user", reason=None)` |
| `reopen` | `(task_id, *, reopened_by="user", reason=None)` |
| `events` | `(task_id)` → `list[EventEnvelope]` |
| `messages` | `(task_id)` → `list[ViewItem]` |
| `events_after` | `(task_id, after_seq=None)` → the stream strictly past a cursor |
| `task_streams` | `()` → per-task `(task_id, last_seq)` summaries |
| `delete_task` | `(task_id)` → `{"ok", "reason"?, "task_id", "deleted": [...]}`; refuses with `reason="running"` / `"not_found"` |
| `get_content` | `(content_hash)` → `bytes \| None` |
| `put_content` | `(body, *, media_type)` → `ContentRef` |
| `memory_root` | `(task_id=None)` → `Path` — the store this task resolves to under the multi-tenant chain |
| `subscribe` | `(callback)` → unsubscribe callable; post-commit envelopes, all tasks |
| `add_sandbox_lifecycle_listener` | `(on_allocate, on_release)` — product wiring for container-tracked side effects; no-op without a sandbox |
| `shutdown` | `()` — idempotent: stops workers, tears down observers and the trace sink, releases the sandbox |

Properties: `registry` (the compiled `AgentRegistry`), `main_agent_name`, and
`workers_running`.

## The recipe: `Options`

### `Options` — `client/options.py`

Frozen dataclass compiled into `AgentSpec`s. Fields split into **identity**
(enter the recording) and **wiring** (mount-point only, ignored by
`compile_options`):

| Field | Type / default | Kind |
| --- | --- | --- |
| `system_prompt` | `str \| SystemPromptPreset` — required | identity |
| `name` | `str = "main"` | identity |
| `skills` | `tuple[str, ...] = ()` | identity |
| `budget` | `BudgetSpec \| None` — `None` ⇒ default with `max_subtask_depth=3` | identity |
| `plugins` | `tuple[str, ...] = DEFAULT_PLUGINS` (`("fs", "web")`) — per-agent plugin **activation**: built-in feature-bundle names (`memory` / `skill_invocation` / `browser` / `todo_write` / `ask_user_question` / `mcp` / `delegation`) and names of loaded plugins in the `PluginSet` handed to `Client` | identity |
| `agents` | `Mapping[str, AgentDefinition] = {}` — flat, non-recursive | identity |
| `allowed_tools` | `tuple \| None` — `None` ⇒ **all 11 built-ins**; entries are name strings or `DecoratedTool`s | identity |
| `disallowed_tools` | `tuple[str, ...] = ()` — subtracted from the allow-list | identity |
| `permission_mode` | `"default"` \| `"acceptEdits"` \| `"bypassPermissions"` | identity |
| `max_turns` | `int \| None` — sugar for `budget.max_iterations`; setting both raises `ValueError` | identity |
| `policy` | callable `(llm) → Policy` with a `.ref` — `None` ⇒ built-in ReAct | identity |
| `mcp_servers` | `tuple[SdkMcpServer, ...] = ()` — their tools enter identity | identity |
| `model` | `str \| None` — routing hint | excluded from identity |
| `metadata` | `Mapping[str, str] = {}` — observational labels | excluded from identity |
| `provider` | `LLMProvider \| None` | wiring |
| `cwd` | `str \| Path \| None` | wiring |
| `can_use_tool` | `(tool_name, arguments) → bool` — auto-resolve gated calls; recorded with `resolver="can_use_tool"` | wiring |
| `output_schema` | `Mapping \| None` — JSON Schema for the final answer | wiring |
| `thinking` | `"adaptive"` \| `"disabled"` \| `None` | wiring |
| `effort` | `"low"` \| `"medium"` \| `"high"` \| `"xhigh"` \| `"max"` \| `None` | wiring |
| `guards` | `tuple[Guard, ...] = ()` | wiring |
| `observers` | `tuple[Observer, ...] = ()` | wiring |
| `content_channels` | `tuple[ContentKindSpec, ...] = ()` — the only composer seam | wiring |

Invalid `thinking` / `effort` values raise `ValueError` at construction;
invalid `permission_mode` raises at compile time (`options.py`).

### `AgentDefinition` — `client/options.py`

Flat child-agent recipe: `description` (required, non-empty), `prompt`
(required), `tools` (`None` ⇒ all built-ins), `model`, `plugins`
(per-agent activation, default `()` — no `fs`/`web`; `plugins=("delegation",)`
is how a child is granted the right to spawn), `metadata`. Cannot nest —
children are leaves. There is no `capabilities` field: it was removed with the
rest of the `Capabilities` authoring surface.

### `SystemPromptPreset` — `client/options.py`

`preset: str = "main"`, `append: str | None = None` — resolves a registered
preset prompt, optionally appending a suffix.

### `compile_options(options) → (AgentSpec, tuple[AgentSpec, ...])` — `client/options.py`

Pure compile of the recipe into `(main_spec, descendant_specs)`.
Referentially transparent: equal `Options` produce equal `AgentSpec`s.

### `register_preset_prompt(name, prompt) → None` — `client/options.py`

Registers a named preset for `SystemPromptPreset` (last-writer-wins).

## Authoring

### `@tool` — `packages/noeta-runtime/noeta/tools/decorator.py`

```python
@tool(name="word_count", version="1", risk_level="low",
      input_schema={...}, description="...")
def word_count(arguments: dict, ctx: ToolContext) -> ToolResult: ...
```

Wraps `fn(arguments, ctx) → ToolResult` as a `DecoratedTool`
(`decorator.py`). `version` is **required** — omitting it raises
`TypeError` (version feeds the identity fingerprint). `risk_level` defaults
to `"low"`. `input_schema` is LLM-facing metadata (not validated at runtime);
`description` is the model's single source of tool semantics. Also callable
directly: `tool(fn, name=..., ...)`.

### `create_sdk_mcp_server(name, version="1.0.0", tools=()) → SdkMcpServer` — `sdk/authoring.py`

Bundles `@tool` functions into an in-process (`"sdk"` transport) MCP server
for `Options.mcp_servers`. Empty `name` raises `ValueError`; a non-
`DecoratedTool` entry raises `TypeError`. `SdkMcpServer`
(`sdk/authoring.py`) is frozen: `name`, `version`, `tools`.

## Message projection & wire

### `as_messages(envelopes, content_store) → list[ViewItem]` — `client/messages.py`

Pure projection of an envelope stream into the human-readable view. The
`content_store` must be the one **paired with** the stream. `ViewItem`
(`messages.py`) is the union of:

| Type | Fields | Source |
| --- | --- | --- |
| `AssistantMessage` | `text` | `messages.py` |
| `UserMessage` | `text` | `messages.py` |
| `ToolUse` | `call_id`, `tool_name`, `arguments` | `messages.py` |
| `ToolResultView` | `call_id`, `tool_name`, `success`, `output: str \| None` | `messages.py` |
| `Result` | `answer`, `status` — on `"failed"`, `answer` holds the failure reason | `messages.py` |

### `envelope_to_dict(env) → dict` — `client/wire.py`

Canonical JSON-ready dict form of an `EventEnvelope` (the wire shape the
SSE stream and the web frontend consume).

### Content blocks

`ImageBlock` (`noeta/protocols/messages.py`) — an image input block for
`start` / `send_goal` / `query(images=…)`. `ContentRef`
(`noeta/protocols/values.py`) — `hash + size + media_type` reference into
the ContentStore.

## Host-level wiring

### `HostConfig` — `client/host_config.py`

Frozen dataclass passed as `Client(..., host_config=…)`; never part of agent
identity, so two clients differing only here compile byte-identical
`AgentSpec`s. Every field defaults to "absent", so a bare `HostConfig()`
reproduces the in-memory, no-preview, no-MCP behaviour.

**Storage** — all-or-none: `storage_triple()` raises `ValueError` on a partial
set; all `None` ⇒ in-memory.

| Field | Default | Purpose |
| --- | --- | --- |
| `event_log` / `content_store` / `dispatcher` | `None` | the durable storage triple |

**Runtime injections**

| Field | Default | Purpose |
| --- | --- | --- |
| `app_gateway` | `None` | `AppPreviewGateway`; `None` ⇒ no `open_app` tool |
| `write_roots` | `None` | `(task_id) → Sequence[str]` extra write roots |
| `mcp_server_resolver` | `None` | `(alias) → McpAnyServerSpec \| None`, resolved per turn |
| `mcp_http_post` | `None` | injectable HTTP transport (`HttpPostFn`) for remote MCP |
| `delta_sink` | `None` | `(StepContext, call_id, StreamDelta) → None` — ephemeral token deltas from a streaming-capable provider; never persisted |
| `otlp_traces` / `otlp_http_post` | `None` | OTLP trace export config + transport |
| `provider_headers` | `None` | `(StepContext) → Mapping[str, str]` per-request headers |

**Sandbox / execution environment**

| Field | Default | Purpose |
| --- | --- | --- |
| `exec_env` | `None` | `SandboxExecEnvConfig` — **attach** one shared container |
| `sandbox_provider` | `None` | `SandboxProvider` — provision a fresh container per session; takes precedence over `exec_env` |
| `sandbox_spec` | `None` | deployment-fixed half of the per-session `SandboxSpec` (image, caps, base mounts) |
| `sandbox_exec_preamble` | `None` | `(exec_env_ref, argv) → prefix`, re-invoked per container command |
| `sandbox_backend_factory` / `sandbox_browser_factory` | `None` | swap the sandbox wire without touching the seam |
| `sandbox_session_policy` | `None` | `(root_task_id, workspace_dir) → bool` per-session opt-out |

**Memory** — precedence `memory_root_resolver` > `memory_dir` >
`global_memory_dir` > `~/.noeta/memories`. See
[Multi-tenant memory](../how-to/multi-tenant-memory.md).

| Field | Default | Purpose |
| --- | --- | --- |
| `memory_dir` / `global_memory_dir` | `None` | host-level store roots |
| `memory_root_resolver` | `None` | `(task_id) → Path \| None` per-task root |

**Kill-switches and policy**

| Field | Default | Purpose |
| --- | --- | --- |
| `workflow_allowed` | `False` | expose `run_workflow` (also requires delegation) |
| `max_background_jobs_per_session` | `8` | over the cap a background `shell_run` is rejected, not queued |
| `max_background_subagents_per_session` | `8` | same for `spawn_subagent(background=True)` |
| `instructions_enabled` | `False` | load the workspace-root `NOETA.md` → `AGENTS.md` |
| `instructions_file` | `None` | read only this path instead of the search |
| `instructions_discovery` | `False` | `read`-triggered discovery of subdirectory instruction files ([Composer & cache](../concepts/composer-and-cache.md)) |
| `write_mode` | `"dry_run"` | `"apply"` performs real writes |

Related re-exports from `noeta.tools.app` / `noeta.tools.mcp`:
`AppPreviewGateway`, `AppMount`, `McpServerSpec` (stdio),
`McpHttpServerSpec`, `McpAnyServerSpec` (their union), `McpError`,
`McpConfigError`, `HttpPostFn`.

## Errors (typed / coded)

Boundary code matches errors structurally — `isinstance(exc, CodedError)` +
`exc.code` — never by message text. `CodedError` is the base
(`noeta/protocols/errors.py`).

| Error | `code` | Source |
| --- | --- | --- |
| `QueryFailedError` — carries `task_id`, `status`, `reason`, `retryable` | `query_failed` | `client/client.py` |
| `ModelSelectorError` | `model_selector_rejected` | `noeta/execution/driver.py` |
| `ProviderSelectorError` | `provider_selector_rejected` | `driver.py` |
| `NotResumableError` | `not_resumable` | `driver.py` |
| `TaskAlreadyTerminalError` | `task_already_terminal` | `driver.py` |
| `UnsupportedSubtaskSuspend` | `unsupported_subtask_suspend` | `noeta/execution/subtask_drain.py` |

## Capability projections

Three **functions** (`packages/noeta-sdk/noeta/client/capabilities.py`):

- `permission_modes() → tuple[str, ...]` — the legal `permission_mode`
  values (`capabilities.py`).
- `effort_modes() → tuple[str, ...]` — the legal `effort` values
  (`capabilities.py`).
- `model_capabilities(models) → dict[str, dict[str, bool]]` — per-model
  capability flags, e.g. the vision gate (`capabilities.py`).

## Extension interfaces

Implement one of these and mount it through the matching `Options` field:

| Interface | Mount via | Source |
| --- | --- | --- |
| `Tool` (protocol: metadata + `invoke(arguments, ctx) → ToolResult`) | `allowed_tools` | `noeta/protocols/tool.py` |
| `ToolContext` / `ToolResult` (`success`, `output`, `artifacts`, `output_ref`) | tool call inputs/outputs | `tool.py` / `tool.py` |
| `LLMProvider` | `provider` | `noeta/protocols/messages.py` |
| `StreamingProvider` / `StreamDelta` (optional capability: `complete_streaming(request, on_delta, request_headers=None)` still returns the complete `LLMResponse`; deltas are ephemeral side effects) | implement alongside `LLMProvider` on `provider`; consumed via `HostConfig.delta_sink` | `messages.py` |
| `Policy` | `policy` | `noeta/protocols/policy.py` |
| `Guard` / `GuardContext` / `VerdictResult` | `guards` | `noeta/protocols/hooks.py` |
| `ProposedAction` and its members `ProposedToolCall` / `ProposedSpawnSubtask` / `ProposedFinish` (a guard dispatches on them) | passed to `Guard.check` | `noeta/protocols/hooks.py` |
| `Observer` (= `Subscriber`, a `Callable[[EventEnvelope], None]`) | `observers` | `noeta/protocols/event_log.py` |
| `ContentKindSpec` | `content_channels` | `noeta/context/content_channel.py` |
| `Decision` (union of Policy decision types) | returned by a custom `Policy` | `noeta/protocols/decisions.py` |
| `StepContext` / `View` | passed to a custom `Policy` | `noeta/protocols/step_context.py` / `noeta/protocols/view.py` |

## Plugin mechanism

**Manifest-declared contribution packages over a surface registry**, with a
host-level **load** / agent-level **activation** split. Full contract in the
[Plugins reference](plugins.md); the `noeta.sdk` surface:

| Symbol | Role | Source |
| --- | --- | --- |
| `PluginManifest` / `ManifestContribution` | the static manifest (`name`, `requires-noeta`, contributions) | `client/plugin_manifest.py` |
| `PluginBuilder` | single-file decorator sugar that *is* a manifest | `client/plugin_manifest.py` |
| `SurfaceSpec` / `SurfaceRegistry` / `standard_registry` | the surface registry (plane, scope, validator, collision, merge, ordering) | `client/surfaces.py` |
| `load_plugin_set(...) → PluginSet` | the five-source loader (built-ins / entry points / modules / user dirs / workspace dirs) | `client/plugin_set.py` |
| `PluginSet` | the loaded set — listable / collision-checkable **without executing plugin code** | `client/plugin_set.py` |
| `PluginActivation` | one external plugin's identity-plane contributions (built by `Client`) | `client/options.py` |
| `DEFAULT_PLUGINS` | `("fs", "web")` — the default of `Options.plugins` (identity-inert) | `client/options.py` |
| `grant_trust` / `is_trusted` | the workspace-dir trust store | `client/plugins.py` |
| `PluginError` / `UntrustedPluginDirWarning` | loud load faults / the one non-raising skip | `client/plugins.py` |

Activate loaded plugins per-agent through `Options.plugins` /
`AgentDefinition.plugins`, and hand the `PluginSet` to `Client(options,
plugins=…)` or `query(…, plugins=…)`. Governance surfaces (`guard` / `observer`)
apply process-wide once loaded; every other surface follows activation.
`load_plugin_set` is the `noeta.sdk` name for `client.plugin_set.load_plugins`.

> The 0.4.0 `noeta_plugin(api)` bundle path (`PluginAPI`, `load_plugins`,
> `merge_plugins`, `merged_mcp_servers`, `merged_skill_dirs`, `LoadedPlugin`,
> `PluginContributions`) has been **removed** and replaced by the manifest
> mechanism above; only the shared trust-store + error primitives remain in
> `client/plugins.py`.

## Official presets

`presets` — the module re-export (`noeta.presets`,
`packages/noeta-sdk/noeta/presets/__init__.py`). Key entries:
`main_options()` (`presets/__init__.py`) returns the official main-agent
`Options`; `official_specs()` (`presets/__init__.py`) returns the
compiled four-agent set (`main` / `general-purpose` / `explore` / `plan`).

## See also

- [Your first agent](../tutorials/first-agent.md) — guided SDK walkthrough
- [Architecture overview](../architecture/overview.md) — identity vs wiring,
  the extension seams in context
- [WorkerLoop](worker-loop.md) — the resident drain primitive
