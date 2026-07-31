# Kernel and plugin final form

> **Status: Shipped** — landed on `main` 2026-07-30 (branch
> `kernel-final-form`, tip ec30edb, fast-forwarded; the durable decisions live
> in the unified-context-supply / single-writer-invariant /
> plugin-contribution-bundles ADR addenda). Originally the from-zero target
> design, settled with the owner on
> 2026-07-30 after reviewing an external proposal against the tree. Per the
> owner's explicit directive this spec carries **no historical compatibility**:
> it describes the final form as if built from scratch. Sequencing, migration,
> and byte-compatibility with the current tree are deliberately out of scope.

> **Implementation status (2026-07-30, branch `kernel-final-form`).** Shipped
> green: §4.2 mechanism-slots `SessionContext` (fs knobs → `plugin_config["fs"]`);
> §4.4/§4.5 the scoped `SessionRecorder` + generic `init` hook replacing the three
> feature-named seed recorders, on both the top-level and child-subtask seed
> paths; §3/§6 `active_content` as `kind → {name → hash}` with hash-last-write-wins
> refresh and renderers that `resolve(kind, name) → bytes` from the ContentStore
> (skills excepted — pinned, registry-rendered); §4.3 the stringly export
> vocabulary removed in favour of typed `PackContribution` / `ControlToolMount`
> fields, then completed to *closures-not-fields* (the pack-internal
> pass-throughs and `SessionInputs.skill_registry` are gone; the inspecting
> tests assert through the generic `content_hashes` resolver); §5's "no kit,
> menu, or registry crosses into kernel code" (the skill control tool rides
> `PackContribution.control_tools` as a registry closure; guard inputs travel
> as ONE opaque single-writer `guard_facts` bundle; the goal-intake seam is
> the single generic `intake_reminder_providers`); §2/§9.1 zero feature-named
> kernel modules (`execution.{commands,memory,skills,instructions,environment}`
> and `context.{memory,instructions,environment}` deleted or sunk into their
> built-ins); §9 acceptance property tests
> (`tests/test_acceptance_properties.py`).
> **Deliberate deviations / retained exceptions:** (1) §5's *interleaving* of
> invoke-tools and control schemas onto one priority scale was **not** done —
> the composer already renders both into the stable prefix deterministically
> (invoke by band, then control by `schema_priority`), satisfying §5's
> substantive goals; the remaining interleave is a cosmetic re-ordering that
> would re-pin the tool-schema goldens and burst the KV cache for no model
> benefit (owner decision, 2026-07-30). (2) The **event-vocabulary family**
> stays: `TaskStatePatch.activate_skills` / `TaskState.active_skills` /
> `ContextPlan.selected_skills` and fold's `skill_content_hashes` are recorded
> wire/ledger shapes — changing them is a kernel SPI + recording-compat
> change (§8 locks the event vocabulary), not a plugin refactor. The
> `PermissionPolicy` skill fields (and the `SkillGuardFacts` bundle beside
> them in `noeta.runtime.governance`) are the same family: the guard config
> vocabulary the governance and skills plugins share through the kernel's
> opaque channel.

## 1. Principle

The kernel is a **mechanism container**: it knows events, blobs, fences,
backends, assembly, invocation, and the decision loop — and no feature. Every
capability, including noeta's own, is a plugin. Three laws bind everything:

1. **All durable state flows through events.** No shared mutable objects cross
   a plugin/kernel boundary; what a plugin wants remembered, it records.
2. **All composed bytes are a pure function of (folded state, content store).**
   The ledger fully determines what the model saw. No compose-time callback to
   any external source.
3. **All model-visible order is deterministic.** Every merged collection
   carries an explicit integer priority, ties broken by `(plugin, name)`.
   Order is a pure function of (activation, priorities) — never of load
   timing, dict iteration, or environment.

Laws 2 and 3 are not style: the stable-prefix KV cache prices any
nondeterminism in real money, and audit requires the ledger to be sufficient.

## 2. The kernel: seven mechanisms

| Mechanism | Provides | Knows features? |
| --- | --- | --- |
| EventLog + fold | The single source of truth; generic fold rules | No — generic event types only |
| ContentStore | Content-addressed immutable blobs (`hash + size + media_type`) | No |
| WorkspaceRoot | Path containment (component-wise, resolver-widened) | No |
| ExecEnv | Execution backend (local / container), durably welded per task | No |
| Composer | Three-segment assembly: prefix / semi-stable / dynamic suffix | No — renders kinds it is handed |
| ToolRuntime | guard → invoke → transform → record | No |
| Decision loop | compose → decide → dispatch, suspend/wake, subtask join | No |

The kernel contains **zero** feature-named modules, fields, seams, or events.
There is no per-feature seed recorder, no feature config on any kernel type,
and no stringly export vocabulary. If the kernel must receive a value from a
plugin, that value is a **typed field** on the contribution that carries it.

## 3. Generic event vocabulary and fold rules

| Event | Purpose |
| --- | --- |
| `TaskCreated` / `TaskCompleted` / `TaskFailed` | Lifecycle (writer: Engine) |
| `MessageRecorded` | User/assistant/tool messages, `origin` stamped by the engine's sole origin-writer path |
| `ToolCallStarted` / `ToolResultRecorded` | Invocation trace |
| `ContextContentRecorded` | Resident-content activation/refresh: `kind / name / version / content_hash / policy` |
| `StatePatch` | Policy-owned durable task state (todos, phase, decisions) |
| `SubtaskSpawned` / `SubtaskResult` | Delegation |
| `Snapshot` | fold acceleration point |

Feature state never gets a feature event: it is `ContextContentRecorded`
(discriminated by `kind`) or `StatePatch` (discriminated by key). Fold is a
closed set of generic rules; plugins register no fold handlers.

Fold rules for `ContextContentRecorded` (the one place refresh semantics
live, so every kind gets them for free):

- `active_content[kind][name] = content_hash` — **hash: last-write-wins**. A
  re-record with a new hash is a refresh; with an identical hash it is a
  no-op the recorder already swallowed.
- `content_anchors[(kind, name)]` — **anchor: first-write-wins** at the
  rolling-history length. Refresh moves bytes, never placement.

Single-writer slices are unchanged from the event-sourcing ADRs: Engine owns
runtime/lifecycle events, the Policy owns `StatePatch`, plugins own nothing
except what the scoped recorder (below) lets them say.

## 4. The plugin contract: one manifest + one factory

A plugin is a package carrying a **static manifest** (inert data, readable
without importing code) and **one session factory**. The manifest is the audit
surface; the factory is the construction surface; the kernel cross-checks the
two.

### 4.1 Contribution planes

| Plane | Contributions | Scope | Enters AgentSpec identity? |
| --- | --- | --- | --- |
| **identity** | tool (name/version/schema), translate-tool (name/priorities), content kind (kind/name/version), prompt fragment, agent definition, policy | per-agent (activation) | Yes |
| **session** | the factory: live tool instances, bound renderers, translate closures, reminder providers, result transforms, init hook, discovery hook | per build | No — construction only |
| **process** | guard, observer | process-wide at load | No — governance is operator authority, not agent choice |

Identity is **what is declared**, never what is environmentally present: the
`AgentSpec` is the activation tuple plus the manifest-declared identity
contributions of the activated plugins. A live backend's presence gates
*mounting*, and durable welding (`exec_env_ref` and friends) pins the mounted
set per task — so identity is stable across environments and the tool table
is stable across resume.

### 4.2 SessionContext — mechanism slots only

```python
@dataclass(frozen=True)
class SessionContext:
    workspace: WorkspaceRoot
    workspace_dir: Path
    content_store: ContentStore
    exec_env: ExecEnv | None
    model: str
    provider_family: str | None
    allowed_tools: frozenset[str]
    backends: Mapping[str, object]          # live backings, plugin-named keys
    capability_flags: Mapping[str, bool]    # activation truth, by name
    plugin_config: Mapping[str, Mapping[str, object]]  # each plugin parses its own entry
```

No feature-named field, ever. Write/shell policy, memory roots, skill dirs —
each is its owning plugin's `plugin_config` entry, parsed by that plugin,
failing loudly on what it cannot read. A config key is promoted to a typed
context slot only when a second, unrelated plugin demonstrably consumes it.
There is **no event log on the context**: build-time code records through the
scoped recorder or not at all.

### 4.3 Contribution — typed fields, no exports

```python
@dataclass(frozen=True)
class Contribution:
    tools: Mapping[str, Tool] = ()                       # live invoke-tools
    translate_tools: tuple[TranslateToolMount, ...] = () # schema + translate closure (+ typed codec fields)
    content_kinds: tuple[ContentKindBinding, ...] = ()   # renderer bound to plugin state
    reminder_providers: tuple[ReminderProvider, ...] = ()# track A (recorded, may be impure)
    reminders: tuple[ReminderSpec, ...] = ()             # track B (pure, compose-time)
    tool_result_transforms: tuple[ResultTransform, ...] = ()
    init: InitHook | None = None                         # (SessionRecorder) -> None
    content_discovery: DiscoveryHook | None = None       # (call, result) -> activation payloads
```

- **Closures, not exports.** State shared between a plugin's own parts (a
  store shared by its tools, its renderer, its init hook, its translate
  closure) lives inside the factory closure. A stringly export vocabulary
  does not exist.
- **Typed fields, not a bag.** A value the *kernel* consumes (an answer
  codec on a translate-tool, a discovery hook) is a named, typed field.
  Adding one is an SPI change, reviewed as such — that is a feature, not a
  cost.
- **Self-gating.** A factory that finds itself inapplicable (flag off,
  backend absent, config missing) returns the empty `Contribution`. The
  kernel never gates for a plugin.
- **Manifest cross-check.** Every name a factory returns must be declared in
  the manifest (undeclared ⇒ loud failure at build; declared-but-absent ⇒
  fine, that is self-gating). Contributions stay listable and
  collision-checkable without executing plugin code, while schemas may still
  be materialized at build time.

### 4.4 SessionRecorder — the one scoped write verb

```python
class SessionRecorder(Protocol):
    def record_content(
        self, *, kind: str, name: str, version: str,
        ref: ContentRef, policy: DriftPolicy,
    ) -> None: ...
```

Handed by the kernel to `init` and `content_discovery` hooks. The kernel
stamps the envelope (`actor="plugin:<name>"`, seq, causation); the verb
no-ops when `(kind, name)` is already active with an identical hash and
records a refresh otherwise. This is the **entire** event-writing surface a
plugin gets — no raw `EventLog`. Not a security fence (in-process Python has
none); it is invariant preservation by construction: lifecycle events,
message origin, and slice ownership cannot be corrupted by a buggy plugin,
and the well-typed path is the easy path.

### 4.5 Build and the seams

```
build(agent_spec, ctx):
    contribs = [factory(ctx) for factory in packs_in_priority_order]   # every build, incl. resume
    cross_check(contribs, manifests)
    tool_table    = merge_by_priority(contribs.tools ∪ contribs.translate_tools)
    kinds         = merge_by_priority(contribs.content_kinds)
    recorder      = SessionRecorder(event_log, folded_state)
    for c in contribs: c.init?(recorder)                               # idempotent via no-op gate

run loop (locked):
    view    = composer.compose(folded_state)          # laws 2 & 3
    decision = policy.decide(view)                    # translate-tools rewrite here
    guards.check(decision)                            # process plane
    result  = tool_runtime.invoke(...)                # transforms, then recorded
    discovery hooks → recorder                        # post-tool activations
    fold(new events) → folded_state
```

Named seams, all generic: `init` (every build), `turn_intake`
(reminder providers, output recorded), `pre_tool_call` (guards),
`post_tool_call` (transforms, then discovery), `observe` (observers).

## 5. One model-facing tool namespace

The model sees **one tool list**. Two execution kinds stand behind it:

- **invoke-tools** — external actions: guard → invoke through `ExecEnv` →
  transform → `ToolResultRecorded`.
- **translate-tools** — control surfaces: a schema plus a `translate` closure
  that rewrites the call into a neutral `Decision`
  (`state_patch` / `yield_for_human` / `spawn_subtask(s)` / `finish`). The
  kernel executes decisions; it never knows what a "todo" or a "skill" is.
  A translate-tool needing to hand the kernel a decoder (e.g. an
  answer codec for a human reply) carries it as a typed field on its mount.

One namespace means one priority scale orders the whole provider-facing
table, and the schema of every mounted tool — invoke or translate — is in
the prefix. There is no "model-visible but outside the prefix" position.

Resolution happens in the translate closure, not the kernel: a skill
selection tool's `translate` resolves the chosen skill's body itself and
emits a decision carrying content refs; the kernel records what the decision
says. No kit, menu, or registry crosses into kernel code.

## 6. State flow: record → fold → render

The memory plugin end-to-end (the corrected version of the reviewed sketch):

```python
def memory_factory(ctx: SessionContext) -> Contribution:
    if not ctx.capability_flags.get("memory"):
        return Contribution()
    store = MemoryStore(ctx.plugin_config["memory"]["dir"])

    def init(rec: SessionRecorder) -> None:
        entries = store.entries()                     # impure: legal, output is recorded
        ref = ctx.content_store.put(serialize(entries))
        rec.record_content(kind="memory", name="index", version="1",
                           ref=ref, policy="evolving")  # no-op if hash unchanged

    def render(names, resolve):                       # pure over (state, store)
        if "index" not in names:
            return Rendered.empty()
        return Rendered(render_index(deserialize(resolve("memory", "index"))))

    return Contribution(
        tools={t.name: t for t in build_memory_tools(store)},
        content_kinds=(ContentKindBinding("memory", render, priority=300),),
        reminder_providers=(build_recall_provider(store),),   # track A, recorded
        init=init,
    )
```

- **Activation & bytes**: `init` serializes the index into the ContentStore
  and records its hash. Fold puts the hash in `active_content`; the renderer
  resolves hash → bytes → text. The ledger fully determines the composed
  index (law 2).
- **Refresh**: build reruns on every drive; `init` reruns; the recorder
  no-ops on an unchanged store and records a refresh event otherwise. Hash
  LWW updates the bytes; anchor FWW keeps the placement. Cost: one event +
  one blob per turn *in which the store actually changed*.
- **Replay vs resume**: replay/inspect folds the ledger and touches no
  plugin code. Resume **rebuilds** (live tools are needed) and folds;
  welded backends plus deterministic ordering guarantee the rebuilt table
  and prefix are identical. "Build runs once" is false and not the
  invariant; **"build is idempotent and its recording side effects are
  gated"** is.
- **Purity table**: renderers and track-B reminders are pure over
  (state, store) and rerun every compose; init hooks, track-A providers,
  and tool invokes are impure and their outputs are recorded — replay reads
  the record, never reruns the effect.

## 7. Ordering

Every merged collection — the pack loop, the unified tool table, content-kind
registration, reminder tracks, transform chains — orders by explicit integer
priority, ties by `(plugin, name)`. Built-in plugins pick clean spaced bands
(100, 200, …); third parties slot between. The invariant is locked by
**property tests** (same activation ⇒ byte-identical composed prefix across
process restarts, load orders, and dict-hash seeds), not by legacy goldens.

## 8. Deliberately locked (not extension points)

- The Engine main loop, Dispatcher/Worker/Lease (host config tunes
  concurrency only).
- The Composer's three-segment shape; its open hooks are exactly the
  registry-append surfaces above (kinds, reminders) — wholesale replacement
  is closed because prefix reproducibility is priced in cash.
- Fold: a closed generic rule set; no plugin fold handlers.
- The event vocabulary: growing it is a kernel SPI change, never a plugin
  contribution.
- Governance: guards/observers bind at load, process-wide; activation cannot
  shed them.
- Storage backends (EventLog/ContentStore/Dispatcher) are host wiring, never
  plugin contributions and never identity.

## 9. Acceptance properties (the end state, testable)

1. **No feature in the kernel**: no kernel module references any capability
   name; `grep` for every built-in plugin name over the kernel tree is
   empty (identifiers and string literals both).
2. **Third-party parity**: a test-only external plugin can contribute a
   resident content kind, record it from its init hook, refresh it
   mid-session, mount a translate-tool, and inject a recorded reminder —
   with zero kernel edits.
3. **Ledger sufficiency**: fold a ledger, compose; mutate every plugin's
   backing store on disk; compose again from the same folded state ⇒
   byte-identical output.
4. **Rebuild determinism**: build → drop → rebuild from the same
   (activation, welded refs) ⇒ identical tool table order and identical
   composed prefix.
5. **Recorder idempotence**: running every init hook twice appends zero new
   events when nothing changed, exactly one refresh per changed resident
   otherwise.
6. **Audit without execution**: listing and collision-checking every
   contribution of every installed plugin executes no plugin code; a
   factory returning an undeclared name fails the build loudly.
7. **Governance is inescapable**: an agent whose activation names no
   plugins still passes every loaded guard on every decision.

## 10. Explicitly rejected (from the reviewed proposal and the review)

- **Raw `event_log` on the context or hooks** — replaced by the scoped
  recorder (§4.4). Rejected not for replay (fold never reruns plugins) but
  for slice-ownership-by-construction.
- **"Session build runs once"** — conflates replay with resume; replaced by
  idempotent rebuild (§6).
- **"Stable prefix contains only identity-layer tools"** — incoherent; a
  mounted tool's schema is in the prefix by definition. Replaced by
  identity-from-declaration + per-task welding (§4.1).
- **`StatePatch` as a second channel for resident refresh** — one concern,
  one channel: re-record `ContextContentRecorded`, hash LWW / anchor FWW
  (§3).
- **A stringly export vocabulary** — replaced by closures within a plugin
  and typed fields toward the kernel (§4.3).
- **Feature-named kernel seed recorders and feature config on the build
  context** — dissolved into init hooks and `plugin_config` (§4.2, §4.5).
