# Glossary

Every term Noeta uses, defined once. Each entry is a short plain-language
definition plus a pointer to the page that treats it fully. The authoritative
source is [`CONTEXT.md`](https://github.com/initxy/noeta/blob/main/CONTEXT.md)
in the repository root; this page is its published form.

Terms are grouped by domain below. Use the index to jump straight to one.

## A–Z index

**A** [Activation](#activation) · [Agent](#agent) · [Anchored placement](#anchored-placement) · [App plugin](#app-plugin) · [Artifact](#artifact) · [Attempt](#attempt)

**B** [Backend bag](#backend-bag) · [Browser tool pack](#browser-tool-pack) · [Budget](#budget) · [Built-in plugin](#built-in-plugin)

**C** [Content channel](#content-channel) · [ContentRef](#contentref) · [ContentStore](#contentstore) · [Context segments](#context-segments) · [ContextComposer](#contextcomposer) · [ContextPlan](#contextplan) · [Contract](#contract) · [Control tool mount](#control-tool-mount)

**D** [Decision](#decision) · [Dispatcher](#dispatcher)

**E** [Engine](#engine) · [Event and EventEnvelope](#event-and-eventenvelope) · [EventLog](#eventlog) · [ExecEnv](#execenv)

**G** [Guard](#guard)

**I** [Inspect](#inspect) · [Interrupt](#interrupt)

**L** [Lease](#lease)

**M** [Memory](#memory) · [Memory consolidation](#memory-consolidation)

**O** [Observer](#observer) · [Options](#options) · [Origin](#origin)

**P** [PackContribution](#packcontribution) · [Plugin](#plugin) · [PluginSet](#pluginset) · [Policy](#policy) · [Principal](#principal) · [Provider](#provider)

**R** [Reminder tracks](#reminder-tracks) · [Resume](#resume) · [Rewind and fork](#rewind-and-fork) · [Run](#run)

**S** [SandboxProvider](#sandboxprovider) · [Session](#session) · [Session pack](#session-pack) · [SessionBuildContext](#sessionbuildcontext) · [Skill](#skill) · [Snapshot](#snapshot) · [Step](#step) · [Subtask](#subtask) · [Surface and SurfaceSpec](#surface-and-surfacespec) · [Suspended](#suspended)

**T** [Task](#task) · [Task state slices](#task-state-slices) · [TaskState](#taskstate) · [Tool](#tool)

**V** [View](#view)

**W** [WakeCondition and WakeEvent](#wakecondition-and-wakeevent) · [Worker](#worker) · [Workflow](#workflow) · [Write fence](#write-fence)

## Core model

### Task

One execution instance of an agent, and the only first-class citizen in the
system. It can spawn subtasks, suspend on a wake condition, and resume. The
`Task` object itself is small — `task_id`, `status`, four state slices — because
its history and snapshots live in the EventLog and the ContentStore.
→ [Task model](../concepts/task-model.md)

### Subtask

A task spawned from a parent by a `spawn_subtask` decision. Structurally
identical to any other task, related only through `parent_task_id` plus
`subtask_depth`, the delegation depth the recursion budget caps.
→ [Spawn subagents](../how-to/spawn-subagents.md)

### Agent

A named, spawnable configuration — an `AgentSpec` carrying instructions, the
policy and composer refs, tools, skills, budget, activated plugins, and the
names it may spawn. **Not a runtime entity**: it is the "class" of a task,
identity only, normalized so two specs differing just in author ordering compare
equal. A child agent's `description` is required and non-blank, because it is
rendered into the `Task` schema so the model knows who to delegate to.
→ [Presets](presets.md)

### Options

The declarative agent recipe (`noeta.sdk.Options`), compiled by
`compile_options` into an `AgentSpec` plus the flat tuple of descendant specs a
`Client` registers. It is the sole way to express both the official agents and
custom ones, and compilation is pure: equal recipes compile to equal specs.
→ [Options](sdk-options.md)

### Contract

A task's immutable header, frozen into the genesis `TaskCreated` event and never
rewritten: `goal`, `policy_name`, `agent_name`, `inputs`, `parent_task_id`,
`subtask_depth`. Every fold bootstraps empty state from it. A goal too large for
the 4 KB payload cap spills to the ContentStore, reachable through `goal_ref`.

### Budget

A task's resource ceilings — `max_iterations`, `max_tool_calls`,
`max_cost_usd`, `max_spawned_subtasks`, `max_subtask_depth`. `None` on a field
means no cap there. Which caps apply depends on the action: a tool call checks
every one, a spawn skips `max_tool_calls`, and a finish checks only the
historical accumulators, so a task that merely exhausted its tool budget may
still answer.

### Principal

Who is acting, and which models that identity may bind: an `identity` string
plus an `allowed_models` set, with `allows_any=True` modelling the unbounded set.
The driver validates a selector against the principal *and* the deployment
allowlist before any `ModelBound` is emitted. The Principal never enters an event
payload — only the `principal_identity` string rides along, as the audit link
back to who sanctioned the binding.

### Step

The slice by which a task advances within one Engine main-loop pass:
`compose → decide → dispatch`. A `tool_calls` decision keeps the loop turning in
place — append results, recompose, ask again — and any other decision ends the
step at a suspend or a terminal.
→ [Engine & execution](../concepts/engine-execution.md)

### Attempt

One decide-then-act iteration inside a Step, and the unit of crash recovery. Its
first durable record is `ContextPlanComposed`; a `StepAttemptAbandoned` marker
seals an interrupted attempt as folded-over dead history.
→ [ADR: Step-attempt recovery](https://github.com/initxy/noeta/blob/main/docs/adr/step-attempt-recovery.md)

### Decision

What `Policy.decide` returns and what Engine dispatch consumes. The variants are
deliberately neutral mechanisms: `tool_calls`, `spawn_subtask`,
`spawn_subtasks`, `yield_for_human`, `wait_timer`, `wait_external`,
`state_patch`, `compaction_requested`, `finish`, `fail`. Product control tools
get no variant of their own — `TodoWrite` and `skill` are expressed as
`state_patch`, `AskUserQuestion` as `yield_for_human`.
→ [Engine & execution](../concepts/engine-execution.md)

### Policy

The function that decides the next step given the current View
(`decide(ctx, view) -> Decision`). It can be a pure LLM (`ReActPolicy`), a pure
state machine, or a hybrid. Every compiled `AgentSpec` pins a policy identity;
the default is `("react", "1")`, and the `policy` surface is how a different
brain is substituted.
→ [Plugin surfaces](plugin-surfaces.md)

### Tool

An external action the agent can invoke. The trio `name` / `input_schema` /
`description` is hand-written and LLM-facing — `description` is the single
source of truth for what the model sees, and is never repeated in the system
prompt. A tool also carries `version` and `risk_level`, which gates approval.
**Not a Skill.**
→ [Built-in tools](tools.md)

### Skill

A local, static LLM-workflow template at `.noeta/skills/<name>/SKILL.md`,
optionally with resource files. Three tiers merge low to high: built-in, global
`~/.noeta/skills`, then workspace. Loading is two-stage — the *menu* (name plus
one-line summary) is rendered into the `skill` control-tool schema, and only the
selected skill's body enters the semi-stable segment, which compaction does not
flush. Bundled resources are reached on demand: the renderer prepends
`Base directory for this skill: <dir>` and the model reads files with `Read`.
**Not a Tool.**

### Provider

A Noeta-shape adapter for an external service; each service kind implements the
matching internal protocol. `LLMProvider` is open through `Options.provider` and
re-exported from `noeta.sdk`; the adapters and model catalog live in the
`providers` built-in, reached through `noeta.sdk.providers`. Storage backends go
through host config instead. **Not a context content source** — content enters
context only by being recorded and then rendered.
→ [Provider neutrality](../concepts/provider-neutrality.md)

## Execution

### Engine

Advances a single task by one step, where a step is a turn boundary:
`run_one_step` loops internally over `tool_calls` decisions and returns at the
next suspend or terminal. It depends on neither the Worker nor the Dispatcher.
The main loop is **locked** — not an extension point. Do not confuse the Engine
class with the `noeta-runtime` wheel.
→ [Engine & execution](../concepts/engine-execution.md)

### Worker

The process that leases a task from the Dispatcher and calls the Engine to
advance it. **One lease runs until the next suspend or terminal state, then
releases.** The drain loop ships as the library primitive
`noeta.runtime.worker.WorkerLoop`; nothing launches it for you.
→ [WorkerLoop](worker-loop.md)

### Lease

A worker's short-term exclusive hold on a task — `lease_id`, `task_id`,
`expires_at` — extended by `heartbeat` and reclaimed by `requeue_stale` once
expired. The worker presents `lease_id` on every EventLog append, which is how
the single-writer invariant is enforced.
→ [State and writers](../architecture/state-and-writers.md)

### Dispatcher

The scheduling component: task enqueue, lease granting, wake delivery, and stale
reclamation. Task state is never read back off it — production code folds the
EventLog, the single source of truth.
→ [Wake & resume](../concepts/wake-resume.md)

### Suspended

One of a task's four states (`pending`, `running`, `suspended`, `terminal`):
parked on a wake condition. It is a unified expression of waiting on a subtask,
an approval, a timer, or an external event — one state and one resume path
whatever the reason.
→ [Wake & resume](../concepts/wake-resume.md)

### WakeCondition and WakeEvent

What a task is waiting on: `SubtaskCompleted`, `SubtaskGroupCompleted`,
`HumanResponseReceived`, `TimerFired`, `ExternalEvent`. Condition and event
share one dataclass per variant — stored in `task.wake_on` it declares the shape
being waited for, delivered through `Dispatcher.wake` it carries the answer.
Matching is an identity-field projection through `matches_wake`, and every
Dispatcher routes through that helper so no adapter can diverge privately.
Delivery is durable, single-worker and exactly-once.
→ [Wake & resume](../concepts/wake-resume.md)

### Resume

Continuing a suspended task: fold its stream back into state, drive it forward
under a lease, append what happens. The trigger is a wake event matching
`wake_on`; the human-side entries are `send_goal`, `approve` / `deny`, `answer`
and `deliver_event`. State comes only from the fold, so a resumed turn never
re-invokes what the ledger already records.
→ [query / Client](sdk-client.md)

### Interrupt

The third human stop. `cancel` writes `TaskCancelled` and the conversation is
terminal; `close` archives it; **`interrupt`** writes `TurnInterrupted` and
stops only the in-flight turn, leaving the task resting at its next-goal
suspend, reopenable by simply typing again. Its granularity *is* the turn
boundary — it cannot abort a tool call already executing. The interrupted turn's
events stay on the stream as real history; un-saying them is rewind's job.

When `interrupt` lands on a task suspended on a pending `ask_user_question` (no
turn is in flight), it **withdraws the question** instead: it writes
`UserQuestionWithdrawn`, closes the dangling ask tool call with a paired
`success=False` tool result, and parks the conversation idle at the next-goal
suspend — no model turn is driven. This is the "Esc" landing: the question
overlay clears, the prior turn's output stays in history, and the user resumes
by typing. Approval suspends keep `deny` as their graceful escape.

### Rewind and fork

The two branch verbs, sharing one anchor — the seq of a user-goal
`MessagesAppended` — and differing only in where the new baseline lands.
**Rewind** appends `TaskRewound` to the same stream, so the anchored turn and
everything after it become folded-over dead history (nothing is deleted) and
workspace files that span edited are restored. **Fork** appends `TaskForked` to
a **new** task's stream and writes nothing to the source, so both branches
survive; a fork is a sibling, not a subtask, and it branches the conversation,
not the workspace.

### Guard

A synchronous check at one of three points — `before_tool_call`,
`before_spawn_subtask`, `before_finish` — returning `allow`, `deny` or
`require_approval`. Guards run in ascending `priority` and the first non-allow
verdict decides; a Guard whose `check` raises is converted into a deciding
`deny`, so a buggy Guard can never quietly grant an action.
→ [Guard vs Observer](../concepts/guard-observer.md)

### Observer

An asynchronous hook subscribed to the EventLog. Its failure cannot affect the
task. Callbacks fire post-commit and outside the writer lock, so an Observer
guards its own state and swallows its own exceptions. Observers are read-only —
to change behaviour, change the Policy or the Composer.
→ [Guard vs Observer](../concepts/guard-observer.md)

### Write fence

The path-containment seam the **write** fs tools (`Edit`, `Write`) resolve through: a target must land under the session workspace
or an extra root the host authorized. Containment is component-wise
(`path_within`), never string-prefix, so `/srv/app-old` is not inside
`/srv/app`. **Reads are not fenced**, and the widening resolver
(`HostConfig.write_roots`) fails closed in every degenerate case. This is a
deliberate-mutation boundary, not process confinement — `Bash` reaches the
whole filesystem.

### ExecEnv

The pluggable execution backend the fs and shell tools act through — a deep seam
between the tools and their real IO, operating on already-resolved absolute
paths. `LocalExecEnv` is the host filesystem and subprocess; `AioSandboxExecEnv`
routes every side effect to a container over HTTP. It is injected as a per-tool
construction field and is **never** part of a tool's schema, so the stable prefix
is byte-identical whichever backend is bound. **Not the same as "sandbox"** —
the sandbox is one backend of this seam.
→ [Use a sandbox](../how-to/use-sandbox.md)

### SandboxProvider

The seam that provisions and reaps a per-session container — distinct from
`ExecEnv`, which talks to an already-running one. `allocate` returns a
`SandboxHandle` (addressing plus a live auth strategy that is never serialized),
`release` tears it down at the root task's terminal state, and `attach`
reconnects to a recorded ref on resume. Provisioning belongs to the host, the
mechanism to the runtime, the binding to the SDK — config carries addressing,
never a secret.

### Browser tool pack

The Noeta-owned browser tools (`browser_navigate`, `browser_click`,
`browser_type`, `browser_extract`, `browser_screenshot`) that a sandboxed agent
drives the container's headless browser with. Gated on **both** a live browser
backend and the agent activating `browser`. The names and schemas are Noeta's,
so the stable prefix never depends on the container image. **Not an MCP
connector** — the container's MCP endpoint is an internal transport here.
→ [Built-in tools](tools.md)

## Context and memory

### View

The LLM input the ContextComposer assembles for the Policy. It is a *projection*
of the task, never the task itself.

### ContextComposer

Assembles a task into a View — `compose(task) -> View`. It calls no LLM: a pure
function of folded state plus the ContentStore. The concrete
`ThreeSegmentComposer` is a **closed** extension point, because stable-prefix
cache reproducibility is a hard constraint. The open hooks are registry-only and
append-only: register a `ContentKindSpec` or a compose-time `reminder`.
→ [Composer & cache](../concepts/composer-and-cache.md)

### ContextPlan

The View metadata for one LLM call: which skills and messages were selected,
what was dropped or cleared, what was retrieved. The body is written to the
ContentStore and its ref folds into `ContextState.plan_ref`. It exists for audit
and debug.

### Context segments

The View assembles in three parts. `stable_prefix` carries the system-prompt
message and the provider tool schemas; `semi_stable` carries the content-channel
residents; `dynamic_suffix` carries the rolling history with compose-time
reminders at its tail. Keeping the stable prefix byte-reproducible between steps
is a protocol-level hard constraint — perturbing it blows up the provider KV
cache and sends cost soaring.
→ [Composer & cache](../concepts/composer-and-cache.md)

### Content channel

The generic mechanism by which resident content enters context, in two halves.
**Recording**: a `ContextContentRecorded` event carries kind, name, version,
`content_hash` and drift policy; fold sets `active_content[kind][name]`, hash
last-write-wins, so a re-record with a new hash is a refresh. **Rendering**: one
`ContentKindSpec` per kind, and registration order *is* the semi-stable layout.
Composed bytes are a pure function of folded state plus content store. Tenants
and bands: `skill` (100), `memory` (200), `instructions` (300),
`environment` (400).
→ [Composer & cache](../concepts/composer-and-cache.md)

### Reminder tracks

The three ways authored context text reaches the View, distinguished by when
they run and whether they are recorded. **Track A (`reminder_provider`)** is
recorded and may be impure, running at an intake seam; resume folds its output
back from the ledger instead of re-invoking it. **Track B (`reminder`)** is
compose-time and **pure**, rendered at the tail of the dynamic suffix and never
recorded. **Track C** is the resident content channel. Determinism of a
third-party render in B and C is a documented contract, not enforced.
→ [Plugin surfaces](plugin-surfaces.md)

### Anchored placement

Where a content-channel resident renders, decided by its activation anchor — the
rolling-history length fold records when the activation folds, first-write-wins.
One rule, no per-kind flag: an anchor at or before the first assistant message
renders in the semi-stable segment; a later anchor renders inside the dynamic
suffix at that point, so a mid-task activation appends instead of rewriting the
head. Companion feature: **instructions discovery**, off by default, which
activates the `NOETA.md` / `AGENTS.md` of directories between a read file and
the workspace root.
→ [ADR: Anchored content placement](https://github.com/initxy/noeta/blob/main/docs/adr/anchored-content-placement.md)

### Origin

An optional author marker on a `Message` — `human`, `system` or `memory` —
defaulting to `None`, meaning the role's natural author. Role and origin are
different dimensions: the role says which channel the turn rides, the origin says
who wrote it. **Single-writer guard**: only the Engine's recording path may write
it, and a marker forged in model or tool output is just text. In the SDK message
view, a turn with origin `system` / `memory` projects as `InjectedMessage`, never
`UserMessage`.

### TaskState

Of the four state slices, the one holding long-horizon task memory maintained by
the Policy — goal, phase, todos, decisions, active content. This is the core
difference between a long-horizon agent and a short-task agent. Not to be
confused with [Memory](#memory), which is cross-task.

### Memory

Cross-task long-term memory: file-based and model-managed. Mutation is
`memory_write` and `memory_archive` (retire, never delete); reading is
`memory_read` and `memory_search`. The **resident index** is a content-channel
tenant, so compaction never flushes it, and **auto-recall** is a track-A
provider on the `turn_intake` seam. Recall matches literal tokens (names,
summaries, and the frontmatter `keywords` aliases — the deterministic
cross-lingual bridge); with `Options.recall_model` set, a lexical miss is
retried through one small-model call over the message plus the index (the
**recall judge**), whose picks ride in as pointers and are recorded like any
recall; `memory_write` stamps `created` / `updated` dates and a
`source_task` ledger receipt. Activated by `plugins=("memory", …)`, part
of agent identity — among the official agents only `main` opens it.
→ [Multi-tenant memory](../how-to/multi-tenant-memory.md)

### Memory consolidation

The asynchronous curation pass over the memory store. A reserved-name agent
(`__consolidation__`) runs as an ordinary root task, is fed a digest of recent
activity, and merges duplicates, archives superseded memories, resolves
contradictions between memories, maintains cross-lingual `keywords`, and fills
clear gaps. It is triggered at the host's stop seams behind a debounce marker,
never injected into a live task, and it can only archive, never delete.
→ [query / Client](sdk-client.md)

## Plugins and extension

### Plugin

A manifest-declared contribution package — a pip package or a single local `.py`
file carrying a static manifest that names the plugin, a `requires-noeta` range,
and its contributions to the Surfaces. The manifest is inert data read **without
importing plugin code**: a contribution's `ref` is a string resolved only at the
client-build boundary. Contributions merge deterministically; a collision names
both sides and there is no override.
→ [Plugin manifest](plugin-manifest.md)

### PluginSet

The loaded, host-level set returned by `load_plugins(...)` and passed as
`Client(options, plugins=…)`. Listable and auditable without executing plugin
code: `.contributions()` and `.merged()` read only the static manifests, and
`.resolve()` is the single import boundary, called at client build and never on
a turn.
→ [Plugin manifest](plugin-manifest.md)

### Activation

The per-agent selection of which loaded plugins an agent uses —
`Options.plugins` and `AgentDefinition.plugins`. Activation *is* identity: every
recognised name folds into `AgentSpec.plugins`, and capability gating is a
membership test on that tuple. A name is either a recognised built-in activation
or the name of a plugin in the loaded set; anything else fails compilation
loudly. Effect scope follows the surface — identity surfaces follow activation,
guards and observers do not.
→ [Options](sdk-options.md)

### Surface and SurfaceSpec

A **Surface** is one named extension point; a **SurfaceSpec** describes one
fully — its plane, activation scope, validator, collision key, ordering, and,
for identity-plane surfaces, its activation binding. The loader is
surface-agnostic: it consults one `SurfaceRegistry`, so adding a surface means
registering a SurfaceSpec, not editing the loader. There are sixteen standard
surfaces across three planes.
→ [Plugin surfaces](plugin-surfaces.md)

### Built-in plugin

One of Noeta's own capabilities expressed as a Plugin, in
`noeta/builtins/<name>/`: `__init__.py` holds the zero-execution `MANIFEST`,
`impl/` holds the code. Built-ins ride the identical loader, validation and merge
path as any external plugin, and the band is reached only by dynamic import —
nothing statically imports `noeta.builtins`. There are eighteen. `react` is the
one that refuses to be disabled, because it supplies the default policy identity
every `AgentSpec` pins.
→ [Plugin surfaces](plugin-surfaces.md)

### App plugin

A contribution on a host's own host-plane Surface — routers, channels,
schedules, commands — registered into the surface registry by the host before
load. Validated and collision-checked by the same pipeline, then handed to the
host. Never part of `AgentSpec` identity.

### Session pack

The session-construction half of a capability: a `session_pack` contribution, a
`(SessionBuildContext) -> PackContribution` factory the kernel builder runs in
one priority-ordered loop. The builder enumerates no capability by name. A pack
**self-gates** on its context and returns the empty contribution when it does not
apply, so the kernel holds no `if` for a feature. The built-in bands are locked
by byte-equality goldens, because tool insertion order feeds the stable-prefix
hash.
→ [Plugin surfaces](plugin-surfaces.md)

### SessionBuildContext

The generic frozen context every session pack reads: the containment workspace
root, workspace directory, content store, exec env, model and provider family,
allowed tools, the backend bag, capability flags and plugin config. It is built
before the pack loop, so no pack can perturb a later pack's inputs. It carries
only generic slots — a knob with a single consumer lives in `plugin_config`
under its plugin's name.

### PackContribution

What a session pack hands back: `tools` (merged in loop order, later wins),
`content_kinds` (each with its own registration priority, because layout order
differs from tool order), `init` (the seed-time resident-recording hook), and a
small set of typed side-state fields each read by exactly one kernel seam. All
fields are optional; the empty contribution is the universal "not applicable"
answer.

### Backend bag

The host-populated `backends` mapping on `SessionBuildContext` — live backing
objects keyed by the contributing plugins' own names (`"browser"`,
`"app_preview"`), never the kernel's vocabulary. An absent name means the
capability has no live backing, so the pack returns the empty contribution.

### Control tool mount

The control-tool-construction half of a capability: a `control_tool`
contribution, a `(ControlToolBuildContext) -> ControlToolMount | None` factory
run after tool assembly, because a control-tool schema is a function of the
session state the packs produce. A mount carries `name`, `schema`, `translate`
(a closure over its own build inputs) and two byte-golden-locked priorities —
schema render order and translate dispatch order. A mount self-gates by
returning `None`; mounting *is* enablement.
→ [Plugin surfaces](plugin-surfaces.md)

## Storage and operations

### EventLog

A per-task append-only event stream. **The source of truth for causality and
decisions.** Inline payloads are capped at 4 KB
(`EVENT_PAYLOAD_MAX_BYTES`); larger bodies live in the ContentStore.
→ [Event sourcing](../concepts/event-sourcing.md)

### Event and EventEnvelope

One record on an EventLog stream. The envelope holds `seq` / `type` / `actor` /
`origin` / `trace_id` / `causation_id` — `seq` is assigned by the log on append
— and the payload is a typed dataclass selected by `type`.
→ [Types & testing](sdk-types.md)

### ContentStore

Content-addressed, immutable large-object storage. **The source of truth for
large objects.** Reads come in two shapes: `get` (one ref, raising
`ContentNotFound`) and `get_many` (a batch, missing hashes omitted so one
reclaimed body cannot abort the rest); both are required protocol members.
Because content is immutable, a read cache needs no invalidation rule.
→ [Event sourcing](../concepts/event-sourcing.md)

### ContentRef

A reference into the ContentStore: `hash` + `size` + `media_type`. Lookup is by
`hash` alone.

### Artifact

A large object a Tool produces alongside its inline output, listed as
`ContentRef`s on `ToolResult.artifacts`.

### Snapshot

A `TaskSnapshot` event whose body — the full four-slice task state — lives in
the ContentStore behind `state_ref`, written before each suspend and each
terminal event. It is an acceleration point for fold; a snapshot-free fold
rebuilds the same state.
→ [Fold & snapshot](../concepts/fold-and-snapshot.md)

### Task state slices

Four typed slices, **each with exactly one writer**: `RuntimeState` (messages,
usage — writer: Engine), `TaskState` (goal, phase, todos, decisions, active
content — writer: the Policy's `state_patch`, with `active_content` merged by
fold), `ContextState` (plan ref, compaction summary, per-turn thinking, content
anchors — writer: fold), and `GovernanceState` (cost, token counters, denied
actions, subtask results — writer: fold).
→ [State and writers](../architecture/state-and-writers.md)

### Inspect

Reading a task's history back. `Client.events` and `Client.events_after` return
the raw envelope stream; `Client.messages` folds it into the human-readable
View, dereferencing large bodies through the ContentStore. Pure reads: no
external IO, no effect on the task.
→ [query / Client](sdk-client.md)

## Relationships

- **Task → Subtask** — one-to-many; a subtask has its own EventLog stream,
  related through `parent_task_id`.
- **Agent → Task** — class to instance; one Agent can be instantiated by many
  Tasks.
- **EventLog ↔ ContentStore** — paired; the EventLog holds decisions and refs,
  the ContentStore holds large-object bodies.
- **Engine ↔ Worker** — one-to-many; the same Engine code drives every leased
  task, the Worker wrapping it in the lease and wake loop.
- **Policy ↔ Tool** — the Policy *declares* a call and the Engine *executes* it;
  the Policy never calls the Tool directly.
- **Content channel ↔ Skill / Memory** — mechanism to tenant; adding a tenant
  only requires registering a `ContentKindSpec`.

## Flagged ambiguities

Three words carry a meaning elsewhere that Noeta does not use. The ban is
enforced by `scripts/lint-naming.py`, which fails the build on the class names
`Run`, `Workflow`, `Session`, `Mutator` and `Pattern`, and on the identifiers
`WorkflowRunner`, `WorkflowPolicy`, `WorkflowSpec`, `SessionStore` and
`ConversationManager`.

### Workflow

Not a first-class concept in the engine. Express a fixed procedure as a
deterministic Policy plus `spawn_subtask` decisions. An orchestration script the
model improvises is not a new primitive either: it lands as one Task plus a
Policy that interprets that script, and the assistants it spawns are real
Subtasks. Multi-node sequencing of root tasks is something a host builds on top.

### Session

Not an identity in these libraries. The engine knows only Tasks, and a
multi-turn conversation is one Task receiving user input repeatedly — each
question is one *turn*, each delegation one *Subtask*. A host that groups turns
into a user-visible session owns that concept itself.

The line runs between identity and scope, and only one side is banned.
**Identity is banned**: never name a thing after a session, because the concept
always already has a name — `task_id` for a task, `root_task_id` for the root of
a delegation tree. **Scope is allowed** and is real vocabulary: "for the lifetime
of one root-task tree" is a legitimate thing to say, in prose and in the
session-pack construction vocabulary. A session pack builds one task's tool set
— a scope, not an identity.

### Run

Not a first-class concept. Always use Task.

## Next

- [Concepts](../concepts/index.md) — the same ideas, explained rather than defined
- [SDK reference](sdk.md) — where each term shows up in the API
- [Architecture overview](../architecture/overview.md) — how the pieces fit
