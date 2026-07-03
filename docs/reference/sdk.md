# SDK reference (`noeta.sdk`)

`noeta.sdk` is the single public import surface of the SDK. Everything below
is re-exported from it — users never import `noeta.client` or runtime
internals directly. Source of truth: the `__all__` list in
`packages/noeta-sdk/noeta/sdk/__init__.py:108-174`.

```python
from noeta.sdk import query, Client, Options, tool
```

## Client verbs

### `query(options, goal, *, provider=None, workspace_dir=None, model=None, images=()) → QueryResult`

One-shot query: drives a single turn to a genuine terminal and returns the
full envelope stream with pre-folded projections
(`packages/noeta-sdk/noeta/client/client.py:984`). Creates a temporary
`Client(multi_turn=False)` and shuts it down before returning. Use `Client`
directly for multi-turn work.

### `QueryResult` — `client/client.py:881`

A `list[EventEnvelope]` subclass (iteration/indexing behave like a list) plus:

| Member | Returns | Notes |
| --- | --- | --- |
| `.task_id` | `str` | the driven task |
| `.messages()` | `list[ViewItem]` | pre-folded human view; every `ContentRef` already dereferenced |
| `.answer()` | `Any` | the terminal answer; **raises `QueryFailedError`** on a failed or non-terminal task |

The projections are materialized against the temporary Client's ContentStore
before teardown — do not re-project raw envelopes with a fresh store.

### `Client` — `client/client.py:122`

```python
Client(options, *, provider=None, workspace_dir=None, model=None,
       multi_turn=True, host_config=None, allowed_models=None)
```

(`client/client.py:147`) A provider must come from the `provider` kwarg or
`Options.provider`, and a workspace from `workspace_dir` or `Options.cwd` —
otherwise `ValueError`. Storage defaults to in-memory; pass a `HostConfig` to
inject a durable triple.

| Method | Signature (keyword-only after `task_id`) | Source |
| --- | --- | --- |
| `start` | `(*, goal, agent=None, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), workspace_dir=None, effort=None)` → outcome | `client.py:391` |
| `send_goal` | `(task_id, *, goal, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), effort=None)` → outcome | `client.py:439` |
| `approve` | `(task_id, *, call_id, reason=None, resolver="client")` | `client.py:474` |
| `deny` | `(task_id, *, call_id, reason=None, resolver="client")` | `client.py:487` |
| `answer` | `(task_id, *, question_id, answers, answered_by="client")` | `client.py:500` |
| `cancel` | `(task_id, *, reason="cancelled", cascade=False)` | `client.py:623` |
| `close` | `(task_id, *, closed_by="user", reason=None)` | `client.py:635` |
| `reopen` | `(task_id, *, reopened_by="user", reason=None)` | `client.py:647` |
| `events` | `(task_id)` → `list[EventEnvelope]` | `client.py:671` |
| `messages` | `(task_id)` → `list[ViewItem]` | `client.py:675` |
| `events_after` | `(task_id, after_seq=None)` → `list[EventEnvelope]` — the stream strictly past a cursor | `client.py:685` |
| `task_streams` | `()` → per-task `(task_id, last_seq)` summaries | `client.py:695` |
| `delete_task` | `(task_id)` → `{"ok", "reason"?, "task_id", "deleted": [...]}`; refuses with `reason="running"` / `"not_found"` | `client.py:704` |
| `subscribe` | `(callback)` → unsubscribe callable; post-commit envelopes, all tasks | `client.py:812` |
| `shutdown` | `()` — idempotent observer teardown | `client.py:822` |

Properties: `registry` (the compiled `AgentRegistry`, `client.py:661`) and
`main_agent_name` (`client.py:666`). `workspace_dir` at `start` is welded into
the durable `TaskHostBound` once; later turns fold-resolve it.
`permission_mode` / `enabled_mcp` / `effort` are per-turn, non-durable host
knobs.

## The recipe: `Options`

### `Options` — `client/options.py:197`

Frozen dataclass compiled into `AgentSpec`s. Fields split into **identity**
(enter the recording) and **wiring** (mount-point only, ignored by
`compile_options`):

| Field | Type / default | Kind |
| --- | --- | --- |
| `system_prompt` | `str \| SystemPromptPreset` — required | identity |
| `name` | `str = "main"` | identity |
| `skills` | `tuple[str, ...] = ()` | identity |
| `budget` | `BudgetSpec \| None` — `None` ⇒ default with `max_subtask_depth=3` | identity |
| `capabilities` | `Capabilities \| None` — `None` ⇒ derived from children | identity |
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
invalid `permission_mode` raises at compile time (`options.py:541`).

### `AgentDefinition` — `client/options.py:121`

Flat child-agent recipe: `description` (required, non-empty), `prompt`
(required), `tools` (`None` ⇒ all built-ins), `model`, `capabilities`,
`metadata`. Cannot nest — children are leaves.

### `SystemPromptPreset` — `client/options.py:101`

`preset: str = "main"`, `append: str | None = None` — resolves a registered
preset prompt, optionally appending a suffix.

### `compile_options(options) → (AgentSpec, tuple[AgentSpec, ...])` — `client/options.py:514`

Pure compile of the recipe into `(main_spec, descendant_specs)`.
Referentially transparent: equal `Options` produce equal `AgentSpec`s.

### `register_preset_prompt(name, prompt) → None` — `client/options.py:84`

Registers a named preset for `SystemPromptPreset` (last-writer-wins).

## Authoring

### `@tool` — `packages/noeta-runtime/noeta/tools/decorator.py:99`

```python
@tool(name="word_count", version="1", risk_level="low",
      input_schema={...}, description="...")
def word_count(arguments: dict, ctx: ToolContext) -> ToolResult: ...
```

Wraps `fn(arguments, ctx) → ToolResult` as a `DecoratedTool`
(`decorator.py:43`). `version` is **required** — omitting it raises
`TypeError` (version feeds the identity fingerprint). `risk_level` defaults
to `"low"`. `input_schema` is LLM-facing metadata (not validated at runtime);
`description` is the model's single source of tool semantics. Also callable
directly: `tool(fn, name=..., ...)`.

### `create_sdk_mcp_server(name, version="1.0.0", tools=()) → SdkMcpServer` — `sdk/authoring.py:60`

Bundles `@tool` functions into an in-process (`"sdk"` transport) MCP server
for `Options.mcp_servers`. Empty `name` raises `ValueError`; a non-
`DecoratedTool` entry raises `TypeError`. `SdkMcpServer`
(`sdk/authoring.py:35`) is frozen: `name`, `version`, `tools`.

## Message projection & wire

### `as_messages(envelopes, content_store) → list[ViewItem]` — `client/messages.py:150`

Pure projection of an envelope stream into the human-readable view. The
`content_store` must be the one **paired with** the stream. `ViewItem`
(`messages.py:136`) is the union of:

| Type | Fields | Source |
| --- | --- | --- |
| `AssistantMessage` | `text` | `messages.py:80` |
| `UserMessage` | `text` | `messages.py:87` |
| `ToolUse` | `call_id`, `tool_name`, `arguments` | `messages.py:94` |
| `ToolResultView` | `call_id`, `tool_name`, `success`, `output: str \| None` | `messages.py:108` |
| `Result` | `answer`, `status` — on `"failed"`, `answer` holds the failure reason | `messages.py:123` |

### `envelope_to_dict(env) → dict` — `client/wire.py:25`

Canonical JSON-ready dict form of an `EventEnvelope` (the wire shape the
SSE stream and the web frontend consume).

### Content blocks

`ImageBlock` (`noeta/protocols/messages.py:121`) — an image input block for
`start` / `send_goal` / `query(images=…)`. `ContentRef`
(`noeta/protocols/values.py:27`) — `hash + size + media_type` reference into
the ContentStore.

## Host-level wiring

### `HostConfig` — `client/host_config.py:38`

Frozen dataclass passed as `Client(..., host_config=…)`; never part of agent
identity. Fields: the durable storage triple `event_log` / `content_store` /
`dispatcher` (**all-or-none** — `storage_triple()` at `host_config.py:85`
raises `ValueError` on a partial set; all `None` ⇒ in-memory), `app_gateway`
(`AppPreviewGateway` — `None` ⇒ no `open_app` tool), `mcp_server_resolver`
(`(alias) → McpAnyServerSpec | None`), `mcp_http_post` (injectable HTTP
transport, `HttpPostFn`), `workflow_allowed: bool = False`, and
`write_mode: str = "dry_run"` (`"apply"` performs real writes).

Related re-exports from `noeta.tools.app` / `noeta.tools.mcp`:
`AppPreviewGateway`, `AppMount`, `McpServerSpec` (stdio),
`McpHttpServerSpec`, `McpAnyServerSpec` (their union), `McpError`,
`McpConfigError`, `HttpPostFn`.

## Errors (typed / coded)

Boundary code matches errors structurally — `isinstance(exc, CodedError)` +
`exc.code` — never by message text. `CodedError` is the base
(`noeta/protocols/errors.py:18`).

| Error | `code` | Source |
| --- | --- | --- |
| `QueryFailedError` — carries `task_id`, `status`, `reason`, `retryable` | `query_failed` | `client/client.py:848` |
| `ModelSelectorError` | `model_selector_rejected` | `noeta/execution/driver.py:123` |
| `ProviderSelectorError` | `provider_selector_rejected` | `driver.py:144` |
| `NotResumableError` | `not_resumable` | `driver.py:171` |
| `TaskAlreadyTerminalError` | `task_already_terminal` | `driver.py:204` |
| `UnsupportedSubtaskSuspend` | `unsupported_subtask_suspend` | `noeta/execution/subtask_drain.py:110` |

## Capability projections

Three **functions** (`packages/noeta-sdk/noeta/client/capabilities.py`):

- `permission_modes() → tuple[str, ...]` — the legal `permission_mode`
  values (`capabilities.py:21`).
- `effort_modes() → tuple[str, ...]` — the legal `effort` values
  (`capabilities.py:26`).
- `model_capabilities(models) → dict[str, dict[str, bool]]` — per-model
  capability flags, e.g. the vision gate (`capabilities.py:31`).

## Extension interfaces

Implement one of these and mount it through the matching `Options` field:

| Interface | Mount via | Source |
| --- | --- | --- |
| `Tool` (protocol: metadata + `invoke(arguments, ctx) → ToolResult`) | `allowed_tools` | `noeta/protocols/tool.py:132` |
| `ToolContext` / `ToolResult` (`success`, `output`, `artifacts`, `output_ref`) | tool call inputs/outputs | `tool.py:108` / `tool.py:19` |
| `LLMProvider` | `provider` | `noeta/protocols/messages.py:286` |
| `Policy` | `policy` | `noeta/protocols/policy.py:21` |
| `Guard` / `GuardContext` / `ProposedAction` / `VerdictResult` | `guards` | `noeta/protocols/hooks.py:159` / `111` / (payload types) / `45` |
| `Observer` (= `Subscriber`, a `Callable[[EventEnvelope], None]`) | `observers` | `noeta/protocols/event_log.py:47` |
| `ContentKindSpec` | `content_channels` | `noeta/context/content_channel.py:63` |
| `Decision` (union of Policy decision types) | returned by a custom `Policy` | `noeta/protocols/decisions.py:427` |
| `StepContext` / `View` | passed to a custom `Policy` | `noeta/protocols/step_context.py:17` / `noeta/protocols/view.py:70` |

## Official presets

`presets` — the module re-export (`noeta.presets`,
`packages/noeta-runtime/noeta/presets/__init__.py`). Key entries:
`main_options()` (`presets/__init__.py:159`) returns the official main-agent
`Options`; `official_specs()` (`presets/__init__.py:185`) returns the
compiled four-agent set (`main` / `general-purpose` / `explore` / `plan`).

## See also

- [Your first agent](../tutorials/first-agent.md) — guided SDK walkthrough
- [Architecture overview](../architecture/overview.md) — identity vs wiring,
  the extension seams in context
- [WorkerLoop](worker-loop.md) — the resident drain primitive
