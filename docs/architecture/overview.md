# Architecture overview

A top-down tour of Noeta: how the two packages stack, how the event-sourced
core shapes each layer, and where the extension surfaces sit. For "what is X"
questions this page links to the [concept pages](../concepts/event-sourcing.md);
for exact API signatures see the [reference pages](../reference/sdk.md).

## The two packages

Noeta ships as two libraries: a thin client over a pure engine.

| Package | Location | Role |
| --- | --- | --- |
| `noeta-runtime` | `packages/noeta-runtime` | The pure kernel: `protocols` (the only typed boundary), `core` (Engine, fold, snapshot), the kernel services (Worker, Dispatcher, tool runtime, the in-memory reference storage, observers, read models), the material mechanisms (`context` — the locked composer and registries; `policies` — the control band; `tools` — authoring machinery), the injection-only `execution` builder, and the `agent` identity layer. Carries no capability implementation and no HTTP client. |
| `noeta-sdk` | `packages/noeta-sdk` | The one public surface — `query` / `Client` / `Options` / `@tool`, the re-exported extension interfaces, and the preset agents — plus `builtins`, the catalogue where every official capability actually lives. |

<p align="center">
  <img src="../assets/architecture.svg" alt="Noeta architecture — the two distributions and module relationships" width="820">
  <br>
  <em>A host drives the SDK in-process; the SDK forwards into the runtime's engine, materials, and storage. Arrows are call paths.</em>
</p>

Both packages contribute subpackages to one shared PEP 420 `noeta.` namespace,
so import paths stay put even when the distribution boundary shifts. The
dependency direction is not left to discipline — import-linter enforces it in
CI (`.importlinter`): the kernel may not import a provider or backend adapter,
and `noeta-sdk` forwards into the runtime in-process. Users import `noeta.sdk`
alone; `noeta-runtime` arrives as a transitive dependency they never touch.

## The kernel carries no capability

Every official capability — the fs and web tool packs, provider adapters,
guards, memory, browser, app, MCP, sandbox backends, skills, and the ReAct
policy — is a **built-in plugin** under
`packages/noeta-sdk/noeta/builtins/<name>/`. A built-in declares a
zero-execution `MANIFEST` in its `__init__.py` and keeps its code under `impl/`;
nothing statically imports `noeta.builtins`. The only doorway is the plugin
loader's dynamic `ref` resolution, and `.importlinter` rejects any static
import — which is also what keeps the kernel from ever reaching a vendor
adapter, since all adapters live in `noeta.builtins`.

The catalogue holds eighteen built-ins: `fs`, `web`, `memory`, `browser`,
`app`, `mcp`, `skills`, `react`, `reminders`, `governance`, `providers`,
`sandbox`, `presets`, `workspace`, `storage`, `todo_write`,
`ask_user_question`, `delegation`.

## Ground truth: state = fold(log)

A Task's ground truth is its append-only `EventLog`; state at any moment is
computed by folding that log, never stored as a first-class copy. The concept
and its consequences are covered in
[Event sourcing](../concepts/event-sourcing.md) and
[Fold & snapshot](../concepts/fold-and-snapshot.md). Two architecture-level
mechanisms make the promise hold in practice.

### Four state slices, one writer each

If anything mutated state without an event, fold's rebuild would stop matching
what ran. Task state is therefore cut into four typed slices, each with exactly
one writer (`packages/noeta-runtime/noeta/protocols/task.py`):

| Slice | Sole writer | Holds |
| --- | --- | --- |
| `RuntimeState` | Engine | the rolling conversation-message stream, per-turn usage, last transition, last input-token count |
| `TaskState` | Policy — only via a `TaskStatePatch` in a Decision | todos, decision records, activated skills |
| `ContextState` | the Composer | context plan ref, compaction summary, stripped-off thinking |
| `GovernanceState` | fold, accumulated from events | cost, iteration count, token counts, subtask results |

The telling cell is `TaskState`: the Policy cannot assign to its own
long-horizon memory. It attaches a `TaskStatePatch` to the Decision it returns;
the Engine lands that as an event; fold writes it back. Envelopes also carry an
`origin` marker recording which role wrote them (`engine`, `llm`, `tool`,
`observer`, `system`), and a message the Policy synthesizes has its origin
scrubbed (`strip_message_origin`) before entering the stream, so it cannot
impersonate another writer.

### Folding old recordings across versions

Event payloads and state slices evolve, but a Task suspended long ago must still
fold under current code. The canonical rendering layer (see
[Fold & snapshot](../concepts/fold-and-snapshot.md)) carries this with two
symmetric rules:

- **Adding a field must not break old recordings.** A new field is appended at
  the end of its slice, given a default, and omitted from the byte stream when
  empty — so an old recording (which never had the field) and current code
  (folding it to the default) stay byte-equal.
- **Removing a field must not crash old snapshots.** When restoring a snapshot,
  keys the current version no longer recognizes are filtered out rather than
  passed to a constructor that would reject them.

One rule guarantees "the same present folds to the same bytes"; the other
tolerates "a past written by a different version." Where a snapshot predates
required fields entirely, fold discards it and replays from the top — slower,
never wrong.

## The execution stack

### Engine

The Engine advances one Task by one step —
[compose → decide → dispatch](../concepts/engine-execution.md) — and knows
nothing of Workers, the Dispatcher, or HTTP. Its control flow only routes
Decisions; the actual work — emitting envelopes, running tools, spawning
subtasks — is delegated to peripheral handlers, keeping the class body small
and legible.

### Worker, Dispatcher, Lease

The Dispatcher owns scheduling: task enqueue, Lease granting, wake delivery, and
stale reclamation. A Worker drives the loop:

1. `dispatcher.lease(worker_id=…)` returns a
   `Lease(lease_id, task_id, expires_at, wake_event=None)` — an exclusive,
   heartbeat-renewed hold on one Task.
2. The Worker folds the `EventLog` into a `RuntimeState`.
3. If `lease.wake_event` is set, the Worker calls `engine.note_woken(…)`, which
   writes a durable `TaskWoken` envelope.
4. The Worker calls `engine.run_one_step(task, lease_id=…)`, which advances the
   Task to its next suspend or terminal — looping internally over `tool_calls`
   decisions, so one call covers a whole turn rather than a single model
   round-trip.
5. The Worker calls `dispatcher.release(lease_id, next_state=…, wake_on=…)` — or
   `dispatcher.fail(…)` on an unexpected exception.

The single-writer invariant is enforced here mechanically: the `EventLog`
consults the Dispatcher (as `LeaseRegistry`) on every `emit(lease_id=…)`, so
only the holder of an active Lease can write to a Task's stream. Observers see
each envelope synchronously after it commits, on the writer thread but outside
the writer lock, with exceptions swallowed.

The drain loop ships as a library primitive,
`noeta.runtime.worker.WorkerLoop` — nothing launches it for you; an embedding
host calls `WorkerLoop(…).run_forever(…)` itself (see the
[WorkerLoop reference](../reference/worker-loop.md)).

### Durable wake

[Wake & resume](../concepts/wake-resume.md) states the guarantee — durable
exactly-once delivery. The mechanism:

- The Dispatcher matches an incoming wake event to a suspended Task by
  projection and holds the match durably. Delivery happens at lease time via
  `Lease.wake_event`.
- The Worker threads the wake into `engine.note_woken`, which writes
  `TaskWoken(wake_event=…)` before the step continues. This write is the
  durability commit point.
- The match **survives the lease**: it is cleared only by a consuming
  `release(consumed_wake_event=…)`. A Worker crash between lease and the
  `TaskWoken` write leaves the wake in place; `requeue_stale()` returns the Task
  to ready, and the next lease re-delivers the same wake.
- Consumption is idempotent. The Worker's woken branch is a recovery state
  machine keyed on the latest matching `TaskWoken` envelope: a re-delivery whose
  `TaskWoken` already landed is reconciled without emitting a second one.
- A resume attempt on a suspended Task with no queued wake reports a typed
  `suspended_without_wake_event` — a diagnostic meaning "waiting for something
  that has not happened yet," not a fault.

The guarantee holds across concurrent Workers: every lease-checked append is
fenced, so a stalled Worker whose lease was reclaimed cannot land a write behind
the new generation. Single-host multi-worker runs on every backend; multi-host
runs on Postgres, where the fence is an in-transaction `FOR SHARE` row check
evaluated against the database clock (removing per-host clock skew from the
decision). SQLite and in-memory are single-host.

A crash mid-step recovers on the next lease: the interrupted attempt is sealed
and re-driven automatically when side-effect-free, or the Task is parked for a
human. The recovery scope, the SQLite boundary, and the one open edge (sandbox
side effects are unfenced across Worker generations) are catalogued in
[known limitations](../operations/limitations.md).

## Context assembly

Per step, the `ThreeSegmentComposer` assembles the model's View from folded
state in three segments ordered by volatility (`stable_prefix`, `semi_stable`,
`dynamic_suffix`), keeping the prefix byte-stable for provider KV-cache reuse;
compaction is a recorded event rather than an in-place edit. The design is
covered in [Composer & cache](../concepts/composer-and-cache.md). One accuracy
detail belongs here: whether compaction should trigger is judged against the
real input-token count the provider reported for the previous step (folded into
`RuntimeState.last_input_tokens`), with only the newly appended messages
estimated — a character-count heuristic systematically undercounts prompts that
carry caching, structured blocks, or images.

## Provider boundary

The Engine speaks a neutral internal protocol; vendor adapters translate at the
edge, fold vendor errors into a neutral taxonomy (transient / context-overflow /
fatal), and keep wire-only mechanics such as cache breakpoints out of the
ledger. Three adapters ship as the `providers` built-in: an Anthropic adapter,
an OpenAI `/chat/completions` adapter for any compatible gateway, and an OpenAI
Responses adapter. The kernel-may-not-import-a-provider rule makes the boundary
structural. See [Provider neutrality](../concepts/provider-neutrality.md).

## The SDK surface

`noeta.sdk` is the thin client: build one `Options`, then drive an agent
in-process with `query` (one turn) or `Client` (multi-turn). The load-bearing
design is a single cut through the `Options` fields:

- **Identity fields** decide how the agent thinks — system prompt, skills, tool
  set, activated plugins, a custom Policy. They enter the recording and are
  reproduced verbatim on fold.
- **Wiring fields** only mount the agent onto a host — the provider instance,
  the working directory, an approval callback, observers. They are excluded from
  identity (`compare=False`), so swapping them does not perturb the recording.

The cut is mandatory because recordings must be reproducible: mix the two and a
recording fails to line up because a working directory changed.

What is open to extend, all `Options` fields re-exported through `noeta.sdk`:

| Field | Extends |
| --- | --- |
| `policy` | swap the ReAct brain for your own decision function (carrying a `.ref` so identity stays deterministic) |
| `guards` | synchronous checks before an effect (see [Guard vs Observer](../concepts/guard-observer.md)) |
| `observers` | read-only event subscribers — audit, metrics |
| `content_channels` | register a `ContentKindSpec` to place custom resident content into the semi-stable segment |
| `mcp_servers` | in-process SDK MCP tools, or connectors to external stdio / HTTP MCP servers |
| `@tool` | stamp a function with name, version, risk level, and input schema to make it a first-class tool |

What stays locked: the Engine main loop, the Dispatcher / Worker / Lease
machinery (a host tunes concurrency and lease timing only), and the
`ThreeSegmentComposer` — replacing the composer wholesale is not on the user
surface because stable-prefix reproducibility is a hard constraint; its only
open hook is the content channel. Storage backends are wired through
`HostConfig`, not `Options`, and never enter agent identity; the public doorway
for that wiring is `noeta.sdk.storage`.

An agent gets the full built-in tool set (11 tools) unless narrowed by
`allowed_tools` / `disallowed_tools`, and `permission_mode`
(`default` / `acceptEdits` / `bypassPermissions`) decides whether high-risk
tools ask first. Exact signatures live in the
[SDK reference](../reference/sdk.md).

## The plugin surfaces

Beneath the `Options` fields, the plugin loader consults one `SurfaceRegistry`
and nothing else, so adding an extension surface means registering a
`SurfaceSpec`, never editing the loader. The standard catalogue holds sixteen
surfaces across three planes:

- **Identity** (enters durable agent identity): `tool`, `agent`,
  `content_kind`, `prompt_fragment`, `policy`, `control_tool`.
- **Wiring** (mounts onto a host, out of identity): `guard`, `observer`,
  `provider`, `reminder_provider`, `reminder`, `tool_result_transform`,
  `session_pack`.
- **Host** (host-wired resources): `mcp_server`, `skills`, `sandbox_provider`.

A host extends the set by taking a `copy()` of `standard_registry()` and
registering its own surfaces before calling `load_plugins`. See
[the plugins reference](../reference/plugins.md).

## The agent layer

An agent's identity is an `AgentSpec` — a name plus the identity-side
configuration (instructions, policy ref, tools, the `plugins` activation tuple,
`spawnable` roster) — compiled from `Options` and collected in a registry. The
identity layer sits low in the runtime, depending only on the protocol layer.

Four presets ship with deliberately trimmed surfaces:

| Preset | Role | Tool surface | Delegates? |
| --- | --- | --- | --- |
| `main` | the conversational controller | full builtins + `todo_write` / `ask_user_question` / `skill_invocation` / `memory` / `mcp` | yes |
| `general-purpose` | a self-contained coding worker | read / write / edit + shell + web | no — a leaf |
| `explore` | a read-only scout | read-only tools only | no |
| `plan` | a read-only planner | read-only tools only | no — produces a plan |

The trimming knife is **activation**: explicit switches written into agent
identity as the `AgentSpec.plugins` tuple plus `spawnable`, not runtime
restrictions bolted on. Feature gating reads that tuple through
`agent_activates` — membership *is* the capability.

Cooperation takes two shapes. **Single delegation**: the parent spawns one
Subtask, suspends, and wakes when it completes. **Fan-out**: the parent spawns a
group of Subtasks that run concurrently on a bounded in-process thread pool
(capped at 8 or the CPU count, whichever is smaller), and the results flow back
together — each result returns via a wake event and is paired to the original
tool call. Each Subtask is a full event-sourced Task with its own log and fold,
related to its parent only by `parent_task_id`; more elaborate orchestration is
expressed as one Task whose Policy interprets a model-written orchestration
script, not as a new primitive.

## Distribution

Because ground truth is "fold over a durable log," distribution is mostly a
scheduling problem: any process that can read the store can rebuild any Task by
folding, and execution assumes nothing about which machine it is on. The default
shape is single-host — a local SQLite file and a resident `WorkerLoop` pool in
one process. Reaching a multi-host cluster is a storage-adapter swap: point the
deployment at Postgres and several host processes share one database, their
writes fenced as described above. The Engine does not change either way.

Cancellation follows the same cooperative design as the Engine's stop probes:
cancelling marks the Task; Worker and Engine stop at the next safe point; the
cascade cancels in-flight Subtasks; background shell processes are registered
and reaped when their session closes.

## Where to go next

- Concepts: [Event sourcing](../concepts/event-sourcing.md) ·
  [Fold & snapshot](../concepts/fold-and-snapshot.md) ·
  [Engine & execution](../concepts/engine-execution.md) ·
  [Wake & resume](../concepts/wake-resume.md)
- Reference: [SDK](../reference/sdk.md) ·
  [WorkerLoop](../reference/worker-loop.md) ·
  [plugins](../reference/plugins.md) ·
  [comparison with the Claude Agent SDK](../reference/comparison.md)
- Decision records: [`docs/adr/`](https://github.com/initxy/noeta/tree/main/docs/adr) — the rationale behind each cross-module decision.
