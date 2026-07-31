# Group members run concurrently on one shared bounded pool, and only on the live drain

## Context

A fan-out group joins N subtasks on an all-of barrier (see
subtask-fanout-and-durable-wake.md), and the orchestration interpreter's
`parallel()` builds such a group from a script. The barrier constrains ordering,
not timing, so the group's wall-clock cost is a separate question: driven one at
a time, a group costs the sum of its members rather than its slowest member.

The payoff is latency on the live path only, and it must not cost determinism:
fold and resume have to re-derive the same parent state from a recording no
matter how the members interleaved.

## Decision

- **The intent rides the suspend condition.** `SubtaskGroupCompleted` carries an
  optional `concurrent` flag that is conditionally folded — absent whenever it is
  unset, so a sequential group's condition carries no scheduling hint at all. The
  intent starts on the transient spawn decision, and the Engine's spawn handler
  copies it onto the persisted condition. A fan-out of two or more members, and
  every `parallel()` group, asks for concurrency by default; setting
  `NOETA_SUBTASK_CONCURRENCY` to `0`/`false`/`off`/`no` forces the sequential
  drain.

- **The executor lives inside the in-process live drain**
  (`noeta.execution.subtask_drain`), not in a worker pool. A concurrent group
  submits each member subtree to a process-global, bounded thread pool whose
  `max_workers` *is* the concurrency ceiling (`NOETA_MAX_SUBTASK_CONCURRENCY`,
  default `min(8, cpu)`) — there is no separate semaphore. The parent stays
  suspended with its lease released until the barrier fires, then resumes in one
  shot. A group of zero or one member drives inline, without spinning the pool
  up.

- **A nested group drains sequentially inside its own worker.** A member that
  fans out further drains its own subtree one at a time, so a pool worker never
  resubmits to the pool. That is what keeps a single shared pool deadlock-free:
  no worker blocks waiting on a saturated pool that cannot schedule the job it is
  waiting for. Consequently `max_workers` caps the total members in flight at any
  nesting depth, and nested *concurrency* is not offered — the payoff is
  overlapping top-level groups.

- **Concurrency is confined to the drain.** Neither the Engine nor the Dispatcher
  protocol carries it: the dispatcher hands out leases for distinct tasks
  concurrently under its own lock, and the only relaxation is that the drain
  holds N member leases at once for a concurrent group. The concurrent join
  collects every future even when one raises, then re-raises the first, so no
  member is left mid-flight holding a lease.

- **The recorded order is authoritative.** Each `SubtaskCompleted` is written to
  the parent's EventLog on arrival, and every non-live path — fold, resume,
  inspection — reads that recording rather than the executor, so no mode flag has
  to be threaded anywhere. The parent's *use* of the results is spawn-order
  deterministic: they are rebuilt by member id.

- **Storage requires no relaxation.** The SQLite adapters open connections with
  same-thread checking off, WAL journaling, a busy timeout and `BEGIN IMMEDIATE`
  write transactions, and subscribers fire after commit and outside the writer
  lock. Writes serialize through a per-adapter lock; the wall-clock win comes
  from overlapping LLM and tool I/O, not from parallel database writes.

- **The observer carries the concurrency hardening.** `ChildLifecycleObserver`
  serializes both its lineage mutations and its read-count / decide / wake
  critical section under one lock, and claims each barrier exactly once through a
  set keyed by `group_id`, so N siblings finishing on N threads fire the group
  wake once and never race the lineage table. The `SubtaskCompleted` emit stays
  outside that lock — it notifies subscribers synchronously and would otherwise
  self-deadlock a non-reentrant lock.

- **The barrier stays all-of.** any-of, k-of-n and fail-fast need subtask
  cancellation and a dynamic group size, and are not offered.

## Rationale

- **Live latency is the only payoff, so pay only the live cost.** A resume reads
  recorded results back out of the EventLog and gains nothing from concurrency.
  Running the executor only on the live drain guarantees that every non-live path
  is single-threaded and deterministic without extra machinery.

- **Committing arrival order to the log is what makes determinism free.** Each
  completion is persisted as it arrives, so the recording is self-consistent
  whatever order produced it. The guarantee is "fold and resume reproduce *that*
  recorded order", not "two live runs are byte-identical" — and the former is
  untouched by concurrency. Ordering would only matter if state were re-derived
  by re-executing subtasks live, which nothing does.

- **A per-group flag with conditional folding keeps the blast radius at zero.**
  Concurrency is something a group actively asks for, and a group that does not
  ask carries no trace of the question in its canonical bytes.

- **Putting the executor in the drain keeps the change local.** The execution
  model is a synchronous inline drain; a standing multi-worker pool would rewrite
  the cancel and resume seams wholesale for no added capability.

## Alternatives considered

1. **Canonically sort a group's completion sequence, and normalize the result
   multiset, so any two live runs record identical bytes.** Rejected: each
   completion is persisted on arrival and fold reads it back in that same
   recorded order, so there is nothing to re-normalize. The sort would be dead
   defensive code guarding a property nothing depends on.
2. **A real worker pool plus a multi-lease dispatcher to drive subtasks.**
   Rejected: far more re-architecture than the capability needs; the inline drain
   carries delegation on its own.
3. **Normalize on write — buffer a group's completions, sort at the barrier, then
   emit.** Rejected: it defers each completion's durable record to the barrier
   moment, so a mid-group crash loses the records of members that finished,
   contradicting the exactly-once wake guarantee.
4. **A non-optional `concurrent: bool = False` field.** Rejected: `False` is not
   absent, so the flag would serialize into every group's condition, writing a
   scheduling hint into the canonical bytes of groups that never use it.
5. **A per-group thread pool.** Rejected: nested fan-out would multiply pools and
   threads. One process-global pool bounds total members in flight through
   `max_workers`, and nested groups drain sequentially in their own worker rather
   than re-entering it — which is also what makes the shared pool deadlock-free
   without a separate semaphore.

## Consequences

- Field naming is uniform: the transient spawn decision carries the intent, the
  persisted `SubtaskGroupCompleted` carries the conditionally folded flag, and
  the spawn handler bridges them.
- The concurrency ceiling is capped at a single point by the process-global
  pool's `max_workers`; nesting does not amplify it.
- The determinism guarantee is that fold and resume reproduce the recorded order.
  Any change that re-derives state by re-running subtasks breaks that and must
  revisit the ordering question.
- `ChildLifecycleObserver` is the component that must stay hardened as
  concurrency evolves: lineage mutations serialized, the group wake claimed
  exactly once, and the completion emit kept outside the lock.
