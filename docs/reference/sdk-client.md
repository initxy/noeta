# query / Client

These are the verbs that make an agent run. `query` drives one goal to its
answer and tears everything down; `Client` keeps a conversation alive across
turns, approvals and restarts. Both live in
`packages/noeta-sdk/noeta/client/client.py` and are re-exported from
`noeta.sdk`.

If you are configuring *what* the agent is, you want
[Options](sdk-options.md) instead. This page is about *running* it.

## Which one do I need?

| You want | Use | Why |
| --- | --- | --- |
| one goal, one answer, no follow-up | `query(...)` | creates a throwaway `Client`, drives to a terminal, returns the whole envelope stream |
| a conversation — follow-up questions, approvals, cancel | `Client` | keeps the task alive; every verb resumes the same `task_id` |
| many conversations at once, in one process | `Client` + `start_workers(n)` | a resident pool drains turns in the background instead of on the calling thread |

## `query`

```python
query(options, goal, *, provider=None, workspace_dir=None, model=None,
      images=(), plugins=None, host_config=None) -> QueryResult
```

It builds a temporary `Client(multi_turn=False)` so the agent reaches a genuine
terminal instead of resting on the next-goal suspend, drives one turn, folds the
projections, then shuts the client down. Its parameters mirror the `Client`
constructor, so the sugar path is not limited to in-memory storage — passing a
`host_config` records the run durably.

```python
from noeta.sdk import HostConfig, Options, query

result = query(
    Options(system_prompt="Answer in one sentence."),
    goal="What does fold(events) mean here?",
    provider=my_provider,
    workspace_dir=".",
    host_config=HostConfig(storage_path="noeta.sqlite"),
)

print(result.task_id)      # → 't-1a2b3c…'
print(len(result))         # → 14   (QueryResult is a list of EventEnvelope)
print(result.answer())     # → 'State is derived by replaying the event log.'
```

### `QueryResult`

A `list[EventEnvelope]` subclass — iteration and indexing behave like a list —
with three extras:

| Member | Returns | Notes |
| --- | --- | --- |
| `.task_id` | `str` | the task that was driven |
| `.messages()` | `list[ViewItem]` | the human-readable view, every `ContentRef` already dereferenced |
| `.answer()` | `Any` | the terminal answer; **raises `QueryFailedError`** if the task failed or never reached a terminal |

The projections are materialized against the temporary client's ContentStore
*before* teardown. Do not re-project the raw envelopes with a fresh store — the
large bodies they reference would no longer resolve. For a lenient read, take
the terminal `Result` item from `.messages()` and branch on its `status`.

## `Client`

```python
Client(options, *, provider=None, workspace_dir=None, model=None,
       multi_turn=True, host_config=None, allowed_models=None, plugins=None)
```

A provider must come from the `provider` kwarg or `Options.provider`, otherwise
the constructor raises `ValueError`. The workspace resolves
`workspace_dir` > `Options.cwd` > `Path.cwd()`. Storage defaults to in-memory;
pass a [`HostConfig`](sdk-options.md#hostconfig) to inject a durable backend.

`allowed_models` is the per-turn model-selector allowlist. `None` falls back to
`DEFAULT_MODEL_ALLOWLIST` (`opus` / `sonnet` / `haiku`); an explicitly **empty**
sequence authorizes no selector at all, while the host default still binds.

`plugins` is a loaded `PluginSet` (see [Plugins](plugins.md)). Its
identity-plane contributions reach an agent only where `Options.plugins`
activates that plugin; its guards and observers apply process-wide. An
activation name absent from the loaded set fails the build.

`Client` is a context manager, so `shutdown` cannot be forgotten:

```python
from noeta.sdk import Client, Options

with Client(Options(system_prompt="…"), provider=my_provider,
            workspace_dir=".") as client:
    outcome = client.start(goal="Summarise README.md")
    print(outcome.task_id)      # → 't-9f8e…'
```

Properties: `registry` (the compiled `AgentRegistry`), `main_agent_name`,
`workers_running`.

## Turn-driving verbs

Each runs the whole turn on the calling thread and returns a `DriveOutcome`. All
of them drain through `Options.can_use_tool` when one is configured, so a gated
tool call resolves the same way no matter which verb resumed the conversation.

| Method | Signature (keyword-only after `task_id`) |
| --- | --- |
| `start` | `(*, goal, agent=None, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), workspace_dir=None, effort=None, activations=(), attachment_texts=())` |
| `send_goal` | `(task_id, *, goal, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), effort=None, activations=(), attachment_texts=())` |
| `approve` | `(task_id, *, call_id, reason=None, resolver="client")` |
| `deny` | `(task_id, *, call_id, reason=None, resolver="client")` |
| `answer` | `(task_id, *, question_id, answers, answered_by="client")` |
| `deliver_event` | `(task_id, *, event_kind, payload=None)` |

`workspace_dir` at `start` is welded into the durable `TaskHostBound` record
once; every later turn fold-resolves it, which is why `send_goal` has no such
parameter. `permission_mode`, `enabled_mcp`, `effort` and `activations` are
per-turn, non-durable host knobs. `activations` pins built-in skills before the
loop starts — the channel a `/skill-name` slash command rides.

`attachment_texts` are host-composed reference snapshots (`@` mentions, a task
briefing, a workspace summary), each recorded as its own `origin="system"`
message **before** the goal, so the transcript never attributes them to the
person. Being ordinary recorded messages they survive resume and are never
re-read. Use this when the text is already settled at send time; when it must be
computed *while* the turn is recorded — because it reads live state — contribute
a `reminder_provider` instead ([plugin surfaces](plugin-surfaces.md)), whose
output lands **after** the goal. Both channels are reachable with public names
only: `Reminder`, `ResidentActivation`, `RecallView`, `ReminderProvider` and `TURN_INTAKE` are
exported from `noeta.sdk`.

`deliver_event` wakes a task suspended on `wait_external`. Matching is exact on
`event_kind`; the optional `payload` is recorded as an `origin="system"` message
on the resumed turn, never as the wake event itself. Delivering an event the
task is not waiting for raises `NotResumableError`.

Every verb returns a `DriveOutcome` with three fields: `task_id`, `status` (the
folded task status once the turn settled) and `wake_handle` (the
`HumanResponseReceived` handle the task is now waiting on, or `None`). The
handle is how a caller tells a routine next-goal suspend from one that is
waiting for an approval:

```python
outcome = client.start(goal="Refactor utils.py")
print(outcome.status, outcome.wake_handle)
# → suspended approval-call_7c21

if outcome.wake_handle == f"approval-{call_id}":
    outcome = client.approve(outcome.task_id, call_id=call_id)

client.send_goal(outcome.task_id, goal="Now add a test for it.")
```

A gated tool call suspends on `approval-{call_id}`; a gated `finish` or spawn
uses `approval-finish-{task_id}` / `approval-spawn-{task_id}` instead. Read the
`call_id` off the `ToolCallApprovalRequested` event in `client.events(task_id)`
rather than parsing the handle.

## Seed / drive split

An async transport should not hold a request thread for a whole turn. `seed_*`
performs every durable, validated step on the request thread — so a typed
rejection (`ModelSelectorError`, `NotResumableError`) still surfaces as a
synchronous 4xx — and returns a `SeededTurn` you then drive.

| Method | Signature |
| --- | --- |
| `seed_start` | same as `start` |
| `seed_send_goal` | same as `send_goal` |
| `seed_approve` / `seed_deny` | same as `approve` / `deny` |
| `seed_answer` | same as `answer` |
| `seed_deliver_event` | same as `deliver_event` |
| `drive_seeded` | `(seeded) -> DriveOutcome` — run the seeded turn to its next boundary **on this thread** |
| `dispatch_seeded` | `(seeded) -> None` — hand it to the resident worker pool and return at once |

Pick by who should block. `drive_seeded` runs the turn on the calling thread,
which suits a background thread you own. `dispatch_seeded` yields the seed's
lease back to the ready queue for a [resident worker](#resident-worker-pool) to
pick up and returns immediately — the shape an HTTP handler wants, since the
durable seed already made the ack crash-safe. Progress rides the committed
event stream either way.

## Resident worker pool

With workers running, `dispatch_seeded` yields the seed's lease back to the
ready queue instead of spawning a one-off thread, so conversations advance
concurrently. Wake delivery stays durable, single-worker and exactly-once: one
lease holds a task until its next suspend or terminal.

| Method | Signature |
| --- | --- |
| `start_workers` | `(num_workers=1, *, poll_interval=0.1, heartbeat_interval=30.0, stale_sweep_interval=10.0, timer_poll_interval=1.0, lease_seconds=600.0, shutdown_grace_s=10.0)`; raises `RuntimeError` if called twice |
| `stop_workers` | `(timeout=None) -> bool` — `False` when a worker did not exit in time; the pool stays tracked so a retry can finish the job |

```python
client.start_workers(4)
print(client.workers_running)   # → True
...
print(client.stop_workers(timeout=30))   # → True
```

For a worker in its own process, use the library primitive directly — see
[WorkerLoop](worker-loop.md).

## Conversation lifecycle

| Method | Signature and effect |
| --- | --- |
| `cancel` | `(task_id, *, reason="cancelled", cascade=False)` — kill the conversation; it becomes terminal |
| `interrupt` | `(task_id, *, reason=None, interrupted_by="user")` — stop the in-flight turn at its next boundary, leaving the task on its next-goal suspend so `send_goal` simply continues; thread-safe against a turn being driven |
| `close` | `(task_id, *, closed_by="user", reason=None)` — archive it |
| `reopen` | `(task_id, *, reopened_by="user", reason=None)` |
| `rewind` | `(task_id, *, message_seq)` — re-base to just before the user message at `message_seq`: that message, its output and every later turn become dead history (the log stays append-only), and workspace files the undone span edited are restored |
| `fork` | `(task_id, *, message_seq)` — same anchor, opposite retention: mint a **new** task inheriting history up to that boundary and leave the source untouched. The returned `DriveOutcome.task_id` is the fork's. Root tasks only; both branches share one workspace |

`NEXT_GOAL_WAKE_HANDLE` is the wake handle a conversation rests on between
turns. A host's session-stop seam recognizes the trailing next-goal suspend by
this constant.

## Inspection and storage

Pure reads — no external IO, no effect on the task.

| Method | Returns |
| --- | --- |
| `events(task_id)` | `list[EventEnvelope]` |
| `messages(task_id)` | `list[ViewItem]` — the folded human view |
| `task_answer(task_id)` | the latest turn's terminal answer as the **raw** value, off whichever lifecycle event that turn landed on (`TaskCompleted`, or `TaskSuspended` for a multi-turn conversation that finished a turn and parked). `None` when the latest turn produced none. Take this when you want the value — an `output_schema` answer is a `dict` here, while `messages()` renders it through `str()` for the transcript |
| `events_after(task_id, after_seq=None)` | the stream strictly past a cursor |
| `task_streams()` | one `TaskStreamSummary` per driven stream, carrying `task_id` and `last_seq` |
| `delete_task(task_id)` | `{"ok", "task_id", "deleted": [...], "reason"?}`; refuses with `reason="running"` or `"not_found"` |
| `get_content(content_hash)` | `bytes \| None` |
| `put_content(body, *, media_type)` | `ContentRef` |
| `memory_root(task_id=None)` | `Path` — the store this task resolves to under the multi-tenant chain |
| `subscribe(callback)` | an unsubscribe callable; post-commit envelopes, all tasks |
| `add_sandbox_lifecycle_listener(on_allocate, on_release)` | product wiring for container-tracked side effects; a no-op without a sandbox |
| `shutdown()` | idempotent: stops workers, tears down observers and the trace sink, releases the sandbox |

## Memory consolidation

Three host-callable entry points curate the long-term memory store. All are
exported from `noeta.sdk` and defined in `client/consolidation.py`.

| Function | Effect |
| --- | --- |
| `run_consolidation(client, *, memory_root, now=None, debounce=True, debounce_hours=24.0, max_root_tasks=10, max_chars_per_root_task=16000, include_task=None, on_seeded=None) -> bool` | enqueue one background run; `True` iff one was enqueued. Debounce-not-elapsed and nothing-to-digest both return `False` without raising |
| `consolidation_due(memory_root, *, now, debounce_hours=24.0) -> bool` | the debounce half alone |
| `build_consolidation_digest(client, *, since=None, max_root_tasks=10, max_chars_per_root_task=16000, include_task=None) -> str \| None` | the digest half alone, for a host that orchestrates its own runs |

## Errors

Boundary code should match errors **structurally** —
`isinstance(exc, CodedError)` plus `exc.code` — never by message text.
`CodedError` is the base (`noeta/protocols/errors.py`).

| Error | `code` | Raised by |
| --- | --- | --- |
| `QueryFailedError` — carries `task_id`, `status`, `reason`, `retryable` | `query_failed` | `QueryResult.answer()` |
| `ModelSelectorError` | `model_selector_rejected` | the turn driver, at seed time |
| `ProviderSelectorError` | `provider_selector_rejected` | the turn driver, at seed time |
| `NotResumableError` | `not_resumable` | `deliver_event`, `send_goal` on a task that cannot take one |
| `TaskAlreadyTerminalError` | `task_already_terminal` | any verb on a finished task |
| `UnknownTaskError` — carries `task_id`, `verb`, `reason` | `unknown_task` | `cancel` / `interrupt` / `close` / `reopen` on an id that names no live stream. Refused **before** the verb writes, so a typo'd id cannot mint a stream whose genesis is a control event |
| `UnsupportedSubtaskSuspend` | `unsupported_subtask_suspend` | subtask drain |

## Next

- [Options](sdk-options.md) — the recipe these verbs run
- [Types & testing](sdk-types.md) — `EventEnvelope`, `ViewItem`, `FakeLLMProvider`
- [WorkerLoop](worker-loop.md) — running a drain loop in its own process
- [Wake & resume](../concepts/wake-resume.md) — what happens between two turns
