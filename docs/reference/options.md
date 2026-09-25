# Options and HostConfig

`Options` describes the agent; `HostConfig` describes the deployment it runs in. Source: `packages/noeta-sdk/noeta/client/options.py` and `client/host_config.py`.

```python
from noeta.sdk import Client, HostConfig, Options
from noeta.sdk.providers import AnthropicProvider

options = Options(
    system_prompt="You are a careful coding agent.",
    permission_mode="acceptEdits",
    max_turns=40,
)
host = HostConfig(storage_path="noeta.sqlite", write_mode="apply")

with Client(options, provider=AnthropicProvider(), model="claude-sonnet-5", host_config=host) as client:
    client.start(goal="Fix the failing test in tests/test_utils.py")
```

## `Options`

A frozen dataclass. Fields are either **identity** (compiled into the recorded `AgentSpec`; change one and it is a different agent) or **wiring** (ignored by `compile_options` and excluded from `==`, so swapping a provider or directory never changes identity).

### Identity fields

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `system_prompt` | `str \| SystemPromptPreset` | required | the instructions, verbatim or a named preset |
| `name` | `str` | `"main"` | agent name; must not collide with an `agents` key |
| `skills` | `tuple[str, ...]` | `()` | skills activated for this agent |
| `budget` | `BudgetSpec \| None` | `None` | caps; `None` means `BudgetSpec(max_subtask_depth=3)` |
| `plugins` | `tuple[str, ...]` | `DEFAULT_PLUGINS` = `("fs", "web")` | plugins this agent activates ([below](#plugin-activation)) |
| `agents` | `Mapping[str, AgentDefinition]` | `{}` | subagents, a flat dict |
| `allowed_tools` | `tuple[str \| tool, ...] \| None` | `None` | replaces the tool list; `None` = the 10 built-ins, `()` = none |
| `disallowed_tools` | `tuple[str, ...]` | `()` | removed from whichever list applies; unknown names ignored |
| `permission_mode` | `str` | `"default"` | `default` / `acceptEdits` / `bypassPermissions` |
| `max_turns` | `int \| None` | `None` | shorthand for `budget.max_iterations`; setting both raises `ValueError` |
| `policy` | `(llm) -> Policy` with `.ref` | `None` | replaces the built-in ReAct loop |
| `mcp_servers` | `tuple[SdkMcpServer, ...]` | `()` | in-process MCP servers; their tools join the tool list |

The 10 built-in tools are `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`, `BashOutput`, `KillShell`, `WebFetch`, `WebSearch` (`WebSearch` only appears when `NOETA_WEB_SEARCH_API_KEY` is set). See [Tools](tools.md).

### Wiring fields

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `provider` | `LLMProvider \| None` | `None` | the model adapter; `Client(provider=...)` wins |
| `model` | `str \| None` | `None` | model id or alias for decide turns |
| `compaction_model` | `str \| None` | `None` | cheaper model for context-compaction summaries only; `None` = `model` |
| `recall_model` | `str \| None` | `None` | turns on the memory-recall judge when keyword recall finds nothing; `None` = keyword recall only |
| `webfetch_model` | `str \| None` | `None` | model that digests a fetched page for `WebFetch`; `None` = the main model |
| `metadata` | `Mapping[str, str]` | `{}` | labels for observers |
| `cwd` | `str \| Path \| None` | `None` | workspace fallback when `Client` gets no `workspace_dir` |
| `can_use_tool` | `(tool_name, arguments) -> bool` | `None` | approve/deny gated calls in code; recorded with `resolver="can_use_tool"` |
| `output_schema` | `Mapping \| None` | `None` | JSON Schema for the final answer; the answer comes back as a `dict` / `list` (raw text if it doesn't parse) |
| `thinking` | `"adaptive" \| "disabled" \| None` | `None` | reasoning mode; `None` = provider default |
| `effort` | `"low" \| "medium" \| "high" \| "xhigh" \| "max" \| None` | `None` | reasoning effort |
| `guards` | `tuple[Guard, ...]` | `()` | checks that run before an action and can deny it |
| `observers` | `tuple[Observer, ...]` | `()` | callbacks on each committed event |
| `content_channels` | `tuple[ContentKindSpec, ...]` | `()` | extra resident context blocks |

Invalid `thinking` / `effort` raise `ValueError` at construction, and so does `thinking="disabled"` with `effort="xhigh"` or `"max"` (Anthropic rejects that pair).

### `AgentDefinition`

A subagent. It cannot nest: declare every agent at the top level of `Options.agents`.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `description` | `str` | required | shown to the parent model in the `Task` tool; blank raises `ValueError` |
| `prompt` | `str` | required | the subagent's instructions |
| `tools` | `tuple \| None` | `None` | `None` = the built-in tools |
| `model` | `str \| None` | `None` | model for this subagent (an alias such as `haiku` resolves through the catalog); `None` = host default |
| `plugins` | `tuple[str, ...]` | `()` | no `fs`/`web` default; a subagent gets the `Task` tool only with `("delegation",)` here |
| `metadata` | `Mapping[str, str]` | `{}` | labels, not identity |

### `SystemPromptPreset`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `preset` | `str` | `"main"` | a name registered with `register_preset_prompt(name, prompt)` (last write wins) |
| `append` | `str \| None` | `None` | text appended after a blank line |

`main` and `main-web` are registered for you — see [Presets](presets.md).

### `BudgetSpec`

All fields default to `None` (no cap): `max_iterations`, `max_tool_calls`, `max_cost_usd`, `max_spawned_subtasks`, `max_subtask_depth`. Counters cover the task's whole life, not one turn.

### `compile_options`

```python
compile_options(options, *, plugins=None, preset_prompts=None)
    -> tuple[AgentSpec, tuple[AgentSpec, ...]]
```

Pure: equal `Options` give equal specs. `plugins` maps plugin name to `PluginActivation` (the `Client` builds it from its `PluginSet`); `preset_prompts` replaces the process-wide preset registry for a hermetic compile.

## Permission modes

| Mode | Asks before running |
| --- | --- |
| `default` | every tool whose `risk_level` is not `low` |
| `acceptEdits` | the same, except `Edit` and `Write` |
| `bypassPermissions` | nothing |

`Bash` and `WebFetch` are also gated per call: a command outside the shell allowlist, or a host outside `HostConfig.webfetch_allowed_hosts`, asks — except under `bypassPermissions`. A `Guard` can still deny in any mode.

Read the legal values at runtime, in display order:

```python
from noeta.sdk import effort_modes, model_capabilities, permission_modes

permission_modes()   # ('default', 'acceptEdits', 'bypassPermissions')
effort_modes()       # ('low', 'medium', 'high', 'xhigh', 'max')
model_capabilities(["claude-sonnet-4-6", "gpt-4o-mini"])
# {'claude-sonnet-4-6': {'supports_vision': True}, 'gpt-4o-mini': {'supports_vision': True}}
```

An uncatalogued model reports `supports_vision: True`.

## Plugin activation

`Options.plugins` names the plugins this agent uses, and the names are recorded in `AgentSpec.plugins`. A name must be one of:

| Kind | Names |
| --- | --- |
| built-in feature (turns a capability on) | `memory`, `browser`, `mcp`, `todo_write`, `ask_user_question`, `skill_invocation`, `delegation` |
| built-in, no effect on the agent (recognised so typos fail) | `app`, `fs`, `governance`, `presets`, `providers`, `react`, `reminders`, `sandbox`, `skills`, `storage`, `web`, `workspace` |
| a loaded plugin | any name in the `PluginSet` passed to `Client` |

```python
from noeta.sdk import DEFAULT_PLUGINS, Options

Options(system_prompt="...", plugins=DEFAULT_PLUGINS + ("memory", "todo_write"))
# plugins=("memry",) fails at compile:
#   ValueError: unknown plugin activation 'memry' on Options — not a built-in activation (...)
```

`delegation` is added automatically when `agents` is non-empty; naming it only ever turns it on. Dropping `fs`/`web` does not remove the default tools, but it does change the recorded identity.

## `HostConfig`

A frozen dataclass passed as `Client(..., host_config=...)`. Never part of agent identity. `HostConfig()` means in-memory storage, no sandbox, no MCP.

### Storage

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `storage_path` | `str \| None` | `None` | sqlite file path, `postgresql://` DSN, or `":memory:"`; `""` raises. Storage opened from it is closed by `shutdown()` |
| `event_log`, `content_store`, `dispatcher` | adapters | `None` | explicit storage; all three or none |
| `queue` | `str` | `"default"` | this client's worker queue on a shared store; its workers claim only this queue |

Passing `storage_path` and the explicit trio together, or only part of the trio, raises `ValueError`. `noeta.sdk.storage.open_storage_stack(path)` builds the trio from one string; the module also exports `build_storage_stack`, `is_memory_path`, `is_postgres_url` and the Sqlite / Postgres adapters.

### Model calls and MCP

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `provider_headers` | `(StepContext) -> Mapping[str, str]` | `None` | extra headers per model request (e.g. a gateway stickiness key) |
| `delta_sink` | `(StepContext, call_id, StreamDelta) -> None` | `None` | live token deltas from a streaming provider; never stored |
| `extra_models` | `Mapping[str, ModelSpec]` | `{}` | extra model rows for the catalog; a name clash fails; register the same rows every run |
| `mcp_server_resolver` | `(alias) -> McpAnyServerSpec \| None` | `None` | resolves MCP aliases each turn |
| `mcp_http_post` | `HttpPostFn` | `None` | custom HTTP transport for remote MCP |
| `mcp_idle_ttl` | `float \| None` | `1800.0` | seconds an unused pooled MCP connection stays open; `None` = forever; negative raises |
| `mcp_scope_resolver` | `(task_id) -> str \| None` | `None` | pool partition (e.g. a tenant id); tasks share a connection only within a scope |
| `otlp_traces` | `OtlpTraceConfig` | `None` | OTLP/HTTP trace export: `endpoint`, `headers=()`, `service_name="noeta"`. Model-call spans carry `latency_ms`, the five usage counts and `gen_ai.usage.input_tokens` / `output_tokens`; a failed task's span carries `noeta.fail_detail`; a subagent exported from another host still links to its parent's span |
| `otlp_http_post` | callable | `None` | custom transport for the exporter |

### Sandbox

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `exec_env` | `SandboxExecEnvConfig` | `None` | attach one shared container: `base_url`, `api_key_env="SANDBOX_API_KEY"`, `workdir="/workspace"` |
| `sandbox_provider` | `SandboxProvider` | `None` | a fresh container per root task; wins over `exec_env` |
| `sandbox_spec` | `SandboxSpec` | `None` | fixed part of each allocation: `image`, `mounts`, `resources`, `env` |
| `sandbox_exec_preamble` | `(exec_env_ref, argv) -> str` | `None` | shell prefix computed per command (fresh credentials) |
| `sandbox_backend_factory`, `sandbox_browser_factory` | factories | `None` | replace the sandbox or browser client |
| `sandbox_policy` | `(root_task_id, workspace_dir) -> bool` | `None` | `False` runs that task locally |
| `app_gateway` | `AppPreviewGateway` | `None` | enables the `open_app` preview tool |
| `write_roots` | `(task_id) -> Sequence[str]` | `None` | extra directories a task may write outside its workspace |
| `write_mode` | `"dry_run" \| "apply"` | `"dry_run"` | `"apply"` performs real file writes; anything else raises |

### Memory

Store root precedence: `memory_root_resolver` > `memory_dir` > `global_memory_dir` > `~/.noeta/memories`. See [Per-tenant memory](../guides/multi-tenant-memory.md).

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `memory_dir`, `global_memory_dir` | `Path \| None` | `None` | host-level store roots |
| `memory_root_resolver` | `(task_id) -> Path \| None` | `None` | per-task store root; must be deterministic per task |
| `recall_exclude` | `Collection[str]` | `()` | pages auto-recall never brings in (still listed and readable); a collection of strings — a bare `str` raises |
| `memory_max_bytes` | `int \| None` | `None` | refuse a `memory_write` whose page as stored — frontmatter plus body — is over this many UTF-8 bytes; must be positive; keep under 4096 so pages recall whole |
| `memory_read_only` | `bool` | `False` | offer only `memory_read` and `memory_search`, and drop the write guidance from the memory prompt |
| `memory_index_budget_tokens` | `int \| None` | `None` | size cap for the memory index, and for the index the recall judge reads; `None` = 1% of the context window; must be positive |

### Skills and plugins

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `skill_menu_rank_resolver` | `(task_id) -> {skill: score} \| None` | `None` | which skills keep full descriptions when the menu is over budget |
| `skill_usage_ranking` | `bool` | `True` | with no resolver, rank by recent usage across the store; off automatically when a memory or MCP scope resolver is set |
| `plugin_config` | `Mapping[str, Mapping[str, Any]]` | `{}` | per-plugin operator config; for `fs` / `skills` / `workspace` / `memory` your keys override the derived ones key by key; a plugin name the Client does not know raises a `UserWarning` |

The skill menu takes 1% of the context window, at most 4,096 tokens (the same ceiling bounds the derived memory-index budget). Over that, entries shrink to a one-sentence summary, then to the name alone, lowest rank first. A static rank can go in `plugin_config["skills"]["menu_rank"]` instead of a resolver (not both).

### Limits and switches

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `repetition_threshold` | `int \| None` | `None` | after this many identical `(tool, arguments)` calls, ask for approval; must be positive |
| `tool_output_inline_limit` | `int \| None` | `None` | truncate any tool result over this many characters before the model sees it (full bytes stay recorded); must be positive; keep it stable across resumes |
| `hooks` | `HooksConfig \| None` | `None` | user hooks — rules checked before a tool call, and commands run after one or when a call waits for approval; see [Hooks](#hooks) |
| `webfetch_allowed_hosts` | `Sequence[str]` | `()` | hosts `WebFetch` reaches without approval: `"example.com"` exact, `"*.example.com"` subdomains only; bad entries raise |
| `workflow_allowed` | `bool` | `False` | expose `run_workflow` (also needs delegation) |
| `max_background_jobs_per_root_task` | `int` | `8` | background `Bash` jobs past this are rejected; must be positive |
| `max_background_subagents_per_root_task` | `int` | `8` | same for `Task(background=True)`; must be positive |
| `environment_enabled` | `bool` | `True` | record the working directory / git / platform block at task start |
| `instructions_enabled` | `bool` | `False` | load `NOETA.md`, else `AGENTS.md`, else `CLAUDE.md` from the workspace root |
| `instructions_file` | `Path \| None` | `None` | load this file instead of searching; setting it without `instructions_enabled=True` raises |
| `instructions_discovery` | `bool` | `False` | also pick up instruction files in subdirectories the agent reads |
| `reliability_sink` | `(ReliabilityEvent) -> None` | `None` | receives the worker pool's reliability signals (`stale_requeued`, `heartbeat_invalid_lease`, `step_failed_retryable`, …) from `start_workers`; `None` logs them. Not events in the log |

::: warning
`webfetch_allowed_hosts` only controls approval prompts. `WebFetch` blocks no address itself — enforce egress at the network or sandbox.
:::

### Hooks

`HooksConfig` is validated when you build it; a rule that cannot mean anything raises `ValueError`.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `pre_tool_use` | `tuple[PreToolUseRule, ...]` | `()` | checked before each tool call; the first match decides. Rules can only tighten what the built-in guards allow. They are part of the guard stack, so a host that resumes a task must pass the same rules |
| `post_tool_use` | `tuple[PostToolUseRule, ...]` | `()` | run a command after a matching tool call finishes |
| `notification` | `tuple[NotificationRule, ...]` | `()` | run a command when a tool call starts waiting for approval |
| `command_timeout_s` | `float` | `30.0` | a hook command still running after this is killed; must be positive |
| `max_queue` | `int` | `256` | commands waiting to run; one past this is dropped with a warning instead of slowing the agent; must be positive |

| Rule | Fields |
| --- | --- |
| `PreToolUseRule` | `match_tool` (`fnmatch` pattern over the tool name, e.g. `"mcp__*"`), `action` (`"allow"` / `"deny"` / `"require_approval"`), `match_arg=None`, `reason=None` (what the model or approver sees) |
| `MatchArg` | `path` (tuple of argument keys, e.g. `("opts", "force")`), `op` (`"equals"` / `"contains"` / `"regex"`), `value`, `pattern`; a `regex` given as a string `value` is compiled when the config is built |
| `PostToolUseRule` | `match_tool`, `command=None` (an argv tuple, never run through a shell), `log=False` (log one line instead of, or as well as, running a command) |
| `NotificationRule` | `on` (`"approval"` is the only value), `command=None`, `log=False` |

Post-tool-use and notification commands are side effects. One background observer per Client runs them in the Client's workspace directory with a minimal environment (`PATH`, `HOME`, `LANG`, `LC_ALL`, `TERM`, `TMPDIR`). They never block the agent, never change the event log, and never fire again on resume. `shutdown()` stops the observer and cancels a command still running.

```python
from noeta.sdk import HooksConfig, HostConfig, MatchArg, PostToolUseRule, PreToolUseRule

hooks = HooksConfig(
    pre_tool_use=(
        PreToolUseRule(
            match_tool="Bash",
            action="require_approval",
            match_arg=MatchArg(path=("command",), op="regex", value=r"\bgit push\b"),
            reason="pushing needs a human",
        ),
    ),
    post_tool_use=(PostToolUseRule(match_tool="Edit", command=("make", "fmt")),),
)
client = Client(options, provider=provider, host_config=HostConfig(hooks=hooks))
```

## Wiring types

| Symbol | Meaning |
| --- | --- |
| `SandboxProvider` | Protocol: `allocate` / `release` / `attach` |
| `SandboxSpec`, `MountSpec` | allocation input; `MountSpec(source, target, mode="rw", kind="local-path")`, `kind` in `local-path` / `nas` / `volume` / `pvc` |
| `SandboxHandle` | a live container: `base_url`, `sandbox_id`, `auth`, `workdir="/workspace"` |
| `SandboxAuth`, `StaticApiKeyAuth` | `connect_headers()` Protocol and its env-var implementation |
| `encode_exec_env_ref`, `decode_exec_env_ref` | codec for the recorded container reference |
| `ExecEnv`, `BrowserBackend` | execution and browser Protocols |
| `BackendFactory`, `BrowserBackendFactory`, `BoundPreamble` | types for the sandbox factory fields |
| `McpServerSpec`, `McpHttpServerSpec`, `McpAnyServerSpec` | what `mcp_server_resolver` returns (stdio, HTTP, either); `call_timeout_s` (default `None` = 30 s) bounds each `tools/call`, and must be positive; `deferred=True` hides the server's tool schemas behind `ToolSearch` / `McpCall` (see [MCP servers](../guides/mcp.md)) |
| `HttpPostFn`, `McpHttpResponse`, `McpError`, `McpConfigError` | MCP transport and errors |
| `OtlpTraceConfig` | trace export config |
| `path_within(resolved, root) -> bool` | the write fence's containment check, by path component (`/srv/app-old` is not inside `/srv/app`) |

## Next

- [SDK reference](sdk.md) — the verbs that run this recipe
- [Presets](presets.md) — ready-made `Options`
- [Deploy workers](../guides/deploy.md) — storage, workers and Docker in practice
