# SDK reference: `query` and `Client`

Everything public is imported from `noeta.sdk`; this page covers the verbs that run an agent. The source of truth is `__all__` in `packages/noeta-sdk/noeta/sdk/__init__.py` — a name not listed there is not public.

## Which entry point do I need

| You want | Use |
| --- | --- |
| one goal, one answer, no follow-up | `query(options, goal, ...)` |
| a conversation: follow-ups, approvals, cancel, resume | `Client(options, ...)` |
| many conversations at once in one process | `Client` + `start_workers(n)` |
| a worker in its own process | [`WorkerLoop`](worker-loop.md) |

## Import map

| Import from | Names | Page |
| --- | --- | --- |
| `noeta.sdk` | `query`, `QueryResult`, `Client`, `DriveOutcome`, `SeededTurn`, `TaskStatus`, `DeleteTaskResult`, `DEFAULT_MODEL_ALLOWLIST`, `NEXT_GOAL_WAKE_HANDLE`, the errors | this page |
| `noeta.sdk` | `Options`, `AgentDefinition`, `SystemPromptPreset`, `compile_options`, `register_preset_prompt`, `BudgetSpec`, `HostConfig`, `PluginActivation`, `DEFAULT_PLUGINS`, `permission_modes`, `effort_modes`, `model_capabilities`, sandbox / MCP / OTLP wiring types | [Options](options.md) |
| `noeta.sdk` | `tool`, `create_sdk_mcp_server`, extension Protocols, message and event types, `as_messages`, `envelope_to_dict` | [Types](types.md) |
| `noeta.sdk` | `PluginManifest`, `ManifestContribution`, `PluginBuilder`, `PluginSet`, `load_plugins`, `SurfaceSpec`, `SurfaceRegistry`, `standard_registry`, `grant_trust`, `is_trusted`, `PluginError` and the plugin warnings | [Plugin manifest](plugin-manifest.md), [Plugin surfaces](plugin-surfaces.md) |
| `noeta.sdk` | `Reminder`, `ResidentActivation`, `RecallView`, `ReminderProvider`, `TURN_INTAKE` | [Plugin surfaces](plugin-surfaces.md) |
| `noeta.sdk` | `run_consolidation`, `consolidation_due`, `build_consolidation_digest`, `SkillUsage`, `skill_usage_from_events`, `rank_skills_by_usage`, `decayed_usage_score` | [below](#memory-and-skill-helpers) |
| `noeta.sdk.providers` | `AnthropicProvider`, `OpenAICompatProvider`, `OpenAIResponsesProvider`, `CATALOG`, `ModelSpec`, `register_models`, `find_spec`, `catalog_models` | [Connect a model](../guides/models.md) |
| `noeta.sdk.storage` | `open_storage_stack`, `build_storage_stack`, `is_memory_path`, `is_postgres_url`, the Sqlite / Postgres adapters | [Options → storage](options.md#storage) |
| `noeta.sdk.testing` | `FakeLLMProvider`, `FakeStreamingLLMProvider` | [Types → test doubles](types.md#test-doubles) |
| `noeta.presets` (also `noeta.sdk.presets`) | the official agents | [Presets](presets.md) |

The submodules are separate so you only pay for what you use: `providers` pulls in `httpx`, `storage` pulls in `psycopg` for Postgres, and `testing` is never reachable from a production import.

## `query`

```python
query(options, goal, *, provider=None, workspace_dir=None, model=None,
      images=(), plugins=None, host_config=None) -> QueryResult
```

Runs one goal to a terminal answer on a throwaway `Client(multi_turn=False)`, then shuts it down.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `options` | `Options` | required | the agent recipe |
| `goal` | `str` | required | the task text |
| `provider` | `LLMProvider \| None` | `None` | overrides `Options.provider`; one of the two is required |
| `workspace_dir` | `Path \| None` | `None` | falls back to `Options.cwd`, then the process working directory |
| `model` | `str \| None` | `None` | per-client model default (no allowlist check) |
| `images` | `Sequence[ImageBlock]` | `()` | images sent with the goal |
| `plugins` | `PluginSet \| None` | `None` | loaded plugins |
| `host_config` | `HostConfig \| None` | `None` | durable storage and other host wiring |

```python
from noeta.sdk import HostConfig, Options, query
from noeta.sdk.providers import AnthropicProvider

result = query(
    Options(system_prompt="Answer in one sentence."),
    goal="What is an append-only log?",
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
    host_config=HostConfig(storage_path="noeta.sqlite"),  # optional: keep the record
)
print(result.answer())
```

### `QueryResult`

A `list[EventEnvelope]` (iterate and index it like a list) with three extras.

| Member | Returns | Meaning |
| --- | --- | --- |
| `.task_id` | `str` | the task that ran |
| `.messages()` | `list[ViewItem]` | readable transcript, content already resolved |
| `.answer()` | `Any` | the terminal answer; raises `QueryFailedError` if the task failed or never finished |

::: warning
The projections are resolved before the temporary client shuts down. Don't re-project the raw envelopes against a fresh content store — the bodies they reference won't be there.
:::

## `Client`

```python
Client(options, *, provider=None, workspace_dir=None, model=None,
       multi_turn=True, host_config=None, allowed_models=None, plugins=None)
```

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `options` | `Options` | required | the agent recipe |
| `provider` | `LLMProvider \| None` | `None` | overrides `Options.provider`; neither set raises `ValueError` |
| `workspace_dir` | `Path \| None` | `None` | falls back to `Options.cwd`, then `Path.cwd()` |
| `model` | `str \| None` | `None` | per-client model default, not checked against the allowlist |
| `multi_turn` | `bool` | `True` | `True`: a finished turn parks on the next-goal wait; `False`: it completes |
| `host_config` | `HostConfig \| None` | `None` | durable storage, sandbox, MCP, memory wiring; `None` = in-memory |
| `allowed_models` | `Sequence[str] \| None` | `None` | per-turn `model_selector` allowlist; `None` = `DEFAULT_MODEL_ALLOWLIST` (`opus`, `sonnet`, `haiku`); `()` allows no selector |
| `plugins` | `PluginSet \| None` | `None` | loaded plugins; their agent-level contributions apply only where `Options.plugins` activates them, guards and observers apply to every task |

Properties: `registry` (the compiled `AgentRegistry`), `main_agent_name`, `workers_running`.

Use it as a context manager so `shutdown()` always runs:

```python
from noeta.sdk import Client, Options
from noeta.sdk.providers import AnthropicProvider

with Client(Options(system_prompt="You are a coding assistant."),
            provider=AnthropicProvider(), model="claude-sonnet-5", workspace_dir=".") as client:
    out = client.start(goal="Summarise README.md")
    print(client.task_answer(out.task_id))
    out = client.send_goal(out.task_id, goal="Now list its headings.")
```

### Start and drive a turn

Each verb runs the turn on the calling thread and returns a `DriveOutcome(task_id, status, wake_handle)`. All of them pass gated calls through `Options.can_use_tool` when it is set.

| Method | Signature (keyword-only after `task_id`) | Meaning |
| --- | --- | --- |
| `start` | `(*, goal, agent=None, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), workspace_dir=None, effort=None, activations=(), attachment_texts=())` | create a task and run its first turn |
| `send_goal` | `(task_id, *, goal, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), effort=None, activations=(), attachment_texts=())` | add a follow-up turn |
| `inject_goal` | `(task_id, *, goal, images=(), goal_origin=None, drive=True)` | running task: record the message, deliver it at the next turn boundary, return at once; parked on next-goal: acts like `send_goal` (or raises `NotResumableError` if `drive=False`) |
| `deliver_event` | `(task_id, *, event_kind, payload=None)` | wake a task waiting in `wait_external` on exactly `event_kind`; `payload` is recorded as a system message |

| Per-turn argument | Meaning |
| --- | --- |
| `agent` | which compiled agent to run; default is the main agent |
| `model_selector` | model alias for this turn, checked against `allowed_models` (else `ModelSelectorError`) |
| `permission_mode` | override for this turn: `default` / `acceptEdits` / `bypassPermissions` |
| `enabled_mcp` | MCP aliases enabled for this turn (resolved by `HostConfig.mcp_server_resolver`) |
| `workspace_dir` | `start` only: recorded once on the task; later turns reuse it |
| `effort` | reasoning effort for this turn |
| `activations` | skill names to load before the turn — what a `/skill-name` command uses |
| `attachment_texts` | host-written context (an `@` mention, a briefing), each recorded as its own system message before the goal |

`DriveOutcome.status` is the task status after the turn (`suspended` for a normal finished turn, `terminal` after a failure or cancel). `wake_handle` says what it waits on: `NEXT_GOAL_WAKE_HANDLE` for "type again", `approval-{call_id}` for a gated call, `None` when not waiting on a person.

### Approve, deny, answer

| Method | Signature | Meaning |
| --- | --- | --- |
| `approve` | `(task_id, *, call_id, reason=None, resolver="client")` | run the gated call and continue |
| `deny` | `(task_id, *, call_id, reason=None, resolver="client")` | refuse it; the model sees the refusal and the turn continues |
| `answer` | `(task_id, *, question_id, answers, answered_by="client")` | answer an `AskUserQuestion` |

```python
out = client.start(goal="Refactor utils.py")
if out.wake_handle and out.wake_handle.startswith("approval-"):
    req = next(e for e in client.events(out.task_id)
               if e.type == "ToolCallApprovalRequested")
    out = client.approve(out.task_id, call_id=req.payload.call_id)
```

A gated tool call waits on `approval-{call_id}`. A gated `finish` or spawn waits on `approval-finish-{task_id}` / `approval-spawn-{task_id}` with `call_id` `finish-{task_id}` / `spawn-{task_id}`. Read the `call_id` from the `ToolCallApprovalRequested` event instead of parsing the handle.

### Seed now, drive later

For an HTTP handler that must not block for a whole turn: `seed_*` does every durable, validated step on the request thread (so `ModelSelectorError` / `NotResumableError` still surface synchronously) and returns a `SeededTurn`.

| Method | Signature | Meaning |
| --- | --- | --- |
| `seed_start`, `seed_send_goal`, `seed_approve`, `seed_deny`, `seed_answer`, `seed_deliver_event` | same as the matching verb | returns `SeededTurn` without running the turn |
| `drive_seeded` | `(seeded) -> DriveOutcome` | run it on this thread |
| `dispatch_seeded` | `(seeded) -> None` | hand it to the worker pool and return at once; needs `start_workers` |

### Conversation control

| Method | Signature | Meaning |
| --- | --- | --- |
| `interrupt` | `(task_id, *, reason=None, interrupted_by="user", force=False)` | stop the running turn at its next boundary; the task parks on next-goal so `send_goal` continues. Thread-safe. `force=True` clears a step stuck past every checkpoint — call plain `interrupt` first |
| `cancel` | `(task_id, *, reason="cancelled", cascade=False)` | end the conversation (terminal); also kills its background shells |
| `close` | `(task_id, *, closed_by="user", reason=None)` | mark it archived; status stays `suspended`, and `send_goal` reopens it |
| `reopen` | `(task_id, *, reopened_by="user", reason=None)` | clear the closed mark |
| `rewind` | `(task_id, *, message_seq)` | undo the user message at `message_seq` and everything after; files it edited are restored; the log stays append-only |
| `fork` | `(task_id, *, message_seq)` | new task with history up to that message; source untouched; returns the fork's `task_id`. Root tasks only; both share the workspace |

### Inspect

Reads only; nothing is written.

| Method | Returns | Meaning |
| --- | --- | --- |
| `events(task_id)` | `list[EventEnvelope]` | the full stream |
| `events_after(task_id, after_seq=None)` | `list[EventEnvelope]` | stream past a cursor |
| `messages(task_id)` | `list[ViewItem]` | readable transcript |
| `task_answer(task_id)` | `Any` | latest turn's answer as the raw value (an `output_schema` answer is a `dict`); `None` if none |
| `task_status(task_id)` | `TaskStatus \| None` | `task_id`, `status`, `closed`, `wake_handle`, `parent_task_id`; `None` for an unknown id |
| `suspend_reason(task_id)` | `SuspendReason \| None` | why it last paused; compare `.kind` with `SUSPEND_REASON_WAITING_HUMAN` / `_INTERRUPTED` / `_TURN_FAILED` |
| `task_streams()` | `list[TaskStreamSummary]` | every stream: `task_id`, `last_seq`, `last_event_time` |
| `task_summaries()` | `list[dict]` | every task folded into a row; reads the whole log — for boot-time repair, not list rendering |
| `subscribe(callback)` | unsubscribe callable | live committed envelopes for all tasks |
| `get_content(content_hash)` | `bytes \| None` | read a stored blob |
| `put_content(body, *, media_type)` | `ContentRef` | store bytes (for example an image upload) |
| `memory_root(task_id=None)` | `Path` | the memory store this task resolves to |
| `delete_task(task_id)` | `DeleteTaskResult` | hard-delete a task and its subtasks: `{ok, task_id, deleted, reason?}`; refuses with `reason="running"` or `"not_found"` |

### Workers and lifecycle

| Method | Signature | Meaning |
| --- | --- | --- |
| `start_workers` | `(num_workers=1, *, poll_interval=0.1, heartbeat_interval=30.0, stale_sweep_interval=10.0, timer_poll_interval=1.0, lease_seconds=600.0, shutdown_grace_s=10.0)` | start a resident pool of worker threads on this client's queue; a second call raises `RuntimeError` |
| `stop_workers` | `(timeout=None) -> bool` | `False` if a worker did not exit in time; call again to finish |
| `reconnect_mcp` | `(alias=None)` | drop pooled MCP connections (all, or one alias); running turns keep theirs until they settle |
| `add_sandbox_lifecycle_listener` | `(on_allocate, on_release)` | hooks for container allocation; no-op without a sandbox |
| `shutdown` | `()` | idempotent: stops workers, observers, MCP connections, the sandbox |

## Memory and skill helpers

| Function | Meaning |
| --- | --- |
| `run_consolidation(client, *, memory_root, now=None, debounce=True, debounce_hours=24.0, max_root_tasks=10, max_chars_per_root_task=16000, include_task=None, on_seeded=None) -> bool` | queue one background memory-curation run; `True` if queued |
| `consolidation_due(memory_root, *, now, debounce_hours=24.0) -> bool` | only the debounce check |
| `build_consolidation_digest(client, *, since=None, max_root_tasks=10, max_chars_per_root_task=16000, include_task=None) -> str \| None` | only the digest, for hosts that run curation themselves |
| `skill_usage_from_events(events) -> dict[str, SkillUsage]` | count skill activations; `SkillUsage(count, last_used_at)` |
| `decayed_usage_score(usage, *, now, half_life_days=7.0, floor=0.1) -> float` | time-decayed score |
| `rank_skills_by_usage(usage, *, now, half_life_days=7.0, floor=0.1) -> dict[str, float]` | the rank a `skill_menu_rank_resolver` returns |

## Errors

Match errors by `isinstance(exc, CodedError)` and `exc.code`, never by message text.

| Error | `code` | Raised when |
| --- | --- | --- |
| `QueryFailedError` (`task_id`, `status`, `reason`, `retryable`) | `query_failed` | `QueryResult.answer()` on a failed or unfinished task |
| `ModelSelectorError` | `model_selector_rejected` | `model_selector` not in the allowlist |
| `ProviderSelectorError` | `provider_selector_rejected` | a `(provider, model)` pair the host has not configured |
| `NotResumableError` | `not_resumable` | the task isn't waiting for this verb (e.g. `deliver_event` for an event it isn't waiting on) |
| `TaskAlreadyTerminalError` | `task_already_terminal` | a verb on a finished task |
| `UnknownTaskError` (`task_id`, `verb`, `reason`) | `unknown_task` | `cancel` / `interrupt` / `close` / `reopen` on an id with no stream; refused before anything is written |
| `NotForkableError` (`task_id`, `reason`) | `not_forkable` | `fork` on an unknown id, a subtask, or a `message_seq` that isn't a user message |
| `UnsupportedSubtaskSuspend` | `unsupported_subtask_suspend` | a driven subagent paused on an approval, question or timer (only delegation is supported inside a child) |

## Next

- [Options](options.md) — configure the agent these verbs run
- [Types](types.md) — events, messages, `@tool`, test doubles
- [Tasks and waking](../how-it-works/tasks-and-waking.md) — what happens between turns
