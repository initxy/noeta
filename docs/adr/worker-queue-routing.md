# Worker queue routing: named queues route work, they do not fence it

## Context

The Dispatcher's untargeted poll was `status='ready' AND reserved=0` — first
ready task wins, regardless of who enqueued it. That is correct when every
worker over a store is interchangeable, and it is exactly wrong the moment two
*differently-configured* hosts share one store: worker A claims B's task and
drives it with A's agent registry, model, and workspace policy — no crash, no
corruption, just the wrong configuration silently applied. The documented
workaround ("give separate workload profiles their own SQLite files") forfeits
shared parent/child fan-out and forces per-profile stores.

A second, quieter assumption had the same root: the default
`ChildLifecycleObserver` kept parent↔child lineage in process memory and was
wired once per `Client`, so N clients over one triple meant N observers
double-writing every handoff, while a child driven to terminal by a process
that had not witnessed its `TaskCreated` never notified its parent at all.

## Decision

Every dispatcher row carries a **queue** name (`DEFAULT_QUEUE = "default"`),
assigned once at row birth — explicit, inherited from `parent_task_id`, or the
default — and immutable afterwards. An untargeted `lease` claims FIFO **within
one queue**; there is no wildcard claim. Targeted leases and the maintenance
sweeps (`requeue_stale`, `fire_due_timers`) ignore queues: they never decide
*who* runs a task, only *whether* it is ready.

The queue is **routing state, not identity**: it lives only in the dispatcher
row, never in the event vocabulary. A dispatcher rebuilt from the log alone
re-homes rows to the default queue — routing degrades, the log stays correct.

Children inherit their parent's queue (foreground via the observer, background
via the background-subagent submit), so a task tree runs on the pool that
seeded its root.

The parent↔child handoff becomes store-derived and store-scoped:
`ChildLifecycleObserver` reads the child's `TaskCreated` to find the parent at
terminal time, skips the emit when the parent stream already records this
child's `SubtaskCompleted`, and on construction emits any handoff a crashed
process left missing. `wire_default_observers` is idempotent per event log —
one observer per store instance, owned by the store's lifetime, not any
client's.

## Rationale

- **Routing solves the actual failure.** Fencing (Postgres leases) already
  guarantees a task is claimed once; nothing guaranteed it was claimed by a
  compatible worker. A queue name is the smallest fact that does.
- **No wildcard claim, by construction.** An "any queue" poll would reintroduce
  the wrong-config hazard behind a flag. A deployment that wants one pool for
  everything names one queue.
- **Queue ≠ identity.** Task identity, provenance, and results live in the
  event log (`event-sourced-truth`). Which pool runs the next segment is a
  liveness concern, exactly the dispatcher's job (`worker-lease-model`), so the
  fact lives and dies with the dispatcher row.
- **Lineage belongs to the log.** Any handoff derived from process memory is
  wrong in every topology where the committing process is not the creating
  process. Deriving from the stream makes the observer correct per *store*,
  and idempotent wiring makes it exactly-once per store instance.

## Consequences

- Same-process clients may now share one storage triple — including SQLite —
  each with its own queue; cross-client stealing is structurally impossible.
  **Cross-process SQLite remains unsupported** (`multi-host-lease-fencing`:
  no cross-host fencing without a database server); Postgres remains the
  multi-process answer, now heterogeneous-fleet-capable via queues.
- Breaking Protocol change: third-party Dispatcher adapters must add
  `queue=` / `parent_task_id=` to `enqueue` and `queue=` to `lease`.
- `Client.close()` no longer stops the default observer; it belongs to the
  store. Custom per-client observers still unsubscribe on close.
- Residual accepted race: an observer constructed while another instance is
  mid-emit can duplicate a handoff; group barriers count distinct members and
  stale buffered wakes never match fresh conditions, so readers stay correct.
- The pre-existing same-queue foreground-child steal race
  (`subtask_drain._ChildNotReady`) is unchanged — queues narrow it to a single
  pool but do not close it.
