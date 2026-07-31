# Fan-out joins N subtasks on one observer-counted group barrier; wakes are delivered at least once and consumed exactly once

## Context

A parent agent dispatches several subagents in one turn and resumes once,
holding all of their results. Separately, a suspended Task's wake — a single
subtask completion, a group barrier, an approval, a human answer, a timer — must
survive a host crash: a wake that should fire always fires, and a redundantly
delivered wake is consumed only once.

Both are bounded by the same constraint: the EventLog is the only source of
truth. Neither mechanism may park coordination state anywhere a replay cannot
reproduce, and neither may widen the recorded shape of an event that every task
writes.

## Decision

### Fan-out and the group barrier

- The provider-visible `spawn_subagent` schema is a batch form: a required
  `spawns` array of `{agent, goal}` entries plus a `background` flag. Routing is
  by the flattened member total across a turn's spawn calls — exactly one member
  yields a single-spawn decision (the only shape that honours `background`); two
  or more yield a fan-out decision, whether they arrive as one call carrying an
  array, several calls, or a mix. The translate seam also accepts a top-level
  `{agent, goal}` form, which the orchestration interpreter fabricates for its
  synthetic spawn turns.
- Barrier accumulation lives in an observer, not the dispatcher. The N
  `SubtaskCompleted` events land on the parent's stream; `ChildLifecycleObserver`
  counts to N by deduplicated membership, then wakes the parent with a single
  scalar `SubtaskGroupCompleted`. The dispatcher matches one scalar condition.
- The barrier is all-of: completion and failure both count as arrivals, and the
  parent resumes with every result — failures included — and decides what
  follows.
- Batch admission is all-or-nothing. Before any subtask is minted, every spec is
  pre-checked: batch size within `MAX_FANOUT`, a `(call_id, member_index)` layout
  the resume pairing can reproduce, and a per-spec guard verdict with the
  spawned-subtask counter simulated at `current + i`. Any deny — a
  require-approval verdict counts as one — fails the parent with zero subtasks
  created.
- `group_id` is a hash of the ordered member ids, so it draws no id from the
  factory and recomputes identically on resume. Wake matching projects on
  `group_id` alone; the member id list rides along for diagnosis and result
  assembly, and `MAX_FANOUT` keeps it under the envelope size cap.
- Results are rebuilt in member order from the keyed `SubtaskCompleted` payloads
  on the parent's stream, not from the unkeyed governance result list. The wire
  shape is exactly one tool result block per originating call: a single-member
  call renders a plain result; a k-member batch call renders one block whose
  output lists the k member results in entry order, successful only when every
  member succeeded. `member_index` is never persisted — the layout it describes
  is reproduced positionally from the recorded assistant message.

### Exactly-once wake

- Delivery is at-least-once and consumption is idempotent. `lease()` leaves the
  matched wake event in place so it survives the lease; `release()` clears it
  only through an explicit typed `consumed_wake_event` argument, passed only
  after the corresponding `TaskWoken` is durable; `requeue_stale()` keeps the
  matched event, so the next lease re-delivers it.
- `release(consumed_wake_event=...)` validates its argument against the stored
  matched event: a mismatch raises and rolls back rather than releasing while
  leaving a stale matched row behind. The heartbeat-cap and `fail()` paths never
  clear it — neither can prove consumption.
- Idempotency is keyed on folded state, not on a dedup field. The worker's woken
  branch reconciles against the most recent matching `TaskWoken`: whether one
  matches, the folded status, and whether an attempt sentinel
  (`ContextPlanComposed`) follows the wake together select between first
  consumption, skipping a crash-resent duplicate, reconciling a terminal or
  re-suspended task, recovering an interrupted attempt, and failing loudly on a
  wake no suspension can consume.

## Rationale

- **Barrier state belongs in the log, not the dispatcher.** The arrival set is
  derivable from the parent's own stream, so folding it in an observer keeps the
  dispatcher's model a single scalar and the replay surface small.
- **Deduplicated membership is idempotent by construction.** Intersecting the
  arrivals with the declared member set and checking for the full set makes a
  duplicate completion a no-op, filters an out-of-group or late completion, and
  lets nothing pad the barrier full. A bare count has none of those properties.
- **Exactly-once costs no second schema.** The idempotency key is the folded pair
  of task status and the latest `TaskWoken`'s wake event, both of which the
  recording carries anyway, so no event grows and the fold surface does not
  widen. The clear timing lives entirely in dispatcher state, which is not part
  of the EventLog and is therefore invisible to fold and resume.
- **The array is the schema because it is the shape models emit.** A model
  narrates a parallel plan and then emits exactly one spawn call per turn, even
  under an explicit demand for several, while the same session happily
  parallel-calls ordinary tools. Given an array parameter, it batches several
  goals into one call. A fan-out that depends on multi-call turns collapses to
  strictly sequential delegation.

## Alternatives considered

1. **The dispatcher accumulates the arrival set behind a durable column.**
   Rejected: it turns join state into mutable dispatcher state a replay must
   reproduce, and touches the wake-recovery surface, for something already
   derivable from the parent's stream.
2. **A configurable join policy (any-of / k-of-n / fail-fast).** Rejected: those
   modes need subtask cancellation and a dynamic group size, both of which
   complicate resume. The deterministic wait-all is pinned instead, leaving a
   policy slot open.
3. **Reuse `SubtaskCompleted` with the parent holding N scalar conditions.**
   Rejected: `wake_on` is a scalar field, and turning it into a list would touch
   fold, snapshot and dispatcher serialization while leaving "wait for all"
   nowhere to live. A distinct group condition makes the group semantics explicit
   and keeps `wake_on` a single condition.
4. **A `wake_id` dedup field on the wake payload.** Rejected: it enlarges every
   wake event ever written for a property that folded state already answers.
5. **`WakeReady` / `WakeConsumed` events written inside the dispatcher
   transaction.** Rejected: recorded event types widen the fold surface for
   bookkeeping that non-recorded dispatcher state carries just as well.
6. **A periodic reconciliation sweep over the EventLog.** Rejected: a standalone
   daemon with its own liveness and timing semantics, where the lease / release /
   requeue model closes the crash window deterministically without one. It
   remains available as defence in depth.

## Consequences

- The group condition and its `group_id` projection live in
  `noeta.protocols.wake`, the spawn decisions in `noeta.protocols.decisions`, and
  the `consumed_wake_event` argument on `noeta.protocols.dispatcher`.
- All-or-nothing batch admission lives in `noeta.core._decision_handlers`, the
  barrier count in `noeta.core.observers`, the wake-recovery state machine in
  `noeta.runtime.worker`, and the clear timing in the dispatcher adapters.
- Wall-clock concurrency between group members is a separate decision; see
  subtask-parallel-execution.md.
