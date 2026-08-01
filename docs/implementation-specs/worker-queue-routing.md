# Worker queue routing (named queues on the Dispatcher)

Status: shipped 2026-08-01 (gates green: 3542 passed, coverage 85.77%, mypy strict, lints) — awaiting owner review/archive
Owner: initxy

## Goal

Let differently-configured hosts share one storage stack without stealing each
other's work. Every dispatched task row carries a **queue** name; an untargeted
worker poll claims only its own queue. Alongside it, make the parent↔child
handoff safe under shared stores: the `ChildLifecycleObserver` derives lineage
from the durable log instead of process memory, and default-observer wiring
becomes idempotent per event log so N clients over one triple cannot double-fire
the handoff.

No backward compatibility is kept: the Dispatcher Protocol changes shape
(breaking for third-party adapters), and the observer drops its in-memory
lineage. Final form only.

## Non-goals

- Cross-process SQLite sharing. Lease fencing still needs a database server;
  multi-process stays Postgres-only (ADR `multi-host-lease-fencing` unchanged).
- Fixing the pre-existing same-queue foreground-child steal race
  (`subtask_drain._ChildNotReady`'s KNOWN GAP). Queues remove the *cross*-client
  steal; the intra-pool race keeps its documented degrade path.
- Putting the queue into the event vocabulary. The queue is dispatcher-local
  routing state, not task identity; a dispatcher rebuilt from the log alone
  re-homes rows to the default queue (documented consequence).
- Per-queue worker prioritisation or weights. One name, one filter.

## Decisions

**D1 — Queue is a column on the dispatcher row, immutable after birth.**
`DEFAULT_QUEUE = "default"` lives in `noeta.protocols.dispatcher`. A row gets
its queue exactly once, at INSERT: explicit `queue=` if given, else inherited
from `parent_task_id`'s row when supplied, else `DEFAULT_QUEUE`. Enqueue on an
existing row never changes the stored queue. Wake / release / fail /
requeue_stale / fire_due_timers preserve it untouched.

**D2 — Untargeted lease filters by queue; targeted lease ignores it.**
`lease(task_id=None, queue=...)` claims FIFO within `status='ready' AND
reserved=0 AND queue=?`. There is no wildcard claim — a pool that wants
everything names one queue and routes everything there. A targeted lease knows
its task and needs no filter. Maintenance sweeps (`requeue_stale`,
`fire_due_timers`) stay queue-agnostic: they only flip status, and the row's
own queue routes it back to its pool.

**D3 — Enqueue call sites.** `InteractionDriver` gains `queue=` (stamps root
tasks at seed). `ChildLifecycleObserver` and
`BackgroundSubagentRegistry._submit` pass `parent_task_id=` so children inherit.
`subtask_drain`'s parent re-enqueue touches an existing row — nothing to pass.
`WorkerLoop` gains `queue=` and claims only it. The SDK `Client` threads
`HostConfig.queue` into both driver and worker pool.

**D4 — Observer lineage moves from memory to the log.** `_on_terminal` derives
`(parent_id, background)` by reading the child stream's `TaskCreated` instead of
popping an in-process dict, so the handoff fires correctly in whichever process
committed the terminal. Duplicate protection becomes durable: skip the emit if
the parent stream already carries `SubtaskCompleted` for this child.
Construction replay becomes *recovery*: emit the missing `SubtaskCompleted` (and
wake) for any non-background child that is terminal but unrecorded on its
parent — this closes the today-standing crash window where a crash between the
child's terminal commit and the handoff emit hung the parent forever. Residual
accepted race: an instance starting up while another instance is mid-emit can
double-write the handoff; group barriers count distinct members and a stale
buffered wake never matches a fresh condition, so both reads stay correct.

**D5 — Default-observer wiring is idempotent per event log.**
`wire_default_observers` marks the event log it wired and returns the existing
stop for repeat calls, so N clients over one shared triple get exactly one
`ChildLifecycleObserver`. The observer's lifetime is the store's, not any one
client's: `Client.close()` no longer unsubscribes it.

## Plan

- [x] Protocol: `DEFAULT_QUEUE`, `enqueue(queue=, parent_task_id=)`,
      `lease(queue=)` in `noeta/protocols/dispatcher.py`.
- [x] Backends: `InMemoryDispatcher` (+`_DispatcherTask.queue`); sqlite
      migration 11 (`queue` column + ready-index on `(queue, ready_order)`) and
      enqueue/lease SQL; postgres migration 6 and enqueue/lease SQL.
- [x] Observer: store-derived lineage, durable dedupe, recovery replay; drop
      `_lineage` / `_replay_lineage`.
- [x] Wiring: idempotent `wire_default_observers`; `Client` stops
      unsubscribing the default observer on close.
- [x] Call sites: driver seed enqueue (`queue=`), background-subagent submit
      (`parent_task_id=`), `WorkerLoop` claim (`queue=`), `HostConfig.queue`,
      `Client.start_workers`.
- [x] Tests: per-backend queue contract (birth, inheritance, immutability,
      claim filter, sweep preservation); observer cross-instance handoff,
      durable dedupe, crash recovery; wiring idempotency; client-level
      two-clients-one-store routing.
- [x] Docs: ADR `worker-queue-routing.md` (+ index), `operations/limitations.md`
      + zh mirror (same-process shared SQLite is now supported via queues;
      cross-process still Postgres), CONTEXT.md vocabulary.
- [x] Gate: `make check`.

## Acceptance criteria

- Two Clients with different queues over one shared triple: each resident pool
  drains only its own tasks; a task seeded on queue B is never claimed by A's
  pool; subtasks (foreground and background) run on their parent's queue.
- One shared triple, N clients: a child's completion produces exactly one
  `SubtaskCompleted` on the parent stream and one wake.
- Kill between a child's terminal commit and the handoff emit: a freshly
  constructed observer over the same store emits the missing handoff and the
  parent wakes.
- `requeue_stale` / `fire_due_timers` re-ready rows without changing their
  queue.
- `make check` green.

## Risks

- Third-party Dispatcher implementations break (two new keyword params). By
  design — no compatibility kept; release notes must call it out.
- A dispatcher rebuilt from the log alone (disaster path,
  `_restore_dispatcher_to_baseline` / `restore_task`) re-homes rows to
  `DEFAULT_QUEUE`; routing degrades, correctness holds (targeted leases and the
  log are unaffected). Documented in the ADR.
- Observer terminal-path now reads the child stream (one `read()` per child
  terminal) and the parent stream (dedupe + wake logic, already read today).
  Bounded by stream length; no ContentStore reads added.
