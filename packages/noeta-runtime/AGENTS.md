# noeta-runtime

The pure kernel (import names `noeta.*` except `noeta.sdk`, `noeta.client`, `noeta.builtins`,
`noeta.presets`). Root `CONTEXT.md` holds the cross-package vocabulary; this file holds rules that
matter only when editing the kernel.

## Injection-only builder

`packages/noeta-runtime/noeta/execution/builder.py` assembles a session from injections only
(packs, control tools, guards, reminders, provider facts). A missing required part fails loudly;
never add a fallback default or a capability-named branch here. Feature knobs with one consumer
live in `plugin_config` under that plugin's name and get a typed `SessionBuildContext` slot only
once a second, unrelated plugin reads them.

## PackContribution side-state

Besides `tools` (merged later-wins) and `content_kinds` (their own registration priority, because
semi-stable layout order differs from tool order), a `PackContribution` carries typed side-state
fields each read by exactly one kernel seam and single-writer across the pack loop. Adding one is
an SPI change. See `packages/noeta-runtime/noeta/execution/session_pack.py`.

## Step and Attempt

A Step is one Engine pass `compose -> decide -> dispatch`; `tool_calls` loops in place, any other
decision ends it. An Attempt is one decide/act iteration and the unit of crash recovery:
`ContextPlanComposed` opens it. An interrupted one whose tool batch has started is kept and finished
(no started call runs again); otherwise `StepAttemptAbandoned` seals it as folded-over dead
history. See `docs/adr/032-step-attempt-recovery.md`, refined by
`docs/adr/065-crash-recovery-keeps-finished-tool-calls.md`.

## Contract

The genesis `TaskCreated` payload is the Task's immutable header (goal, policy, agent, inputs,
parent, depth); every fold bootstraps from it and it is never rewritten. An oversized goal spills
to the ContentStore via `goal_ref`. A subtask's expected-output schema travels as
`inputs["output_schema"]`.

## Wake matching

Condition and delivered event share one dataclass per variant. Matching is by identity-field
projection through `matches_wake` (`packages/noeta-runtime/noeta/protocols/wake.py`); every
Dispatcher implementation must route through it so no adapter diverges. Payload fields ride along
and never affect matching.

## Lease and wake delivery

The EventLog backend checks only `LeaseRegistry.is_lease_valid`, deliberately narrow and
side-effect-free so it can run inside a write lock. A lease carries its matched `wake_event`, which
is not consumed at lease time: it is cleared only by a consuming `release` after the durable
`TaskWoken` is written. A crash in between re-delivers it — at-least-once delivery plus idempotent
consumption gives exactly-once. Do not "simplify" by consuming at lease time.
See `packages/noeta-runtime/noeta/protocols/dispatcher.py`.

## Resume outcomes

`run_leased_task` has three outcomes: `woken` (lease carried a wake event), `drained` (pending or
running), `skipped` (still suspended with no wake; re-released preserving `wake_on`). `WorkerLoop`
adds heartbeats, stale and timer sweeps, and a bounded graceful shutdown that abandons the
in-flight step after the grace period and lets the lease expire. A resumed turn takes state only
from fold and never re-invokes what the ledger already records.

## Engine per turn

Nothing may outlive a turn on the Engine object. Per-task state that must cross turns (files read
for the edit tools' read-first precondition, built-in `task_slot` data such as compaction baselines
and caches) goes in `TaskLocalRegistry`, keyed by task id and forgotten at cancel / close. The
build is deterministic, so the stable prefix moves only when an input changed.
See `docs/adr/046-engine-per-turn.md`.

## Snapshot

`TaskSnapshot` is written before each suspend and terminal; its body is the full state behind
`state_ref`. It only accelerates fold: a snapshot-free fold must produce the same state, and fold
falls back to replay when a body was reclaimed by GC. The re-base bodies behind `TaskRewound` /
`StepAttemptAbandoned` / `TaskForked` are never reclaimed.
See `packages/noeta-runtime/noeta/core/snapshot.py`.

## Composer side effect

The composer's only side effect is putting the `ContextPlan` body into the injected ContentStore;
`ContextState.plan_ref` is fold-derived from `ContextPlanComposed`, so the composer never writes
task state. `PassthroughComposer` (`packages/noeta-runtime/noeta/core/composer.py`) is the
zero-opinion fallback. The stable-prefix hash is canonical bytes of (stable content, tool
schemas); an empty tool description is omitted so it never churns the hash.

## Content channel rules

Fold sets `active_content[kind][name]` hash last-write-wins (new hash = refresh, same hash = no-op,
empty hash ignored) and stamps the activation anchor first-write-wins, so a refresh moves bytes but
never placement. Renderers get `resolve(kind, name)` over the active hash; a changed file on disk
changes nothing until re-recorded. Skills are the exception: `pinned`, rendered from the preloaded
registry. Pack `init` reruns at every seed and relies on the recorder's no-op-on-unchanged-hash.

## Anchored placement

A resident renders by its activation anchor (`ContextState.content_anchors`): anchor at or before
the first assistant message → semi-stable segment; later → inside the dynamic suffix at that point,
so a mid-task activation appends instead of rewriting the head. Insertion slides past `role="tool"`
messages so it never splits a `tool_use` from its results; an anchor inside a compaction summary
clamps to just after it. See `docs/adr/036-anchored-content-placement.md`.

## Mid-turn injection

`inject_goal` is status-dispatched: running → a durable `InjectionRequested` written via lease-free
`system_emit` plus a poke to the process-local `InjectionInbox` (an accelerator only; resume reads
the durable record); parked on next-goal → `send_goal`; otherwise `NotResumableError`. The Engine
drains at its loop top beside the cancel poll and delivers each as `MessagesAppended` with
`consumes_injection`; fold appends and pops the pending marker in one reduction, so delivery is
exactly-once and never splits a `tool_use` / `tool_result` pair.
See `docs/adr/040-mid-turn-goal-injection.md`.

## Interrupt mechanics

Interrupt lands at the turn boundary, reached promptly because blocking waits are cooperative: the
LLM call is an abandonable wait on an I/O thread (safe because providers are pure), retry backoff
is sliced around the poll, a tool batch checks between calls (unrun calls get paired failed
results), foreground shells are reaped. `force=True` force-clears the wedged step's lease and lets
step-attempt recovery seal the dirty window.

## Guard approval anchors

Whichever action point asked for approval, the suspend records one `ToolCallApprovalRequested`
and `approve` / `deny` resolve it by `call_id`: the model's own for a tool call, reserved
`finish-{task_id}` / `spawn-{task_id}` for the decision points. Approval proceeds without
re-consulting guards; denial returns the refusal to the model and the turn continues. Guards get a
deepcopy of `GovernanceState` and must not mutate it.

## Budget checks per action

A tool call checks every cap; a spawn skips the tool-call cap; a finish checks only historical
accumulators (iterations, cost), so a task that exhausted its tool budget may still answer.
See `packages/noeta-runtime/noeta/runtime/governance.py`.

## Principal

`Principal` (`packages/noeta-runtime/noeta/protocols/values.py`) says who is acting and which
models it may bind; the driver checks a selector against the principal and the deployment
allowlist before any `ModelBound`. Only `principal_identity` is recorded, never the object.
`LOCAL_PRINCIPAL` is the unbounded principal for an embedding with no trust boundary.
