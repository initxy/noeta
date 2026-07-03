# Glossary

Canonical vocabulary for Noeta. Each term has a single, stable meaning
across the codebase and docs. The authoritative source is
[`CONTEXT.md`](https://github.com/initxy/noeta/blob/main/CONTEXT.md)
in the repository root.

## Core abstractions

### Task

One execution instance of an agent; it can spawn sub-tasks and can suspend and resume. The only first-class citizen in the system.
_Avoid:_ Run, Job, Execution, Workflow Instance.

See also: [Concepts](../concepts.md#task), [ADR: Task as the only primitive](../adr/task-as-only-primitive.md)

### Subtask

A task spawned from a parent task via `spawn_subtask`. Structurally identical to a parent task, related only through `parent_task_id`.
_Avoid:_ Child Run, Sub-agent.

See also: [Concepts](../concepts.md#wake-resume), [ADR: Subtask fan-out and durable wake](../adr/subtask-fanout-and-durable-wake.md)

### Agent

A named, spawnable configuration (policy + tools + context spec + budget). **Not a runtime entity** — just the "class" of a task. Every Agent carries a `description` used to render the subagent dispatch control tool schema.
_Avoid:_ Bot, Assistant, AI.

See also: [Presets](presets.md), [ADR: Tool and agent catalog](../adr/tool-and-agent-catalog.md)

### Options

The declarative agent configuration (public surface `noeta.sdk.Options`). Compiled by `compile_options` into an `AgentSpec`. **The sole way to express both the official agent set and custom agents.**

See also: [API Reference](api/index.md), [Configuration](configuration.md)

### Step

The slice by which a task advances within one Engine main-loop pass: `compose_view → decide → dispatch`.
_Avoid:_ Iteration, Turn, Cycle.

### Decision

The return value of `Policy.decide`, input to Engine dispatch. A set of neutral mechanism variants: `tool_calls`, `spawn_subtask`, `yield_for_human`, `wait_timer`, `wait_external`, `finish`, `fail`, `spawn_subtasks`, `state_patch`.
_Avoid:_ Action, Command, Intent.

See also: [Concepts](../concepts.md#policy)

### Policy

The function that "decides the next step given the current View." Can be a pure LLM (ReActPolicy), a pure FSM, or a hybrid.
_Avoid:_ Pattern, Strategy, Brain.

See also: [Concepts](../concepts.md#policy), [ADR: Engine-policy-dataflow](../adr/engine-policy-dataflow.md)

### Tool

An external action the agent can invoke. The structured-contract trio `name` / `input_schema` / `description` is hand-written and LLM-facing. Also carries `risk_level`. An **open** extension surface via `Options`.
_Avoid:_ Function, Action, Skill.

See also: [Tools Reference](tools.md), [ADR: Tool description canonical](../adr/tool-description-canonical.md)

### Provider

A Noeta-shape adapter for an external service (LLM / storage / vector store). `LLMProvider` is open via `Options.provider` and re-exported through `noeta.sdk`. Storage backends are configured through **host config**, not Options. **Not a context content source.**
_Avoid:_ Vendor, Backend, Connector.

See also: [Configuration](configuration.md#provider-adapters), [ADR: Provider adapters and multimodal](../adr/provider-adapters-and-multimodal.md), [ADR: Provider-neutral](../adr/provider-neutral.md)

### Skill

A local, static LLM-workflow template at `.noeta/skills/<name>/SKILL.md`, optionally with resource files. Three-layer merge (builtin < global `~/.noeta/skills` < workspace). Two-stage on-demand loading: menu rendered into the `skill` control tool schema; body rendered into semi-stable context once selected. **Not the same thing as a Tool.**
_Avoid:_ Plugin, Module, Macro.

See also: [ADR: Model-driven skill invocation](../adr/model-driven-skill-invocation.md), [ADR: Skill resource on-demand](../adr/skill-resource-on-demand.md)

## State and events

### EventLog

Per-task append-only stream of `EventEnvelope` records. **The source of truth for causality and decisions.**
_Avoid:_ Journal, Log, Audit Trail.

See also: [Concepts](../concepts.md#eventlog), [ADR: Event-sourced truth](../adr/event-sourced-truth.md)

### Event / EventEnvelope

One record in the EventLog. The envelope holds `seq / type / actor / trace_id / causation_id`; the payload is a typed dataclass.
_Avoid:_ Message, Record.

### ContentStore

Content-addressed, immutable large-object storage. **The source of truth for large objects.** Bodies larger than the 4 KB event-payload cap go here; the envelope only carries a `ContentRef`.
_Avoid:_ BLOB Store, Asset Store, Object Store.

See also: [Concepts](../concepts.md#contentstore), [ADR: Storage protocols L0](../adr/storage-protocols-l0.md)

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

See also: [Concepts](../concepts.md#engine), [ADR: Engine-policy-dataflow](../adr/engine-policy-dataflow.md)

### Worker

The process that leases a Task from the Dispatcher and calls the Engine to advance it. **One lease runs until the next suspend or terminal state, then releases.**
_Avoid:_ Runner, Daemon.

See also: [Concepts](../concepts.md#how-a-step-flows), [ADR: Worker lease model](../adr/worker-lease-model.md)

### Lease

A Worker's short-term exclusive hold on a Task, with `lease_id / expires_at`.
_Avoid:_ Lock, Claim.

See also: [ADR: Worker lease model](../adr/worker-lease-model.md), [ADR: Single-writer invariant](../adr/single-writer-invariant.md)

### Dispatcher

Manages Task enqueue, Lease granting, Wake-event delivery, and Stale reclamation.
_Avoid:_ Scheduler, Queue Manager.

See also: [Concepts](../concepts.md#dispatcher), [ADR: Worker lease model](../adr/worker-lease-model.md)

### Suspended

One of a Task's 4 states, waiting on a wake event. A **unified expression** of waiting on subtask / approval / timer / external event.
_Avoid:_ Yielded, Paused, Blocked, Waiting.

### WakeCondition / WakeEvent

Describes what a Task is waiting on. `SubtaskCompleted` / `HumanResponseReceived` / `TimerFired` / `ExternalEvent`.

See also: [Concepts](../concepts.md#wake-resume), [ADR: Subtask fan-out and durable wake](../adr/subtask-fanout-and-durable-wake.md)

## Context

### View

The LLM input the ContextComposer assembles for the Policy. **Not equal to the Task** — it is a projection.
_Avoid:_ Prompt (View is the structured form of a Prompt), Frame.

### ContextComposer

Assembles a Task into a View. The main path calls no LLM. The concrete `ThreeSegmentComposer` is a **closed** extension point on the user surface (stable-prefix KV-cache reproducibility is a hard constraint). The only open hook is registering a `ContentKindSpec`.
_Avoid:_ PromptBuilder, ContextAssembler.

See also: [ADR: Unified context supply](../adr/unified-context-supply.md), [ADR: Context compaction](../adr/context-compaction.md)

### ContextPlan

The View metadata for a given LLM call (which blocks were selected, what was compacted, what was dropped). Used for audit and debug.
_Avoid:_ Prompt Trace.

### Stable Prefix / Semi-stable / Dynamic Suffix

The fixed segment names in the View's three-part assembly. The cache-friendliness of the Stable Prefix is a hard constraint.

### Content Channel

The generic mechanism by which resident content (skills, memory index) enters context. Two parts: **event recording** (`ContextContentRecorded`) + **assembly rendering** (`ContentChannelRegistry` renders each kind into the semi-stable segment). Registering a `ContentKindSpec` is the open extension hook.
_Avoid:_ Provider, ContentSource, Middleware.

See also: [ADR: Model-driven skill invocation](../adr/model-driven-skill-invocation.md)

### origin

An optional author marker on a `Message`, one of `human / system / memory`, defaulting to `None` = the role's natural author. **Single-writer guard**: only the engine's recording path may write it.
_Avoid:_ Author, Sender, Role.

See also: [ADR: Event origin marker](../adr/event-origin-marker.md)

### Memory

Cross-task long-term memory v1: **write** = `memory_write` tool, **read** = `memory_read` tool, **resident index** = content channel tenant (`kind="memory"`, policy `evolving`), **auto-recall** = host retrieves at user-message seam. Controlled by `Capabilities.memory`.
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

See also: [Concepts](../concepts.md#guard-observer), [ADR: Guard-observer hooks](../adr/guard-observer-hooks.md)

### Observer

An asynchronous hook subscribed to the EventLog; its failure does not affect the Task.
_Avoid:_ Listener, Subscriber.

See also: [Concepts](../concepts.md#guard-observer), [ADR: Guard-observer hooks](../adr/guard-observer-hooks.md)

### Mutator

**Deprecated in Noeta v2.** Hooks may not modify ctx / payload. To modify, change the Policy or the Composer instead.

## Operations

### Inspect

Reads the EventLog + ContentStore and presents history to a human. No external IO.
_Avoid:_ View Log, Dump.

### Resume

Continues actual execution from a suspended state. An operational emergency-stop lever; the normal path is triggered by a wake event.
_Avoid:_ Restart, Continue.

See also: [Failure Modes](../failure-modes.md)

## Flagged ambiguities

### "Workflow"

Not a first-class concept. Express fixed procedures with a deterministic Policy + `spawn_subtask`. An orchestration script the model improvises lands as **one Task + a Policy that interprets that script**.

### "Session"

Not a first-class concept. Multi-turn conversation is simply one Task receiving user input repeatedly: **one interactive session = one Task**.

### "Run"

Not a first-class concept. Always use Task.
