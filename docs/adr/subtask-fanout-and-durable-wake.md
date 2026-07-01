# Parallel fan-out / N-way join + durable exactly-once wake

## Context

This extends the wake protocol to add two related things:

- Parallel fan-out / N-way join: a parent agent fans out **N sub-agents** at once, suspends, and after all N terminate resumes once and uses all N results at once (join / barrier).
- Durable exactly-once wake: a suspended Task's wake (a single subtask / a group, human / approval, timer) must be **delivered and consumed exactly once** even across a crash — a wake that should fire always fires, and a redundantly delivered wake is consumed only once.

Both hold one red line: **don't change the EventLog / payload**, so recorded bytes don't drift.

## Decision

### Parallel fan-out / N-way join

- **Core characterization**: at the wake-protocol level, fan-out is one "N-way join," **not wall-clock parallelism**. Under a single worker (one lease per segment / single writer, see worker-lease-model.md, single-writer-invariant.md), the N subtasks stream and lease independently but **drain serially**. The new capability only lets the parent express "resume after all N terminate." True multi-worker concurrency is **explicitly out of scope**.
- **The join accumulation lives in an observer (fold count), and the dispatcher's scalar model is untouched**: N `SubtaskCompleted` events (`origin="observer"`) land on the parent stream, `ChildLifecycleObserver` counts to N by **deduplicated membership** (not a bare count), then calls `wake(parent, SubtaskGroupCompleted)` — a single scalar composite event. The dispatcher still matches a scalar, unchanged.
- **all-of (wait for all to terminate)**: both `completed` and `failed` count as arrivals; the parent resumes with all N results (including failures) and decides the next step. any-of / k-of-n / fail-fast reserve a `policy` extension slot; not done in v1.
- **Batch spawn = all-or-nothing admission**: before minting any subtask, pre-check all N specs (budget simulated by `current+i`). Any rejection → the parent fails with zero subtasks; any require_approval → treated as a rejection.
- **`group_id` is derived from the ordered subtask ids** (`sha256(":".join(subtask_ids))`), consuming no extra id_factory draws; fold/resume reads it back from history's `TaskSuspended.wake_on.group_id` and recomputes the same value byte-for-byte.
- **Results are rebuilt by member (spawn) order from the keyed `SubtaskCompletedPayload`**, not from the unkeyed `governance.subtask_results`. `subtask_ids` is bounded by the 4KB envelope cap, `1<=N<=MAX_FANOUT`.

### Durable exactly-once wake

- **The gap**: previously `lease()` would **destroy** `matched_wake_event` (at-most-once), so a worker crashing between `lease()` and the durable `TaskWoken` **lost the wake**.
- **The fix = at-least-once delivery + idempotent consumption = exactly once**: `lease()` **no longer clears** matched (D1, it survives the lease); `release()` clears matched only through an explicit typed `consumed_wake_event` seam (D2, and only the worker-woken branch passes it, after `TaskWoken` is durable); `requeue_stale()` **keeps** matched → the next lease re-delivers it automatically (D3).
- **Idempotent consumption via folded state**: the worker-woken branch is a **"recover-from-most-recent-matching-`TaskWoken` state machine"** (D4, keyed on `(whether a TaskWoken matches / folded status / whether there's a step event after the wake)`, 6 cases) — first consumption, skip-on-crash-resend, terminal / re-suspend reconciliation, partial-step typed error, and mismatch loud failure.
- **`release(consumed_wake_event=X)` is validated** (D6): X ≠ the stored matched → typed raise + rollback, never "release normally but skip the clear" leaving a stale matched row. The heartbeat-cap and `fail()` paths **never clear** matched (D5, they can't prove consumption).
- **Don't change the EventLog / payload**: `TaskWoken`'s shape bytes are unchanged; D1–D3 only move the dispatcher's internal clear timing (dispatcher state isn't in the EventLog → invisible to fold/resume), and D4's fold check is a no-op on every clean recording. **All existing recordings fold and resume as before.** Scope: single host, single worker.

## Rationale

- **The join accumulation goes into the observer / EventLog rather than the dispatcher, to touch less hardened surface.** Putting the arrival set into the dispatcher would mean one more mutable state replay must reproduce, and it would touch the already-hardened `matched_wake_event` wake-recovery incision. But that state **is already derivable from the parent stream** (the N `SubtaskCompleted` events are right there), so the observer-fold route needs zero dispatcher changes and has a smaller replay surface.
- **The deduplicated-membership check is naturally idempotent.** Intersect and then check the full set: a duplicate completion for the same subtask is idempotent, an out-of-group / late completion is filtered out by the intersection, and there's no way to "pad" the barrier full. More robust than a bare count.
- **exactly-once adds no second schema, to keep recorded bytes stable.** Adding `wake_id` dedup to `TaskWokenPayload` would change the canonical bytes of **every** `TaskWoken`, move their content hashes, and drift all historical recordings. The idempotency instead draws from **the folded `(status, latest TaskWoken.wake_event)`** — `wake_event` is already in the existing payload, so no event grows. The dispatcher's internal clear-timing change is invisible to the EventLog, so zero drift.
- **A single worker is an honest determinism boundary.** The order of the N `SubtaskCompleted` events = the drain order, which under a single worker is deterministic (FIFO ready queue) → fold/resume re-derives the same state. Multi-worker parallelism would make completion order non-deterministic → the same EventLog could fold to different orders, so it is explicitly out of scope (at which point in-group events would need canonical ordering by subtask_id, folded into a separate ADR). *(v2 update: subtask-parallel-execution.md landed that path and found the canonical subtask_id ordering unnecessary — committing arrival order into the log is itself authoritative — so it was removed as dead defensive code.)*

## Alternatives considered

1. **Fan-out: the dispatcher accumulates the arrival set + adds a new durable column.** Rejected: it puts join state into the dispatcher — one more mutable state replay must reproduce, and it touches the hardened `matched_wake_event` surface; that state can be derived from the EventLog.
2. **Fan-out: ship a configurable policy (all/any/k-of-n) in v1.** Rejected: any-of / k-of-n need subtask cancellation + dynamic group size, complicating resume. Pin the deterministic wait-all first; reserve an extension slot for policy.
3. **Fan-out: reuse `SubtaskCompleted` without a new variant, the parent holding N scalar conditions.** Rejected: `wake_on` is a scalar field and can't hold N; making it a list would touch fold / snapshot / dispatcher serialization surfaces, and "wait for all" would have nowhere to live. Adding the `SubtaskGroupCompleted` variant makes the group semantics explicit while `wake_on` stays a single condition.
4. **Wake: add `wake_id` / `wake_seq` to wake events + add a dedup field to `TaskWokenPayload`.** Rejected: it changes the canonical bytes of every `TaskWoken` and drifts all history.
5. **Wake: additionally write `WakeReady` / `WakeConsumed` events inside the dispatcher transaction.** Rejected: new recorded event types → changing recording shape + enlarging the fold surface, heavier and more prone to drift.
6. **Wake: a periodic EventLog reconciliation sweep.** Rejected: a standalone daemon mechanism with its own liveness / timing semantics; the lease / release / requeue model already deterministically closes the crash window, and a sweep is left as future defense-in-depth.

## Consequences

- Protocol layer: the `SubtaskGroupCompleted` variant + the `matches_wake` projected by group_id land in `noeta.protocols.wake`, `SpawnSubtasksDecision` lands in `noeta.protocols.decisions`, the `release(consumed_wake_event=...)` seam lands in `noeta.protocols.dispatcher`.
- Handling layer: the all-or-nothing `handle_spawn_subtasks` lands in `noeta.core._decision_handlers`, the group-aware fold count lands in `noeta.core.observers`, and the related fold is in `noeta.core.fold`.
- Runtime and storage: D4's 6-case recovery state machine lands in `noeta.runtime.worker`, and D1–D6's clear timing lands in `noeta.storage.memory` and `noeta.storage.sqlite.dispatcher`.
- Follow-on note: this decision deliberately excludes multi-worker concurrency as a determinism boundary; true concurrent execution is taken up by subtask-parallel-execution.md, which has proven the canonical subtask_id ordering can be dropped.
