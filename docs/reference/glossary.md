# Glossary

Canonical vocabulary for Noeta. Each term has a single, stable meaning across
the codebase and the docs. The authoritative source is
[`CONTEXT.md`](https://github.com/initxy/noeta/blob/main/CONTEXT.md) in the
repository root; this page is its published form.

## Core abstractions

### Task

One execution instance of an agent. It can spawn subtasks, suspend on a wake
condition, and resume. The only first-class citizen in the system. The `Task`
object is small — `task_id`, `status`, four state slices; its history and
snapshots live in the EventLog and the ContentStore.

See also: [Task model](/concepts/task-model), [ADR: Task as the only primitive](https://github.com/initxy/noeta/blob/main/docs/adr/task-as-only-primitive.md)

### Subtask

A task spawned from a parent task by a `spawn_subtask` decision. Structurally
identical to a parent task, related only through `parent_task_id` (plus
`subtask_depth`, the delegation depth the recursion budget caps).

See also: [ADR: Subtask fan-out and durable wake](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-fanout-and-durable-wake.md)

### Agent

A named, spawnable configuration — an `AgentSpec` carrying instructions, the
policy and composer refs, tools, skills, budget, activated plugins, and the
names it may spawn. **Not a runtime entity**: the "class" of a task, identity
only, normalized to sorted tuples so two specs differing only in author
ordering compare equal. A child agent's `description` is required and non-blank
— it is rendered into the `spawn_subagent` control-tool schema so the model
knows who to hand work to.

See also: [Presets](presets.md), [ADR: Tool and agent catalog](https://github.com/initxy/noeta/blob/main/docs/adr/tool-and-agent-catalog.md)

### Options

The declarative agent configuration (`noeta.sdk.Options`), compiled by
`compile_options` into an `AgentSpec` plus the flat tuple of descendant specs a
`Client` registers. The sole way to express both the official agents and custom
ones; compilation is pure and additive. Identity-bearing fields include
`system_prompt`, `name`, `agents`, `allowed_tools`, `disallowed_tools`,
`permission_mode`, `max_turns`, `skills`, `plugins`, `budget`, `policy`, and
`mcp_servers` (an in-process server's tools join the agent's tool set).
Wiring fields — `model`, `provider`, `cwd`, `can_use_tool`, `metadata`,
`guards`, `observers`, `content_channels` — are excluded from identity and from
equality.

`allowed_tools` is a **replacement** allowlist: setting it means *only* those
tools. Omitting it (`None`) yields the full built-in set of 11 tools (`read`,
`glob`, `grep`, `edit`, `write`, `apply_patch`, `shell_run`, `shell_poll`,
`shell_kill`, `webfetch`, `web_search`); `()` means no tools. Control tools are
not in that set — they mount on activation.

See also: [SDK API](/reference/sdk)

### Step

The slice by which a task advances within one Engine main-loop pass:
`compose → decide → dispatch`. A `tool_calls` decision keeps the loop turning in
place (append results, recompose, ask again); any other decision ends the step
at a suspend or a terminal.

### Attempt

One decide→act iteration inside a Step. Its first durable record is
`ContextPlanComposed`, and it is the unit of crash recovery: a
`StepAttemptAbandoned` marker seals an interrupted attempt as folded-over dead
history.

See also: [ADR: Step-attempt recovery](https://github.com/initxy/noeta/blob/main/docs/adr/step-attempt-recovery.md)

### Decision

The return value of `Policy.decide` and the input to Engine dispatch. A set of
neutral mechanism variants: `tool_calls`, `spawn_subtask`, `spawn_subtasks`,
`yield_for_human`, `wait_timer`, `wait_external`, `state_patch`,
`compaction_requested`, `finish`, `fail`. Product control tools get no kernel
variant of their own — `todo_write` and `skill` are expressed as `state_patch`,
`ask_user_question` as `yield_for_human`, by translate bodies that live in the
contributing built-in plugins.

See also: [Engine & execution](/concepts/engine-execution), [ADR: Control tools as neutral mechanism](https://github.com/initxy/noeta/blob/main/docs/adr/control-tools-neutral-mechanism.md)

### Policy

The function that decides the next step given the current View
(`decide(ctx, view) -> Decision`). It can be a pure LLM (`ReActPolicy`), a pure
state machine, or a hybrid. Every compiled `AgentSpec` pins a policy identity;
the default is `("react", "1")`, and the `policy` surface is how a different
brain is substituted.

See also: [ADR: Engine-policy dataflow](https://github.com/initxy/noeta/blob/main/docs/adr/engine-policy-dataflow.md)

### Tool

An external action the agent can invoke. The structured-contract trio
`name` / `input_schema` / `description` is hand-written and LLM-facing;
`description` is the single source of truth for the model-visible semantics and
is never repeated in the system prompt. A tool also carries `version` and
`risk_level` (which gates approval). **Not a Skill** — that is a separate
concept.

See also: [Tools](tools.md), [ADR: Tool description canonical](https://github.com/initxy/noeta/blob/main/docs/adr/tool-description-canonical.md)

### Provider

A Noeta-shape adapter for an external service; each service kind implements the
matching internal protocol. `LLMProvider` is open through `Options.provider`
and re-exported from `noeta.sdk`; the adapters and model catalog live in the
`providers` built-in, reached through `noeta.sdk.providers`. Storage backends
are configured through host config, never through Options. **Not a context
content source** — content enters context only by event recording plus
assembly rendering.

See also: [Provider neutrality](/concepts/provider-neutrality), [ADR: Provider-neutral](https://github.com/initxy/noeta/blob/main/docs/adr/provider-neutral.md)

### Skill

A local, static LLM-workflow template at `.noeta/skills/<name>/SKILL.md`,
optionally with resource files. Three-tier merge, low to high: built-in, then
global `~/.noeta/skills`, then workspace. Two-stage on-demand loading — the
*menu* (name plus one-line summary) is rendered into the `skill` control-tool
schema; once the model selects one, its body is rendered into the semi-stable
segment, which is exempt from compaction. Accompanying resources are reached by
progressive disclosure: the renderer prepends
`Base directory for this skill: <dir>` and the model reads files on demand with
the generic `read` tool. **Not a Tool.**

See also: [ADR: Model-driven skill invocation](https://github.com/initxy/noeta/blob/main/docs/adr/model-driven-skill-invocation.md), [ADR: Skill resource on-demand](https://github.com/initxy/noeta/blob/main/docs/adr/skill-resource-on-demand.md)

### Plugin

A manifest-declared contribution package — a pip package or a single local
`.py` file carrying a static manifest that names the plugin, a
`requires-noeta` range, and its contributions to the Surfaces. The manifest is
inert data read **without importing plugin code**: a contribution's `ref` is a
string resolved only at the client-build boundary, so contributions are
listable and collision-checkable before anything runs. Contributions merge
deterministically by `(plugin, name)`; a collision names both sides and there is
no override.

See also: [Plugins](plugins.md), [ADR: Plugin contribution bundles](https://github.com/initxy/noeta/blob/main/docs/adr/plugin-contribution-bundles.md)

### PluginSet

The loaded, host-level set of plugins returned by `load_plugins(...)` and
passed as `Client(options, plugins=...)`. Listable and auditable without
executing plugin code: `.contributions()` and `.merged()` read only the static
manifests, and `.resolve()` is the single import boundary, called at client
build and never on a turn.

### Activation

The per-agent selection of which loaded plugins an agent uses —
`Options.plugins` and `AgentDefinition.plugins`. Activation *is* identity: every
recognised name folds into `AgentSpec.plugins`, and gating reads that tuple
through `agent_activates(agent, plugin)`. A name is either a recognised
built-in activation or the name of a plugin in the loaded PluginSet; anything
else fails compilation loudly. The identity-bearing built-in activation names
are `memory`, `browser`, `skill_invocation`, `mcp`, `todo_write`,
`ask_user_question`, and `delegation`. `DEFAULT_PLUGINS` is `("fs", "web")` —
identity-inert, so a bare `Options()` compiles byte-identically. Effect scope
follows the surface: identity surfaces follow activation; guards and observers
do not — once loaded they are in force process-wide.

### Surface / SurfaceSpec

A **Surface** is one named extension point. A **SurfaceSpec** describes one
fully: its `plane`, `activation_scope`, `validator`, `collision_key`,
`ordering`, and — for identity-plane surfaces — `activation_binding`. The
loader is surface-agnostic: it consults one `SurfaceRegistry`, so adding a
surface means registering a SurfaceSpec, not editing the loader.

Sixteen standard surfaces across three planes:

| Plane | Surfaces |
| --- | --- |
| identity | `tool`, `agent`, `content_kind`, `prompt_fragment`, `policy`, `control_tool` |
| wiring | `guard`, `observer`, `provider`, `reminder_provider`, `reminder`, `tool_result_transform`, `session_pack` |
| host | `mcp_server`, `skills`, `sandbox_provider` |

The wiring plane has exactly two process-wide channels — `guard` and
`observer`. A process-scoped surface beyond those is refused, not filed under
one of them.

### Built-in plugin

One of Noeta's own capabilities expressed as a Plugin, living in
`noeta/builtins/<name>/`: `__init__.py` holds the zero-execution `MANIFEST`,
`impl/` holds the code, and the manifest's `ref`s point at the sibling impl
modules. Built-ins ride the identical loader, validation, and merge path as any
external plugin, and the band is reached only by dynamic import — nothing
statically imports `noeta.builtins`.

The 18 built-ins: `fs`, `web`, `memory`, `browser`, `app`, `mcp`, `skills`,
`react`, `reminders`, `governance`, `providers`, `sandbox`, `presets`,
`workspace`, `storage`, `todo_write`, `ask_user_question`, `delegation`.

`react` is the one built-in that refuses to be disabled: it supplies the
default decision policy, whose identity every compiled `AgentSpec` pins. The
default brain is replaceable through the `policy` surface, not removable.

### App Plugin

A contribution on a host's own host-plane Surface — routers, channels,
schedules, commands — registered into the surface registry by the host before
load. Validated and collision-checked by the same pipeline, then handed to the
host. Never part of `AgentSpec` identity.

### Session pack

The session-construction half of a capability: a plugin's `session_pack`
contribution, a factory `(SessionBuildContext) -> PackContribution` that the
kernel builder runs in one priority-ordered loop. The builder enumerates no
capability by name. A pack **self-gates** on its context — backend absent, flag
off, no config — and returns the empty `PackContribution` when it does not
apply, so the kernel never holds an `if` for a feature. Built-in bands are
locked by byte-equality goldens, because tool-dict insertion order feeds the
stable-prefix hash.

### SessionBuildContext

The generic frozen context every session pack reads: the containment
`WorkspaceRoot`, `workspace_dir`, content store, exec env, model and provider
family, `allowed_tools`, the backend bag, `capability_flags`, and
`plugin_config`. Built before the pack loop, so no pack can perturb a later
pack's inputs. It carries only generic slots — no feature-named field; a knob
with a single consumer lives in `plugin_config` under its plugin's name.

### PackContribution

What a session pack hands back: `tools` (a `name → Tool` mapping merged in loop
order, later-wins), `content_kinds` (each with its own registration priority,
because semi-stable layout order differs from tool order), `init` (the seed-time
resident-recording hook), and a small set of typed side-state fields each read
by exactly one kernel seam — `control_tools`, `guard_facts`,
`content_discovery`, `content_preloader`. The builder never reads inside the
opaque ones. All fields are optional; `EMPTY_CONTRIBUTION` is the universal
"not applicable" answer.

### Backend bag

The host-populated `backends` mapping on `SessionBuildContext` — live backing
objects keyed by the contributing plugins' own names (`"browser"`,
`"app_preview"`), never the kernel's vocabulary. An absent name means the
capability has no live backing, so the pack returns the empty contribution.

### Control tool mount

The control-tool-construction half of a capability: a plugin's `control_tool`
contribution, a factory `(ControlToolBuildContext) -> ControlToolMount | None`
run in the builder's post-tools phase, because a control-tool schema is a
function of the session state the packs produce. A mount carries `name`,
`schema` (the materialized provider-facing dict), `translate` (a closure over
its own build inputs, so the runtime translate context stays feature-free), and
two byte-golden-locked priorities: `schema_priority` for render order and
`routing_priority` for translate dispatch order. A mount self-gates by
returning `None`; the kernel never gates for it. `translate=None` means the
tool is intercepted outside the dispatch loop and contributes a schema only —
`structured_output` is the one such mount, intercepted by the ReAct policy. The
`ask_user_question` mount additionally fills `answer_codec`, so the driver's
`answer` path can decode a submitted reply without importing the built-in.

See also: [ADR: Control-tool contributions and activation identity](https://github.com/initxy/noeta/blob/main/docs/adr/control-tool-contributions-and-activation-identity.md)

## State and events

### EventLog

A per-task append-only event stream. **The source of truth for causality and
decisions.** Inline payloads are capped at 4 KB (`EVENT_PAYLOAD_MAX_BYTES`);
larger bodies live in the ContentStore.

See also: [Event sourcing](/concepts/event-sourcing), [ADR: Event-sourced truth](https://github.com/initxy/noeta/blob/main/docs/adr/event-sourced-truth.md)

### Event / EventEnvelope

One record on an EventLog stream. The envelope holds `seq` / `type` / `actor` /
`origin` / `trace_id` / `causation_id` (`seq` assigned by the log on append);
the payload is a typed dataclass selected by `type`.

### ContentStore

Content-addressed, immutable large-object storage. **The source of truth for
large objects.** Anything over the 4 KB event-payload cap goes here and the
envelope carries only a `ContentRef`. Reads come in two shapes: `get` (one ref,
raises `ContentNotFound`) and `get_many` (a batch, missing hashes omitted so
one reclaimed body cannot abort the rest). Both are required protocol members.
Because content is immutable, a read cache needs no invalidation rule —
`CachedContentStore` wraps the durable backends at stack build.

See also: [ADR: Storage protocols L0](https://github.com/initxy/noeta/blob/main/docs/adr/storage-protocols-l0.md)

### ContentRef

A reference into the ContentStore: `hash` + `size` + `media_type`. Lookup is by
`hash` alone.

### Artifact

A large object a Tool produces alongside its inline output, listed as
`ContentRef`s on `ToolResult.artifacts`.

### Snapshot

A `TaskSnapshot` event whose body — the full four-slice task state — lives in
the ContentStore behind `state_ref`, written before each suspend and each
terminal event. An acceleration point for fold; a snapshot-free fold rebuilds
the same state.

See also: [Fold & snapshot](/concepts/fold-and-snapshot)

### Task State (four slices)

Four typed slices, each with exactly one writer:

- **RuntimeState** — messages, usage (writer: Engine)
- **TaskState** — goal, phase, todos, decisions, `active_content` (writer: the
  Policy's `state_patch`; `active_content` is the exception, merged by fold
  from `ContextContentRecorded` events, hash last-write-wins)
- **ContextState** — plan ref, compaction summary, per-turn thinking, content
  anchors (writer: fold)
- **GovernanceState** — cost, token counters, denied actions, subtask results
  (writer: fold)

### TaskState (narrow sense)

Of the four slices, the one holding long-horizon task memory maintained by the
Policy. This is the core difference between a long-horizon agent and a
short-task agent. Not to be confused with Memory, which is cross-task.

## Execution model

### Engine

Advances a single Task by one step, where a step is a turn boundary:
`run_one_step` loops internally over `tool_calls` decisions and returns at the
next suspend or terminal state. It depends on neither the Worker nor the
Dispatcher. The main loop is **locked** — not an extension point; host config
can only tune concurrency and lease parameters. Do not confuse the Engine class
with the `noeta-runtime` wheel.

See also: [Engine & execution](/concepts/engine-execution)

### Worker

The process that leases a Task from the Dispatcher and calls the Engine to
advance it. **One lease runs until the next suspend or terminal state, then
releases.** The drain loop ships as `noeta.runtime.worker.WorkerLoop`; nothing
launches it for you.

See also: [WorkerLoop](worker-loop.md), [ADR: Worker lease model](https://github.com/initxy/noeta/blob/main/docs/adr/worker-lease-model.md)

### Lease

A Worker's short-term exclusive hold on a Task — `lease_id`, `task_id`,
`expires_at` — extended by `heartbeat` and reclaimed by `requeue_stale` once
expired. The Worker presents `lease_id` on every EventLog emit, which is how the
single-writer invariant is enforced.

See also: [ADR: Single-writer invariant](https://github.com/initxy/noeta/blob/main/docs/adr/single-writer-invariant.md)

### Dispatcher

The scheduling component: task enqueue, lease granting, wake-event delivery,
and stale reclamation. Task state itself is never read back off the Dispatcher
— production code folds the EventLog, the single source of truth.

See also: [Wake & resume](/concepts/wake-resume)

### Suspended

One of a Task's four states (`pending`, `running`, `suspended`, `terminal`),
parked on a wake condition. A unified expression of waiting on a subtask, an
approval, a timer, or an external event — one state and one resume path whatever
the reason.

### WakeCondition / WakeEvent

What a Task is waiting on: `SubtaskCompleted`, `SubtaskGroupCompleted`,
`HumanResponseReceived`, `TimerFired`, `ExternalEvent`. Condition and event
share one dataclass per variant; matching is by identity-field projection
through `matches_wake`, and every Dispatcher routes through that helper so no
adapter can diverge privately.

See also: [Wake & resume](/concepts/wake-resume), [ADR: Subtask fan-out and durable wake](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-fanout-and-durable-wake.md)

### ExecEnv

The pluggable execution backend the fs and shell tools act through — a deep
seam between the tools and their real IO (file read/write/create/unlink/mkdir/
stat/glob plus `run_argv`), operating on already-resolved absolute paths.
`LocalExecEnv` is the host filesystem and subprocess; `AioSandboxExecEnv`
routes every side effect to a sandbox container over HTTP. It is injected as a
per-tool construction field, **never** part of a tool's schema, so the stable
prefix is byte-identical whichever backend is bound. In sandbox mode the seam
also carries the skill indexer, `run_skill_script`, the workspace loaders, and
web fetch/search egress; memory and MCP stay on the host. A session's container
is welded durably through `TaskHostBound.exec_env_ref` so a resumed session
reconnects to the same one; the credential rides only on the wire, never in the
log. **Not the same as "sandbox"** — the sandbox is one backend of this seam.

See also: [ADR: Execution environment seam](https://github.com/initxy/noeta/blob/main/docs/adr/execution-environment-seam.md)

### SandboxProvider

The seam that provisions and reaps a per-session sandbox container — distinct
from `ExecEnv`, which talks to an already-running one. `allocate(root_task_id,
spec)` returns a `SandboxHandle` (addressing plus a live `SandboxAuth` strategy
that is never serialized), `release` tears it down at the root task's terminal
state, and `attach` reconnects to a recorded ref on resume or reclaim. The SDK
defines the protocol and drives it through `SandboxExecEnvManager`; a host
implements it. Provisioning belongs to the host, the mechanism to the runtime,
the binding to the SDK — config carries addressing, never a secret.

### Browser tool pack

The Noeta-owned browser tools — `browser_navigate`, `browser_click`,
`browser_type`, `browser_extract`, `browser_screenshot` — that a sandbox
session's agent drives the container's headless browser with. Like the fs pack
it is a per-session tool pack injected by construction field, gated on **both**
a live browser backend in the backend bag **and** the agent activating
`browser`. The model-facing names and schemas are Noeta's; the implementation
delegates through a narrow `BrowserBackend` seam. **Not an MCP connector** — the
container's MCP endpoint is an internal transport here, not a model-facing
connector.

## Context

### View

The LLM input the ContextComposer assembles for the Policy. **Not equal to the
Task** — a projection of it.

### ContextComposer

Assembles a Task into a View — `compose(task) -> View`. It calls no LLM: a pure
function of folded state plus the ContentStore. The concrete
`ThreeSegmentComposer` is a **closed** extension point on the user surface:
replacing the composer wholesale is not open, because stable-prefix KV-cache
reproducibility is a hard constraint. Internally it is protocol-injected — the
Engine imports only the protocol. The open hooks are registry-only and
append-only: registering a `ContentKindSpec` or a compose-time `reminder`.

See also: [Composer & cache](/concepts/composer-and-cache), [ADR: Unified context supply](https://github.com/initxy/noeta/blob/main/docs/adr/unified-context-supply.md)

### ContextPlan

The View metadata for one LLM call: which skills and messages were selected,
what was dropped or cleared, what was retrieved. The body is written to the
ContentStore and its ref folds into `ContextState.plan_ref`; it exists for audit
and debug.

### Stable Prefix / Semi-stable / Dynamic Suffix

The three segment names in the View's assembly. `stable_prefix` carries the
system-prompt message and the provider tool schemas; `semi_stable` carries the
content-channel residents; `dynamic_suffix` carries the rolling history with the
compose-time reminders at its tail. The cache-friendliness of the stable prefix
is a protocol-level hard constraint: perturbing it between steps blows up the
provider KV cache and sends cost soaring, so it must serialize reproducibly —
its hash is `sha256(to_canonical_bytes((stable_content, provider_tool_schemas)))`
with sorted keys, and a tool description enters the schema only when non-empty.

See also: [ADR: Context compaction](https://github.com/initxy/noeta/blob/main/docs/adr/context-compaction.md)

### Content Channel

The generic mechanism by which resident content enters context, made of two
parts. **Event recording**: `ContextContentRecorded` carries kind, name,
version, `content_hash`, and drift policy; fold records
`active_content[kind][name] = content_hash`, hash last-write-wins, so a
re-record with a new hash is a refresh and an identical hash a no-op.
**Assembly rendering**: the `ContentChannelRegistry` renders each kind into the
semi-stable segment through one `ContentKindSpec` per material kind, and
registration order is the layout order. The recorded hash is load-bearing — a
renderer receives a `resolve(kind, name)` that derefs the resident's active hash
through the ContentStore, so composed bytes are a pure function of folded state
plus content store. Registering a `ContentKindSpec` on the `content_kind`
surface is the open extension hook. Tenants and their bands: `skill` (pinned,
100), `memory` (evolving, 200), `instructions` (evolving, 300), `environment`
(evolving, 400); host and third-party kinds register after every built-in. The
red line: a provider may only record on the write side — reaching out to a live
external source at compose time is forbidden.

### Reminder tracks (A / B / C)

The three distinct ways authored context text reaches the View, each a Surface,
distinguished by when it runs and whether it is recorded.

- **Track A — `reminder_provider` (recorded, impure).** A provider at a named
  intake seam (`turn_intake`, `task_seed`) that, given a narrow read-only
  `RecallView`, returns zero or more `Reminder(text, origin)`. It may query an
  external system *because its output is recorded* through the Engine's sole
  origin-writer seam; resume folds the reminder back from the ledger and never
  re-invokes the provider. A raise fails the turn loudly.
- **Track B — `reminder` (compose-time, pure).** A `(name, priority, render)`
  spec where `render` is a pure function of a narrow folded-state projection
  returning `str | None`, rendered at the tail of the dynamic suffix, never
  recorded, re-derived on every compose — so the stable prefix is untouched by
  construction. The three built-ins are `unfinished-todos` (100),
  `delegation-nudge` (200), `read-suggestion` (300).
- **Track C — the resident Content Channel.** A pure renderer for resident
  content in the semi-stable segment, its activation recorded as a
  `ContextContentRecorded` event.

Determinism of a third-party `render` in tracks B and C is a documented
contract, not enforced.

### Anchored Placement

Where a content-channel resident renders, decided by its activation anchor —
the rolling-history length fold records when the activation folds,
first-write-wins. One rule, no per-kind flag: an anchor at or before the first
assistant message renders the resident in the semi-stable segment; a later
anchor renders it inside the dynamic suffix at that anchor, so a mid-task
activation appends instead of rewriting the head. The insertion index slides
past `role="tool"` messages so a tool round-trip is never split, and an anchor
covered by a compaction summary clamps to the slot right after the summary
message.

Companion feature: **instructions discovery** (`instructions_discovery`,
default off) — after a successful `read` inside the workspace, the runtime
activates the not-yet-active `NOETA.md` / `AGENTS.md` of each directory between
the read file and the workspace root. Scope is fenced to the workspace, because
write authorization is not instruction authority.

See also: [ADR: Anchored content placement](https://github.com/initxy/noeta/blob/main/docs/adr/anchored-content-placement.md)

### origin

An optional author marker on a `Message` — `human`, `system`, or `memory` —
defaulting to `None`, meaning the role's natural author. **Single-writer
guard**: only the Engine's recording path (`Engine.append_user_message`) may
write it; a Policy-supplied message has it stripped at the Decision seams, and a
marker forged in model or tool output is just text. Role is a different
dimension; do not conflate them.

See also: [ADR: Event origin marker](https://github.com/initxy/noeta/blob/main/docs/adr/event-origin-marker.md)

### Memory

Cross-task long-term memory, file-based and model-managed. Mutation is the
ordinary tools `memory_write` (one markdown file per memory, optional
frontmatter `description` / `type`) and `memory_archive` (move into `archive/`
— retire, never delete). On-demand reading is `memory_read` (full text by name)
and `memory_search` (case-insensitive substring over names and bodies,
excerpt-only output). The **resident index** is a content-channel tenant
(`kind="memory"`, policy `evolving`), living in the semi-stable segment so
compaction does not flush it. **Auto-recall** is a track-A
`reminder_provider` on the `turn_intake` seam: it reads the store live — legal
because its output is recorded — matches in two tiers (name tokens, then
summary tokens), and records hits as one `origin="memory"` follow-up. The
**policy** is `MEMORY_POLICY_PROMPT`, contributed by the `memory` built-in on
the `prompt_fragment` surface.

Activated by `plugins=("memory", …)`, which is part of behavior-affecting agent
identity — of the four official agents only `main` opens it. The store root
resolves through one chain for every consumer: the per-task
`HostConfig.memory_root_resolver` when it resolves, else `memory_dir`, then
`global_memory_dir`, then `~/.noeta/memories`.

See also: [Multi-tenant memory](/how-to/multi-tenant-memory)

### Memory consolidation

The asynchronous curation pass over the memory store: a reserved-name agent
(`__consolidation__`, whose tool surface is the memory pack alone) runs as an
ordinary root task on the resident worker pool, is fed a digest of recent
session activity, and merges duplicates, archives superseded memories, and
fills clear gaps. Triggered at the host's stop seams behind a debounce marker in
the memory root. The toggle is host configuration, not agent identity. It never
injects into a live session and can only archive, never delete. A multi-tenant
host runs one pass per tenant.

See also: [ADR: Memory consolidation](https://github.com/initxy/noeta/blob/main/docs/adr/memory-consolidation.md)

## Governance

### Principal

Who is acting, and which models that identity may bind
(`noeta.protocols.values.Principal`): an `identity` string plus an
`allowed_models` set, with `allows_any=True` modelling the unbounded ⊤ set.
`permits(selector)` is the principal half of model selection — the driver
validates `selector ∈ principal.allowed_models ∩` the deployment allowlist
before any `ModelBound` is emitted. The Principal itself never enters an event
payload; only the `principal_identity` string rides `ModelBound`, as the durable
audit link from a binding back to who sanctioned it. `LOCAL_PRINCIPAL`
(`identity="local"`, `allows_any=True`) is the ⊤ principal for an embedding with
no trust boundary.

### Contract

A Task's immutable header, frozen into the genesis `TaskCreated` event and never
rewritten: `goal`, `policy_name`, `agent_name`, `inputs`, `parent_task_id`,
`subtask_depth`. Every fold bootstraps empty state from it. A goal too large for
the 4 KB payload cap spills to the ContentStore (`goal` holds `""`, the text is
reachable through `goal_ref`). A delegated subtask's expected-output schema
travels as `inputs["output_schema"]`.

### Budget

A Task's resource ceilings, declared as a `BudgetSpec` and enforced by
`BudgetGuard`: `max_iterations`, `max_tool_calls`, `max_cost_usd`,
`max_spawned_subtasks`, `max_subtask_depth`. `None` on a field means no cap on
that dimension. Which caps apply depends on the action: a tool call checks every
cap, a spawn skips `max_tool_calls`, and a finish checks only the historical
accumulators `iterations` and `cost_usd`.

### Guard

A synchronous hook that runs at three points — `before_tool_call`,
`before_spawn_subtask`, `before_finish` — returning `allow`, `deny`, or
`require_approval`. `require_approval` is carried by `yield_for_human`; there is
no separate approval event type. Guards run in ascending `priority` and the
first non-allow verdict decides; a Guard whose `check` raises is converted into
a deciding `deny`, so a buggy Guard can never quietly grant an action. The
`governance` built-in contributes the default stack: `PermissionGuard`,
`BudgetGuard`, `RepetitionGuard`, `HookGuard`.

See also: [Guard vs Observer](/concepts/guard-observer), [ADR: Guard-observer hooks](https://github.com/initxy/noeta/blob/main/docs/adr/guard-observer-hooks.md)

### Write fence (`WorkspaceRoot`)

The path-containment seam the **write** fs tools (`edit`, `write`,
`apply_patch`) resolve through: a target is canonicalised and must land under
the session workspace, or under one of the extra roots the host has authorized.
Containment is component-wise (`path_within`, published on `noeta.sdk`), never
string-prefix — `/srv/app-old` is not inside `/srv/app`. **Reads are not
fenced**: `read`, `grep`, and `glob` anchor a relative path to the workspace and
take an absolute one wherever it points. The widening is a resolver, not a fixed
tuple — `HostConfig.write_roots` maps `task_id` to extra writable directories
and is consulted per call, so an authorization granted while a task sits paused
takes effect on the resumed call without rebuilding the tool set. It fails
closed in every degenerate case. This is a deliberate-mutation boundary, not
process confinement: `shell_run` reaches the whole filesystem through a
subprocess.

See also: [ADR: Workspace write authorization](https://github.com/initxy/noeta/blob/main/docs/adr/workspace-write-authorization.md)

### Observer

An asynchronous hook subscribed to the EventLog through
`EventLogSubscriber.subscribe`; its failure does not affect the Task. Callbacks
fire post-commit and outside the writer lock, so an Observer guards its own
state and swallows its own exceptions. Observers are read-only — they may not
modify context or payload. To change behaviour, change the Policy or the
Composer.

See also: [Guard vs Observer](/concepts/guard-observer)

## Operations

### Inspect

Reading a Task's history back. `Client.events` / `Client.events_after` return
the raw envelope stream; `Client.messages` folds it into the human-readable
View, dereferencing large bodies through the ContentStore. Pure reads of the
EventLog plus ContentStore: no external IO, no effect on the Task.

### Resume

Continuing a suspended Task — fold its stream back into state, drive it forward
under a lease (`run_leased_task`, the one resume machine every hosting surface
shares), append what happens. The trigger is a wake event matching the Task's
`wake_on`; the human-side entries are the driver verbs `send_goal`,
`approve` / `deny`, `answer`, `deliver_event`. State comes only from the fold,
so a resumed turn never re-invokes what the ledger already records.

See also: [Troubleshooting](/operations/troubleshooting)

### Rewind / Fork

The two branch verbs. They share one **anchor** — the seq of a user-goal
`MessagesAppended` — and one fold-through boundary, the turn boundary just
before that message. They differ only in where the resulting baseline lands.

**Rewind** appends a `TaskRewound` to the same stream, so the anchored turn and
everything after it become folded-over dead history (nothing is deleted —
append-only holds), and the workspace files that span edited are restored from
their recorded baselines. **Fork** appends a `TaskForked` to a **new** task's
stream and writes nothing to the source, so both branches survive: "undo this"
versus "try this instead, keeping the original". A fork is a sibling, not a
Subtask — `parent_task_id` means delegation, so it stays `None` and lineage
lives in `TaskForked.source_task_id`; only a root Task can be forked. A fork
branches the conversation, not the workspace: both branches keep the source's
`workspace_dir` and act on the same disk, which is why fork has no file-restore
half.

See also: [ADR: Conversation rewind and file checkpoint](https://github.com/initxy/noeta/blob/main/docs/adr/conversation-rewind-and-file-checkpoint.md)

### Interrupt

The third of the three human stops. `cancel` writes `TaskCancelled` and the
conversation is terminal; `close` writes `ConversationClosed` and archives it;
**`interrupt`** writes `TurnInterrupted` and stops only the in-flight turn,
leaving the Task resting at its next-goal suspend, reopenable by simply typing
again. It rides the cooperative-cancel poll the Engine runs at turn boundaries,
so its granularity **is** the turn boundary: it lands before the next tool call
or model round and cannot abort a tool call already executing. Not a rewind —
the interrupted turn's events stay on the stream as real history, and the two
verbs compose: interrupt to stop, rewind to un-say. The resulting
`TaskSuspended.reason` is `"interrupted"`.

## Relationships

- **Task → Subtask** — one-to-many; a subtask has its own EventLog stream,
  related through `parent_task_id`.
- **Agent → Task** — class to instance; one Agent can be instantiated by many
  Tasks.
- **EventLog ↔ ContentStore** — paired; the EventLog holds decisions and refs,
  the ContentStore holds large-object bodies.
- **Engine ↔ Worker** — one-to-many; the same Engine code drives every leased
  Task, the Worker wrapping it in the lease / wake loop.
- **Policy ↔ Tool** — the Policy *declares* a call via `ToolCallsDecision.calls`
  and the Engine *executes* it; the Policy never calls the Tool directly.
- **Content Channel ↔ Skill / Memory** — mechanism to tenant; adding a tenant
  only requires registering a `ContentKindSpec`.

## Flagged ambiguities

Three words carry a meaning elsewhere that Noeta does not use. They are enforced
by `scripts/lint-naming.py`, which fails the build on the class names `Run`,
`Workflow`, `Session`, `Mutator`, and `Pattern`, and on the identifiers
`WorkflowRunner`, `WorkflowPolicy`, `WorkflowSpec`, `SessionStore`, and
`ConversationManager`.

### "Workflow"

Not a first-class concept in the engine. Express fixed procedures with a
deterministic Policy plus `spawn_subtask`. An orchestration script the model
improvises is not a new primitive either: it lands as one Task plus a Policy
that interprets that script, and the assistants it spawns are real Subtasks.
Multi-node sequencing of root tasks is something a host builds on top of the
libraries.

### "Session"

Not an identity in these libraries. The engine knows only Tasks, and a
multi-turn conversation is one Task receiving user input repeatedly: each
question is one **turn**, each delegation one **Subtask**. A host that groups
turns into a user-visible session owns that concept itself.

The line runs between identity and scope, and only one side is banned.
**Identity is banned**: never name a thing after a session — the concept always
already has a name, `task_id` for a task and `root_task_id` for the root of a
delegation tree. **Scope is allowed**, and is real vocabulary: "for the lifetime
of one root-task tree" is a legitimate thing to say, in prose and in the
session-pack construction vocabulary (`session_pack`, `SessionBuildContext`,
`SessionPackEntry`, `SessionPackFactory`, `SessionRecorder`, `SessionInputs`,
`build_session_inputs`). A session pack builds one task's tool set — a scope,
not an identity.

### "Run"

Not a first-class concept. Always use Task.
