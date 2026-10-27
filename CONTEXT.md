# Noeta

A runtime for long-horizon, task-oriented agents: it hosts, records, schedules, and replays agent
execution without prescribing how an agent is written. Package-local rules live in
`packages/noeta-runtime/AGENTS.md` and `packages/noeta-sdk/AGENTS.md`.

## Two libraries

- **noeta-runtime** is the pure kernel: protocols, Engine / fold, worker and lease machinery, the
  composer, the injection-only session builder, the InMemory storage reference. Stdlib only;
  `tests/test_install_smoke.py` pins that closure.
- **noeta-sdk** is the thin client users import (`noeta.sdk`). It ships `noeta.presets` and
  `noeta.builtins` (every official capability) and owns the `httpx` / `psycopg` dependencies.
- Both are in-process libraries: no HTTP/SSE server, no UI, no session model, and no operator CLI
  or console script. A host builds those on top (`examples/reference-host` is the smallest one).
- PyPI dist names are `noeta-runtime` / `noeta-sdk` (bare `noeta` is an unrelated package).

## Public surface

`noeta.sdk` is the only module users import; `noeta.core`, `noeta.runtime`, `noeta.execution`,
`noeta.client`, `noeta.builtins` and the rest are internal. Wiring-only escape hatches on the
public surface: `noeta.storage` (backend SPI + InMemory reference), `noeta.read_models`, and the
lazy doorways `noeta.sdk.providers` / `noeta.sdk.storage`, so only callers that need `httpx` /
`psycopg` pull them in. Concrete sandbox adapters stay off the surface; only the protocols and
factory types are exported. See `docs/adr/011-library-sdk-architecture.md`.

## Microkernel rule

Nothing statically imports `noeta.builtins`; the plugin loader's dynamic `ref` resolution is the
only way in (`.importlinter` enforces it). This also keeps the kernel provider-neutral, since
every adapter lives in a built-in plugin — one directory per capability under
`packages/noeta-sdk/noeta/builtins/`, riding the same loader path as external plugins. The kernel
builder is injection-only and fails loudly when a part is missing; bare-`Options()` defaults come
from resolving built-in manifests via `noeta.client.parts`, never from a static default table.

## Locked vs. open

Open: the plugin Surfaces. Locked: the Engine main loop, the Dispatcher / Worker / lease protocol,
and the `ContextComposer` — replacing it is not allowed because stable-prefix cache
reproducibility is a hard constraint. The composer's only open hooks are append-only
registrations (a `ContentKindSpec`, a compose-time `reminder`), which never touch the stable
prefix. Storage backends go through `HostConfig`, never `Options` or a plugin surface, and
`HostConfig` never enters `AgentSpec` identity.

## Surface

One named extension point; a `SurfaceSpec` gives its plane (identity / wiring / host), activation
scope, validator, collision key and ordering (`packages/noeta-sdk/noeta/client/surfaces.py`). The
loader is surface-agnostic: adding a surface, including a host's own on a copy of
`standard_registry()`, is registering a spec, not editing the loader. The wiring plane has only
two process-wide channels (`guard`, `observer`); any other process-scoped surface is refused.
Host-plane contributions are validated the same way but never enter `AgentSpec` identity.

## Plugin and PluginSet

A Plugin is a manifest-declared contribution package. The manifest is inert data read without
importing plugin code — `ref`s are strings resolved only at client build — so contributions are
listable and collision-checked before any code runs; a collision names both sides, no override.
`load_plugins(...)` returns the host-level `PluginSet`; `.resolve()` is its single import
boundary, called at client build and never on a turn. Workspace plugin dirs are trust-gated.
See `docs/adr/038-plugin-contribution-bundles.md`.

## Activation

Per-agent selection of loaded plugins (`Options.plugins`, `AgentDefinition.plugins`). Activation
is identity: names fold into `AgentSpec.plugins` and gating checks membership via
`agent_activates`; an unknown name fails compilation. Identity surfaces follow activation;
guards and observers do not — once loaded they apply process-wide, because governance is operator
authority. `DEFAULT_PLUGINS = ("fs", "web")` is identity-inert, so a bare `Options()` compiles
byte-identically.

## Session pack

The session-construction half of a capability: a factory `(SessionBuildContext) ->
PackContribution` the kernel builder runs in one priority-ordered loop
(`packages/noeta-runtime/noeta/execution/session_pack.py`). The builder names no capability; a
pack self-gates (missing entry in the `backends` bag, flag off, no config) and returns the empty
contribution — never a kernel `if`. Pack priorities are locked by byte-equality goldens because
tool order feeds the stable-prefix hash. Its `init` hook gets a scoped `SessionRecorder`, the only
write verb a plugin has.

## Control tool mount

The control-tool half of a capability: a factory returning a `ControlToolMount` (or `None` = not
applicable), run after tool assembly because the schema depends on session state
(`packages/noeta-runtime/noeta/execution/control_tool.py`). The mount carries its own `translate`
closure so the runtime translate context stays feature-free. Schema and routing priorities are
locked by goldens. See `docs/adr/039-control-tool-contributions-and-activation-identity.md`.

## Task

One execution instance of an agent; the only first-class runtime entity. States: `pending` /
`running` / `suspended` / `terminal`. Every wait — subtask, approval, timer, external event, next
user goal — is the same `suspended` state and the same resume path. A conversation is one Task
receiving goals repeatedly. A **turn** opens with a user goal (or background notice) and runs
through every approval, answer and subtask return that resumes it, until the Task parks for the
next goal or ends. A **Subtask** is a Task spawned by a parent, linked only by
`parent_task_id` (always delegation) and `subtask_depth`. **`root_task_id`** names a delegation
tree and owns its tree-scoped resources: background shells, background subagents, the sandbox.
_Avoid_: Run, Workflow Instance, Session

## Agent

A named, spawnable configuration (`AgentSpec`, `packages/noeta-runtime/noeta/agent/spec.py`): the
"class" of a Task, not a runtime entity. Identity only, no callables, normalized so author
ordering does not change equality; a different policy, plugin set or tool set is a different
agent. Compiled from `Options` (`noeta.sdk.Options`), the only way to express both the official
agents in `noeta.presets` and custom ones. A child's `description` is required: it is rendered
into the `Task` tool schema so the model knows whom to delegate to.

## Decision and Policy

A Policy decides the next step: `decide(ctx, view) -> Decision` (LLM, FSM, or hybrid), supplied
as an `(llm) -> Policy` factory whose `.ref` enters identity; the default `("react", "1")` is why
the `react` built-in cannot be disabled. Decisions are neutral mechanism variants
(`packages/noeta-runtime/noeta/protocols/decisions.py`). Product control tools never get a kernel
variant — they translate to `state_patch` or `yield_for_human` inside their built-in, and the
Engine assigns no meaning to a `state_patch` payload.
See `docs/adr/004-control-tools-neutral-mechanism.md`.
_Avoid_: Pattern

## Tool

An external action the model can invoke (`packages/noeta-runtime/noeta/protocols/tool.py`).
`name` / `input_schema` / `description` are hand-written and LLM-facing, never taken from
docstrings (which would leak internal names). `description` is the single source of model-visible
tool semantics and is never repeated in the system prompt. `risk_level` gates approval. Optional
`getattr` attributes: `advertised`, `route_call` (deferred MCP), `concurrency_safe` (consecutive
safe calls run in parallel, recorded in call order). See `docs/adr/026-tool-description-canonical.md`.

## Provider

An adapter implementing an internal Protocol (`LLMProvider`, storage protocols). LLM adapters are
the `providers` built-in, imported via `noeta.sdk.providers`. Storage is host config: first-party
durable backends via `noeta.sdk.storage`; a third-party backend implements the storage Protocols
plus `noeta.storage.spi`, no plugin needed. A provider is never a context source: content enters
context only by being recorded as an event.

## EventLog

Append-only, one stream per task: the source of truth for causality and decisions. State is always
derived by folding it, never read back from the Dispatcher. Inline payloads are capped
(`EVENT_PAYLOAD_MAX_BYTES`); larger bodies go to the ContentStore. Appends require a valid lease
(single-writer invariant) except the explicit lease-free `system_emit` records.
See `docs/adr/009-event-sourced-truth.md` and `docs/adr/019-single-writer-invariant.md`.

## ContentStore

Content-addressed, immutable large-object storage; lookup by hash. Retention is host-driven only:
`Client.collect_garbage` (`packages/noeta-runtime/noeta/storage/gc.py`) marks referenced hashes and
sweeps the rest. LLM request bodies are not stored unless `HostConfig.record_llm_requests`.
Third-party backends: see `docs/adr/059-content-retention-outside-the-protocol.md`.

## Single-writer state slices

Four slices, each with exactly one writer (`packages/noeta-runtime/noeta/protocols/task.py`):
`RuntimeState` (Engine), `TaskState` (the Policy's `state_patch`; the long-horizon task memory),
`ContextState` (fold), `GovernanceState` (fold). Hooks, guards and observers may not mutate state
or payloads; to change behavior, change the Policy or the composer.

## Engine

Advances one Task to its next suspend or terminal (`run_one_step`); depends on neither Worker nor
Dispatcher, and hosts reach it through `EngineProtocol`. **An Engine is a per-turn value, never a
cached resource**; state that must cross turns lives in the host's `TaskLocalRegistry`. Details in
`packages/noeta-runtime/AGENTS.md` (Engine per turn).

## Worker, Lease, Dispatcher

A Worker leases a Task, drives it to the next suspend or terminal, and releases.
`run_leased_task` (`packages/noeta-runtime/noeta/runtime/worker.py`) is the one resume machine
every surface shares. `reserved=True` makes a Task targeted-lease-only so a worker pool cannot
claim it before its driver seeds the opening message. A row's **Queue** decides which pool may
claim it: fixed at birth, inherited by children, no wildcard — routing state, not identity.
A worker daemon must never crash on one poisoned task. See `docs/adr/029-worker-lease-model.md` and
`docs/adr/041-worker-queue-routing.md`.

## ExecEnv

The backend fs/shell tools do IO through (`packages/noeta-runtime/noeta/runtime/exec_env.py`), on
already-resolved absolute paths — containment stays with `WorkspaceRoot`, so no backend can widen
the fence. It is a construction field, never part of a tool schema, so the stable prefix does not
depend on it; credentials never enter the record. Sandbox wiring: `packages/noeta-sdk/AGENTS.md`.
See `docs/adr/033-execution-environment-seam.md`.

## View and stable prefix

The composer turns a Task into a View; it calls no LLM and is a pure function of folded state plus
the ContentStore (no clock, randomness, or network). The View has three segments:
`stable_prefix` (system prompt + tool schemas), `semi_stable` (residents), `dynamic_suffix`
(history, compose-time reminders at the tail). The stable prefix must serialize byte-identically
across steps or the provider KV cache breaks and cost multiplies. Anything per-step belongs in the
suffix or a recorded event. See `docs/adr/028-unified-context-supply.md`.

## Content Channel

The mechanism for resident context (skills, memory index, instructions, environment). A
`ContextContentRecorded` event sets `active_content[kind][name] = hash`; one `ContentKindSpec`
per kind renders it, and registration order is the semi-stable layout
(`packages/noeta-runtime/noeta/context/content_channel.py`). Bytes come only from the ContentStore
by recorded hash. Red line: never reach a live external source at compose time — fetch on the
write side and record it. Per-process captures (clock, git) record activate-once.

## Reminder tracks

- **A, `reminder_provider`**: runs at an intake seam (`turn_intake`, `task_seed`); may be impure
  because its output is recorded, and replay never re-invokes it. A raise fails the turn.
- **B, `reminder`**: a pure render of folded state at the suffix tail, never recorded. A *placed*
  reminder keeps its position and bytes once it appears, so it caches like history.
- **C**: Content Channel residents.
Determinism of third-party renders (B, C) is a documented contract, not enforced.

## origin

An optional author marker on a `Message` (`human` / `system` / `memory`), orthogonal to `role`.
Only `Engine.append_user_message` may set it; Policy-supplied markers are stripped and forged
markers in model or tool output are just text. Vendor syntax (`<system-reminder>`, an in-band
preamble) is added by provider adapters at serialization and never enters the ledger.
See `docs/adr/008-event-origin-marker.md`.

## Budget and Guard

`Budget` (`packages/noeta-runtime/noeta/runtime/governance.py`) is pure config; the `governance`
built-in enforces it against folded `GovernanceState`, so live and resumed runs agree. A Guard
checks before a tool call, spawn or finish and returns `allow` / `deny` / `require_approval`.
Approval reuses `yield_for_human`; there is no separate approval decision. `HookManager`
(`packages/noeta-runtime/noeta/core/hooks.py`) takes the first non-allow verdict, and a Guard that
raises counts as `deny`, so a buggy guard never grants.

## Write fence

`WorkspaceRoot` (`packages/noeta-runtime/noeta/runtime/workspace.py`) contains the write tools
(`Edit` / `Write`): the canonical target must lie under the workspace or a host-authorized extra
root, compared component-wise (`path_within`), never by string prefix. Reads are not fenced.
`HostConfig.write_roots` is a per-call resolver (no tool rebuild, no prefix change) and fails
closed. Not process confinement: `Bash` can write anywhere; the container is the isolation
boundary. See `docs/adr/037-workspace-write-authorization.md`.

## Observer

An EventLog subscriber whose failure never affects the Task. Callbacks run post-commit, outside
the writer lock, but synchronously before the emit returns — never block in one — and possibly
from several writer threads at once: each observer guards its own state and swallows its own
exceptions (contract: `EventLogSubscriber` in `packages/noeta-runtime/noeta/protocols/event_log.py`). Observers are live-only, never part of fold or resume.
`ChildLifecycleObserver` carries the parent/child handoff and is always installed.

## Human stops and branches

`cancel` → `TaskCancelled` (terminal); `close` → `ConversationClosed`; `interrupt` →
`TurnInterrupted`, stopping only the in-flight turn so the Task parks for the next goal.
Interrupted events stay as history. **Rewind**
appends `TaskRewound` to the same stream (nothing deleted) and restores edited files. **Fork**
writes `TaskForked` into a new root task and leaves the source untouched; it is a sibling, not a
subtask, and shares the source's workspace on disk. Both anchor on a user-goal message seq.
See `docs/adr/005-conversation-rewind-and-file-checkpoint.md`.

## Naming: Workflow

Not a first-class concept. A fixed procedure is a deterministic Policy plus spawn decisions; a
model-improvised orchestration script is one Task plus a Policy interpreting it, whose spawned
assistants are real Subtasks. Multi-node sequencing is something a host builds on top.
_Avoid_: `Workflow`, `WorkflowRunner`, `WorkflowPolicy`, `WorkflowSpec`

## Naming: Session is a scope, never an identity

No session identity in these libraries: no `session_id`, no session list or catalog, no
session-keyed cap, no session field in a recorded event. Use `task_id`, or `root_task_id` for a
delegation tree; a task id named `session_id` is the exact defect this rule prevents. Scope wording
is allowed — "per-session workspace", and the construction vocabulary `session_pack` /
`SessionBuildContext` / `SessionPackEntry` / `SessionPackFactory` / `SessionRecorder` /
`SessionInputs` / `build_session_inputs`. A host may own a session concept.
_Avoid_: `SessionStore`, `ConversationManager`, `class Session`

## Naming: Run, Mutator, Pattern

"Run" is not a concept — say Task. "Mutator" is not a role: hooks may not modify context or
payloads (single-writer invariant). "Pattern" is not a name for a Policy.
_Avoid_: `class Run`, `class Mutator`, `class Pattern`

## Naming: model-visible tool names

Provider-visible tool names follow Claude Code (`Read`, `Glob`, `Grep`, `Edit`, `Write`, `Bash`,
`BashOutput`, `KillShell`, `Monitor`, `WebFetch`, `WebSearch`, `TodoWrite`, `AskUserQuestion`,
`Task`), because models carry their strongest prior on those names and schemas. Everything not
model-visible stays snake_case: plugin and contribution names, capability flags (`todo_write`,
`ask_user_question`, `delegation`), event vocabulary, description resource basenames. Prose says
"the Task tool" when it means the delegation control tool, not the Task primitive.

## Naming: enforcement

`scripts/lint-naming.py` (in `make check`) scans `.py` / `.md` / `.toml` / `.yaml` / `.yml` /
`.cfg` / `.ini` and rejects `class Run` / `Workflow` / `Session` / `Mutator` / `Pattern` and the
identifiers `WorkflowRunner` / `WorkflowPolicy` / `WorkflowSpec` / `SessionStore` /
`ConversationManager`. Inside `packages/` and `examples/` it also rejects compound identifiers
containing "session" outside the scope allow-list (plus `start_new_session`) or `session_pack`.
`tests/` is out of scope. Files that catalogue the bans (`CONTEXT.md`, `AGENTS.md`, `docs/adr/`,
`CHANGELOG.md`, the script and its test) are exempt.
