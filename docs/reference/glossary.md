# Glossary

Every Noeta term in one line or two, A to Z, with a link to the page that explains
it. The authoritative source is
[`CONTEXT.md`](https://github.com/initxy/noeta/blob/main/CONTEXT.md).

## A

- **Activation** — which loaded plugins an agent uses (`Options.plugins`). Activation is part of agent identity; an unknown name fails compilation. → [Options](options.md)
- **Agent** — a named, spawnable configuration (`AgentSpec`): instructions, policy, tools, skills, budget, plugins. The "class" of a task, not a runtime entity. → [Presets](presets.md)
- **Anchored placement** — a resident renders where it was activated: in the semi-stable segment if before the first assistant message, otherwise inline in the history. → [Context](../how-it-works/context.md)
- **App plugin** — a contribution to a host's own surface (routers, channels, schedules). Validated by the loader, handed to the host, never part of agent identity. → [Plugin surfaces](plugin-surfaces.md)
- **Artifact** — a large object a tool returns alongside its inline output, listed as `ContentRef`s on `ToolResult.artifacts`. → [Types](types.md)
- **Attempt** — one decide-then-act iteration inside a step; the unit of crash recovery. An interrupted attempt is sealed by `StepAttemptAbandoned`. → [Event log](../how-it-works/event-log.md)

## B

- **Backend bag** — the `backends` mapping on `SessionBuildContext`: live objects keyed by plugin name (`"browser"`, `"app_preview"`). No entry means the capability is off. → [Plugin surfaces](plugin-surfaces.md)
- **Browser tools** — `browser_navigate`, `browser_click`, `browser_type`, `browser_extract`, `browser_screenshot`. Need a live browser backend and the `browser` activation. Not MCP. → [Built-in tools](tools.md)
- **Budget** — resource caps: `max_iterations`, `max_tool_calls`, `max_cost_usd`, `max_spawned_subtasks`, `max_subtask_depth`. `None` means no cap; counters span the task's whole life. → [Options](options.md)
- **Built-in plugin** — one of Noeta's 18 own capabilities under `noeta/builtins/`, loaded through the same path as any external plugin. `react` cannot be disabled. → [Plugin system](../how-it-works/plugin-system.md)

## C

- **Content channel** — how resident content enters context: a `ContextContentRecorded` event records it, a `ContentKindSpec` renders it. Tenants: `skill`, `memory`, `instructions`, `environment`. → [Context](../how-it-works/context.md)
- **ContentRef** — a pointer into the `ContentStore`: `hash`, `size`, `media_type`. Looked up by `hash`. → [Types](types.md)
- **ContentStore** — content-addressed, immutable storage for large bodies. Protocol members `get` and `get_many`. → [Event log](../how-it-works/event-log.md)
- **Context segments** — the three parts of a View: `stable_prefix` (system prompt, tool schemas), `semi_stable` (residents), `dynamic_suffix` (history, reminders). The prefix must stay byte-identical for the provider cache. → [Context](../how-it-works/context.md)
- **ContextComposer** — builds a View from folded state and the `ContentStore`, with no LLM call. The composer is closed; extend it by registering a content kind or a reminder. → [Context](../how-it-works/context.md)
- **ContextPlan** — per-call metadata: which skills and messages were selected, dropped or cleared. Stored for audit and debugging. → [Context](../how-it-works/context.md)
- **Contract** — a task's immutable header in its first `TaskCreated` event: `goal`, `policy_name`, `agent_name`, `inputs`, `parent_task_id`, `subtask_depth`. → [Tasks and waking](../how-it-works/tasks-and-waking.md)
- **Control tool mount** — a `control_tool` contribution that builds a control tool (`TodoWrite`, `skill`, `Task`…) after the tool set is assembled; returning `None` disables it. → [Plugin surfaces](plugin-surfaces.md)

## D

- **Decision** — what `Policy.decide` returns: `tool_calls`, `spawn_subtask`, `spawn_subtasks`, `yield_for_human`, `wait_timer`, `wait_external`, `state_patch`, `compaction_requested`, `finish`, `fail`. → [Engine](../how-it-works/engine.md)
- **Dispatcher** — schedules tasks: enqueue, leases, wake delivery, reclaiming stale leases. Task state is never read from it. → [Tasks and waking](../how-it-works/tasks-and-waking.md)

## E

- **Engine** — advances one task by one step (`run_one_step`). Its main loop is fixed, not an extension point. → [Engine](../how-it-works/engine.md)
- **Event / EventEnvelope** — one record on a stream: `seq`, `type`, `actor`, `origin`, `trace_id`, `causation_id`, plus a typed payload. → [Types](types.md)
- **EventLog** — one append-only event stream per task; the source of truth. Inline payloads are capped at 4 KB (`EVENT_PAYLOAD_MAX_BYTES`). → [Event log](../how-it-works/event-log.md)
- **ExecEnv** — the backend fs and shell tools act through: `LocalExecEnv` (host) or `AioSandboxExecEnv` (container). Never part of a tool schema. → [Sandbox](../guides/sandbox.md)

## F

- **Fork** — see *Rewind and fork*.

## G

- **Guard** — a synchronous check before a tool call, spawn or finish, returning `allow`, `deny` or `require_approval`. A guard that raises counts as `deny`. → [Engine](../how-it-works/engine.md)

## I

- **Inspect** — reading a task back: `Client.events` / `events_after` return raw envelopes, `Client.messages` returns the readable history. No side effects. → [SDK](sdk.md)
- **Instructions discovery** — optional: activates `NOETA.md` / `AGENTS.md` / `CLAUDE.md` from directories between a file the agent read and the workspace root. Off by default. → [Context](../how-it-works/context.md)
- **Interrupt** — stops only the running turn (`TurnInterrupted`); the task stays open for the next message. `force=True` clears a wedged step. On a pending question it withdraws the question instead. → [SDK](sdk.md)

## L

- **Lease** — a worker's short exclusive hold on a task (`lease_id`, `task_id`, `expires_at`), renewed by heartbeat, reclaimed when expired. Every append presents it. → [Worker loop](worker-loop.md)

## M

- **Memory** — cross-task, file-based memory the model manages with `memory_write`, `memory_archive`, `memory_read`, `memory_search`. Only `main` activates it among the official agents. → [Multi-tenant memory](../guides/multi-tenant-memory.md)
- **Memory consolidation** — a background agent (`__consolidation__`) that merges, archives and tidies the memory store. Runs as its own root task; archives, never deletes. → [Multi-tenant memory](../guides/multi-tenant-memory.md)
- **Memory recall** — before a turn, pages the message names ride in automatically; a page the model already read is skipped. `Options.recall_model` adds a small-model fallback. → [Multi-tenant memory](../guides/multi-tenant-memory.md)

## O

- **Observer** — an asynchronous, read-only subscriber to the event log. It runs after commit; its failure never affects the task. → [Engine](../how-it-works/engine.md)
- **Options** — the declarative agent recipe (`noeta.sdk.Options`), compiled into an `AgentSpec` plus its descendants. → [Options](options.md)
- **Origin** — who wrote a message: `human`, `system` or `memory` (default `None`, the role's natural author). Only the Engine sets it; `system` / `memory` turns show as `InjectedMessage`. → [Types](types.md)

## P

- **PackContribution** — what a session pack returns: `tools`, `content_kinds`, `init`, and a few typed side fields. Empty means "not applicable". → [Plugin surfaces](plugin-surfaces.md)
- **Plugin** — a pip package or single `.py` file with a static manifest naming its contributions. The manifest is read without importing plugin code. → [Write a plugin](../guides/plugins.md)
- **PluginSet** — the result of `load_plugins(...)`, passed as `Client(options, plugins=...)`. Auditable without running plugin code; `.resolve()` is the only import point. → [Plugin manifest](plugin-manifest.md)
- **Policy** — decides the next step from a View (`decide(ctx, view) -> Decision`). Default is `ReActPolicy`, identity `("react", "1")`. → [Engine](../how-it-works/engine.md)
- **Principal** — who is acting and which models they may use (`identity`, `allowed_models`). Only `principal_identity` is recorded. → [Options](options.md)
- **Provider** — an adapter to an external service. `LLMProvider` is set with `Options.provider`; adapters live in `noeta.sdk.providers`. → [Connect a model](../guides/models.md)

## R

- **Reminders** — text added to context. `reminder_provider` runs at intake and is recorded; `reminder` is pure, rendered at the history tail and never recorded; resident content is the third path. → [Plugin surfaces](plugin-surfaces.md)
- **Resume** — continuing a suspended task: fold its log, drive it under a lease. Triggered by a matching wake event (`send_goal`, `approve` / `deny`, `answer`, `deliver_event`). → [Tasks and waking](../how-it-works/tasks-and-waking.md)
- **Rewind and fork** — both branch at a user message. Rewind appends `TaskRewound` to the same task and restores edited files; fork starts a new task (`TaskForked`) and leaves the source intact. → [SDK](sdk.md)

## S

- **SandboxProvider** — provisions and reaps a container per root task (`allocate`, `release`, `attach`). `ExecEnv` then talks to it. → [Sandbox](../guides/sandbox.md)
- **Session pack** — a `session_pack` contribution that builds part of a task's tool set and residents from a `SessionBuildContext`. It returns an empty contribution when it does not apply. → [Plugin surfaces](plugin-surfaces.md)
- **SessionBuildContext** — the frozen input every session pack reads: workspace, content store, exec env, model, allowed tools, backend bag, plugin config. → [Plugin surfaces](plugin-surfaces.md)
- **Skill** — a static workflow template at `.noeta/skills/<name>/SKILL.md`. The model sees a menu of names and summaries; the body loads only when chosen. Not a tool. → [Presets](presets.md)
- **Snapshot** — a `TaskSnapshot` event pointing at the full state in the `ContentStore`, written before each suspend and terminal event. Speeds up folding; not required. → [Event log](../how-it-works/event-log.md)
- **Step** — one Engine pass: compose → decide → dispatch, looping on tool calls until a suspend or terminal. → [Engine](../how-it-works/engine.md)
- **Subtask** — a task spawned by a parent, linked by `parent_task_id` and `subtask_depth`. Otherwise an ordinary task. → [Subagents](../guides/subagents.md)
- **Surface / SurfaceSpec** — a named extension point and its description (plane, scope, validator, collision key, order). There are 16 standard surfaces. → [Plugin surfaces](plugin-surfaces.md)
- **Suspended** — a task parked on a wake condition, whatever it waits for (subtask, approval, timer, external event). States: `pending`, `running`, `suspended`, `terminal`. → [Tasks and waking](../how-it-works/tasks-and-waking.md)

## T

- **Task** — one execution of an agent; the only first-class entity. It can spawn subtasks, suspend and resume. → [Tasks and waking](../how-it-works/tasks-and-waking.md)
- **Task state slices** — four slices, each with one writer: `RuntimeState` (Engine), `TaskState` (Policy's `state_patch`), `ContextState` (fold), `GovernanceState` (fold). → [Event log](../how-it-works/event-log.md)
- **TaskState** — the slice holding the task's working memory: goal, phase, todos, decisions, active content. Per task, unlike *Memory*. → [Event log](../how-it-works/event-log.md)
- **Tool** — an action the agent can call: `name`, `input_schema`, `description` (what the model sees), plus `version` and `risk_level`. Not a skill. → [Built-in tools](tools.md)

## V

- **View** — the model input the composer builds for the policy; a projection of the task, not the task. → [Context](../how-it-works/context.md)

## W

- **WakeCondition / WakeEvent** — what a task waits for and what arrives: `SubtaskCompleted`, `SubtaskGroupCompleted`, `HumanResponseReceived`, `TimerFired`, `ExternalEvent`. Delivered durably, exactly once. → [Tasks and waking](../how-it-works/tasks-and-waking.md)
- **Worker** — a process that leases a task and drives it until its next suspend or terminal state. The loop is `noeta.runtime.worker.WorkerLoop`. → [Worker loop](worker-loop.md)
- **Write fence** — `Edit` and `Write` may only write inside the workspace or host-approved roots (`HostConfig.write_roots`). Reads are not fenced, and `Bash` is not confined. → [Options](options.md)

## Words Noeta does not use

`scripts/lint-naming.py` rejects the class names `Run`, `Workflow`, `Session`,
`Mutator`, `Pattern` and the identifiers `WorkflowRunner`, `WorkflowPolicy`,
`WorkflowSpec`, `SessionStore`, `ConversationManager`.

- **Run** — say Task.
- **Session** — not an identity. A conversation is one Task receiving many goals; name things by `task_id` or `root_task_id`. "For the lifetime of one root task" as a scope is fine.
- **Workflow** — not a primitive. Write a deterministic Policy plus `spawn_subtask` decisions.

## Next

- [How it works](../how-it-works/index.md)
- [SDK reference](sdk.md)
