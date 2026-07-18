# Glossary

Canonical vocabulary for Noeta. Each term has a single, stable meaning
across the codebase and docs. The authoritative source is
[`CONTEXT.md`](https://github.com/initxy/noeta/blob/main/CONTEXT.md)
in the repository root.

## Core abstractions

### Task

One execution instance of an agent; it can spawn sub-tasks and can suspend and resume. The only first-class citizen in the system.
_Avoid:_ Run, Job, Execution, Workflow Instance.

See also: [Concepts](/concepts/task-model), [ADR: Task as the only primitive](https://github.com/initxy/noeta/blob/main/docs/adr/task-as-only-primitive.md)

### Subtask

A task spawned from a parent task via `spawn_subtask`. Structurally identical to a parent task, related only through `parent_task_id`.
_Avoid:_ Child Run, Sub-agent.

See also: [Concepts](/concepts/wake-resume), [ADR: Subtask fan-out and durable wake](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-fanout-and-durable-wake.md)

### Agent

A named, spawnable configuration (policy + tools + context spec + budget). **Not a runtime entity** — just the "class" of a task. Every Agent carries a `description` used to render the subagent dispatch control tool schema.
_Avoid:_ Bot, Assistant, AI.

See also: [Presets](presets.md), [ADR: Tool and agent catalog](https://github.com/initxy/noeta/blob/main/docs/adr/tool-and-agent-catalog.md)

### Options

The declarative agent configuration (public surface `noeta.sdk.Options`). Compiled by `compile_options` into an `AgentSpec`. **The sole way to express both the official agent set and custom agents.**

See also: [API Reference](/reference/sdk), [Configuration](configuration.md)

### Step

The slice by which a task advances within one Engine main-loop pass: `compose_view → decide → dispatch`.
_Avoid:_ Iteration, Turn, Cycle.

### Decision

The return value of `Policy.decide`, input to Engine dispatch. A set of neutral mechanism variants: `tool_calls`, `spawn_subtask`, `yield_for_human`, `wait_timer`, `wait_external`, `finish`, `fail`, `spawn_subtasks`, `state_patch`.
_Avoid:_ Action, Command, Intent.

See also: [Concepts](/concepts/engine-execution)

### Policy

The function that "decides the next step given the current View." Can be a pure LLM (ReActPolicy), a pure FSM, or a hybrid.
_Avoid:_ Pattern, Strategy, Brain.

See also: [Concepts](/concepts/engine-execution), [ADR: Engine-policy-dataflow](https://github.com/initxy/noeta/blob/main/docs/adr/engine-policy-dataflow.md)

### Tool

An external action the agent can invoke. The structured-contract trio `name` / `input_schema` / `description` is hand-written and LLM-facing. Also carries `risk_level`. An **open** extension surface via `Options`.
_Avoid:_ Function, Action, Skill.

See also: [Tools Reference](tools.md), [ADR: Tool description canonical](https://github.com/initxy/noeta/blob/main/docs/adr/tool-description-canonical.md)

### Provider

A Noeta-shape adapter for an external service (LLM / storage / vector store). `LLMProvider` is open via `Options.provider` and re-exported through `noeta.sdk`. Storage backends are configured through **host config**, not Options. **Not a context content source.**
_Avoid:_ Vendor, Backend, Connector.

See also: [Configuration](configuration.md#provider-adapters), [ADR: Provider adapters and multimodal](https://github.com/initxy/noeta/blob/main/docs/adr/provider-adapters-and-multimodal.md), [ADR: Provider-neutral](https://github.com/initxy/noeta/blob/main/docs/adr/provider-neutral.md)

### Skill

A local, static LLM-workflow template at `.noeta/skills/<name>/SKILL.md`, optionally with resource files. Three-layer merge (builtin < global `~/.noeta/skills` < workspace). Two-stage on-demand loading: menu rendered into the `skill` control tool schema; body rendered into semi-stable context once selected. **Not the same thing as a Tool.**
_Avoid:_ Plugin, Module, Macro.

See also: [ADR: Model-driven skill invocation](https://github.com/initxy/noeta/blob/main/docs/adr/model-driven-skill-invocation.md), [ADR: Skill resource on-demand](https://github.com/initxy/noeta/blob/main/docs/adr/skill-resource-on-demand.md)

## State and events

### EventLog

Per-task append-only stream of `EventEnvelope` records. **The source of truth for causality and decisions.**
_Avoid:_ Journal, Log, Audit Trail.

See also: [Concepts](/concepts/event-sourcing), [ADR: Event-sourced truth](https://github.com/initxy/noeta/blob/main/docs/adr/event-sourced-truth.md)

### Event / EventEnvelope

One record in the EventLog. The envelope holds `seq / type / actor / trace_id / causation_id`; the payload is a typed dataclass.
_Avoid:_ Message, Record.

### ContentStore

Content-addressed, immutable large-object storage. **The source of truth for large objects.** Bodies larger than the 4 KB event-payload cap go here; the envelope only carries a `ContentRef`.
_Avoid:_ BLOB Store, Asset Store, Object Store.

See also: [Concepts](/concepts/event-sourcing), [ADR: Storage protocols L0](https://github.com/initxy/noeta/blob/main/docs/adr/storage-protocols-l0.md)

### ContentRef

A reference into the ContentStore: `hash + size + media_type`.
_Avoid:_ URL, Path, Pointer.

### Artifact

A large object produced by a Tool or Provider, referenced via a ContentRef.
_Avoid:_ File, Attachment, Blob.

### Snapshot

A special event in the EventLog whose body goes into the ContentStore. Written before each suspend; an acceleration point for fold.
_Avoid:_ Checkpoint, State Dump.

### Task State (four slices)

Four typed slices, each with exactly one writer:

- **RuntimeState** — messages / usage (writer: Engine)
- **TaskState** — goal / phase / todos / decisions / active_content (writer: Policy's `state_patch`)
- **ContextState** — current plan ref (writer: Engine fold)
- **GovernanceState** — cost / denied (writer: Engine)

## Execution model

### Engine

Advances a single Task by one step. ≤ 500 lines. Knows nothing of worker / dispatcher / workflow. **Locked**: not an extension point.
_Avoid:_ Runtime, Executor.

See also: [Concepts](/concepts/engine-execution), [ADR: Engine-policy-dataflow](https://github.com/initxy/noeta/blob/main/docs/adr/engine-policy-dataflow.md)

### Worker

The process that leases a Task from the Dispatcher and calls the Engine to advance it. **One lease runs until the next suspend or terminal state, then releases.**
_Avoid:_ Runner, Daemon.

See also: [Concepts](/concepts/engine-execution), [ADR: Worker lease model](https://github.com/initxy/noeta/blob/main/docs/adr/worker-lease-model.md)

### Lease

A Worker's short-term exclusive hold on a Task, with `lease_id / expires_at`.
_Avoid:_ Lock, Claim.

See also: [ADR: Worker lease model](https://github.com/initxy/noeta/blob/main/docs/adr/worker-lease-model.md), [ADR: Single-writer invariant](https://github.com/initxy/noeta/blob/main/docs/adr/single-writer-invariant.md)

### Dispatcher

Manages Task enqueue, Lease granting, Wake-event delivery, and Stale reclamation.
_Avoid:_ Scheduler, Queue Manager.

See also: [Concepts](/concepts/wake-resume), [ADR: Worker lease model](https://github.com/initxy/noeta/blob/main/docs/adr/worker-lease-model.md)

### Suspended

One of a Task's 4 states, waiting on a wake event. A **unified expression** of waiting on subtask / approval / timer / external event.
_Avoid:_ Yielded, Paused, Blocked, Waiting.

### WakeCondition / WakeEvent

Describes what a Task is waiting on. `SubtaskCompleted` / `HumanResponseReceived` / `TimerFired` / `ExternalEvent`.

See also: [Concepts](/concepts/wake-resume), [ADR: Subtask fan-out and durable wake](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-fanout-and-durable-wake.md)

## Context

### View

The LLM input the ContextComposer assembles for the Policy. **Not equal to the Task** — it is a projection.
_Avoid:_ Prompt (View is the structured form of a Prompt), Frame.

### ContextComposer

Assembles a Task into a View. The main path calls no LLM. The concrete `ThreeSegmentComposer` is a **closed** extension point on the user surface (stable-prefix KV-cache reproducibility is a hard constraint). The only open hook is registering a `ContentKindSpec`.
_Avoid:_ PromptBuilder, ContextAssembler.

See also: [ADR: Unified context supply](https://github.com/initxy/noeta/blob/main/docs/adr/unified-context-supply.md), [ADR: Context compaction](https://github.com/initxy/noeta/blob/main/docs/adr/context-compaction.md)

### ContextPlan

The View metadata for a given LLM call (which blocks were selected, what was compacted, what was dropped). Used for audit and debug.
_Avoid:_ Prompt Trace.

### Stable Prefix / Semi-stable / Dynamic Suffix

The fixed segment names in the View's three-part assembly. The cache-friendliness of the Stable Prefix is a hard constraint.

### Content Channel

The generic mechanism by which resident content (skills, memory index) enters context. Two parts: **event recording** (`ContextContentRecorded`) + **assembly rendering** (`ContentChannelRegistry` renders each kind into the semi-stable segment). Registering a `ContentKindSpec` is the open extension hook.
_Avoid:_ Provider, ContentSource, Middleware.

See also: [ADR: Model-driven skill invocation](https://github.com/initxy/noeta/blob/main/docs/adr/model-driven-skill-invocation.md)

### origin

An optional author marker on a `Message`, one of `human / system / memory`, defaulting to `None` = the role's natural author. **Single-writer guard**: only the engine's recording path may write it.
_Avoid:_ Author, Sender, Role.

See also: [ADR: Event origin marker](https://github.com/initxy/noeta/blob/main/docs/adr/event-origin-marker.md)

### Memory

Cross-task long-term memory (v2): **mutate** = `memory_write` (optional frontmatter `description` / `type`) and `memory_archive` (retire into `archive/`, never delete) tools, **read** = `memory_read` (full text) and `memory_search` (substring, excerpts) tools, **resident index** = content channel tenant (`kind="memory"`, policy `evolving`), **auto-recall** = host retrieves at user-message seam (name tokens first, then summary tokens), **policy** = the `MEMORY_POLICY_PROMPT` fragment on memory-enabled preset prompts. Controlled by `Capabilities.memory`. A background **consolidation** pass (a hidden `__consolidation__` agent on the resident worker pool, session-stop triggered, debounced) merges / archives / backfills memories; its toggle is host configuration, not agent identity — see [ADR: Memory consolidation](https://github.com/initxy/noeta/blob/main/docs/adr/memory-consolidation.md).
_Avoid:_ using "Memory" to mean TaskState (that is in-task state; this is cross-task).

## Governance

### Principal

The initiator of, or party responsible for, a Task; holds identity / capabilities / allowed_side_effects / delegation chain.
_Avoid:_ User (a User is a kind of Principal), Actor.

### Contract

A Task's input, expected-output schema, rejection conditions, and side-effect declaration. Frozen into the `TaskCreated` event.
_Avoid:_ Spec, Schema.

### Budget

A Task's resource ceilings (iterations / cost_usd / wall_seconds / tool_calls).
_Avoid:_ Quota, Limit.

### Guard

A synchronous hook that runs at three points — `before_tool_call` / `before_spawn_subtask` / `before_finish` — returning `allow / deny / require_approval`.
_Avoid:_ Middleware, Interceptor, Filter.

See also: [Concepts](/concepts/guard-observer), [ADR: Guard-observer hooks](https://github.com/initxy/noeta/blob/main/docs/adr/guard-observer-hooks.md)

### Observer

An asynchronous hook subscribed to the EventLog; its failure does not affect the Task.
_Avoid:_ Listener, Subscriber.

See also: [Concepts](/concepts/guard-observer), [ADR: Guard-observer hooks](https://github.com/initxy/noeta/blob/main/docs/adr/guard-observer-hooks.md)

### Mutator

**Deprecated in Noeta v2.** Hooks may not modify ctx / payload. To modify, change the Policy or the Composer instead.

## Operations

### Inspect

Reads the EventLog + ContentStore and presents history to a human. No external IO.
_Avoid:_ View Log, Dump.

### Resume

Continues actual execution from a suspended state. An operational emergency-stop lever; the normal path is triggered by a wake event.
_Avoid:_ Restart, Continue.

See also: [Failure Modes](/operations/troubleshooting)

## Application layer (the noeta-agent platform)

Vocabulary owned by the product ([ADR: server-platform product](https://github.com/initxy/noeta/blob/main/docs/adr/server-platform-product.md)). None of these terms exists below the application layer: the engine knows only Tasks.

### Session

The application-layer unit of conversation — what the UI lists, resumes, and deletes. Owned by a user, scoped to a Space; groups **one or more engine tasks** (a workflow session owns one root task per node) and owns one workspace directory and one sandbox container. App-layer indexing only: persisted in the application database; the EventLog stays the single source of truth.
_Avoid:_ Conversation, Thread; using Session below the application layer.

### Space

The unit of collaboration and scoping. Users belong to spaces; a space scopes skills, knowledge sources, agent memory, MCP connectors, agent-config, and templates. Every user gets a personal space; team spaces have owner-managed membership. Session visibility = space membership.
_Avoid:_ Team, Organization, Workspace (Workspace is the session's file root).

### UI event

One frame of the product wire vocabulary (`user_message`, `assistant_text`, `thinking`, `tool_call` / `tool_result`, `skill_activated`, `todo_update`, `subtask_started` / `subtask_finished`, `question`, `compaction`, turn markers, …), produced by the **translator** — a deterministic, stateless, pure function over `EventEnvelope`s. Replay is re-derivation from the EventLog via `since_seq`; token deltas are ephemeral and never replayed. Raw envelopes appear only on the admin trace surface.
_Avoid:_ calling raw `EventEnvelope`s UI events; "projection" (implies a stored copy).

### Skill registry

The platform's database-backed skill surface: **builtin skills** (admin-managed, platform-wide) and **space skills** (owner-uploaded per space), both mounted read-only into session sandboxes and rendered into the model's skill menu. The management layer over the library-level Skill format (`SKILL.md` is unchanged).
_Avoid:_ Skill market, plugin store.

### Knowledge source

A space-scoped synced content source with pluggable sync adapters; the open-source core ships `git_repo` and `local_dir`. Materialized under the shared data directory, mounted read-only into sandboxes, selected into assembly through agent-config.
_Avoid:_ RAG index (there is no vector store), Dataset.

### MCP connector

A per-space MCP server configuration: alias + transport (`http` | `stdio`) + credentials + an enabled tool subset, stored in the application database and credential-scrubbed on every read. Resolved into the agent host per turn; tools appear as `mcp__<alias>__<tool>`. Replaces the retired global `~/.noeta/mcp_servers.json` registry.
_Avoid:_ global MCP registry (retired), plugin.

### Agent-config

The space's agent configuration: persona prompt (written into the session workspace `AGENT.md` at assembly), default model / reasoning effort, knowledge-source selection, memory toggle. Owner-managed via `GET/PUT /api/v1/spaces/{id}/agent-config`.
_Avoid:_ Options (the SDK-level agent configuration), Settings (server config).

### Feedback loop

Per-message ratings from space members feeding an owner-triggered analysis agent whose suggestions are owner-gated: adopt into space memory, apply a skill patch (after a backup), or export a markdown report.
_Avoid:_ RLHF (nothing trains a model).

## Flagged ambiguities

### "Workflow"

Not a first-class concept in the engine. Express fixed procedures with a deterministic Policy + `spawn_subtask`. An orchestration script the model improvises lands as **one Task + a Policy that interprets that script**. (The platform's *workflow session* is app-layer sequencing of root tasks, not an engine primitive.)

### "Session"

An **application-layer concept only** (see above). Below the application layer it remains a non-concept: the engine knows only Tasks, and multi-turn conversation is one Task receiving user input repeatedly.

### "Run"

Not a first-class concept. Always use Task.
