# ADR-005: Process-bound work holds its task tree, with epochs and a hold_lost notice

- Status: accepted
- Date: 2026-10-28

## Context

Almost everything a task needs lives in its log (ADR-001), but a background shell job is a
process on one machine: its output and its ability to be read or stopped exist only on the
worker that started it. With many workers, another worker could claim the task and find no
job; when that worker dies, the job dies with it and the waiting task would never hear.

## Decision

- A worker running process-bound work holds the task tree (`Store.hold`): until the hold is
  released, other workers skip claiming any task of that tree. Several workers may hold one
  tree.
- A hold has an epoch, never reused, and renewing needs it, so a holder whose hold was reaped
  cannot resume it.
- Every worker reaps expired holds (`Store.reap_holds`, before each claim) and, in the same
  transaction, posts `External("hold_lost")` to the tree's unended tasks, waking a waiting
  one; the model is told the job was lost. A worker stopping on purpose posts the same notice
  without waking the task.
- A local job runs beside a guard that kills it once its worker is gone or its hold is no
  longer fresh; a job's end is posted to the inbox before its hold is released and settled
  once by the task's runner. The holding side is `noeta/core/holds.py`.

## Rationale

Pinning a whole tree to the job's worker is the smallest rule that keeps "read and stop your
job" working with many workers, without making jobs durable (which would mean a job scheduler
inside the harness). Epochs make a reaped hold final, the same way claim epochs fence stale
writers. Posting the loss in the reap's transaction means a waiting task cannot miss it.

Rejected: durable, migratable background jobs (a job scheduler, out of scope); silently
dropping lost jobs (the model would wait forever); ignoring expired holds without a notice.

## Consequences

- While a job runs, its tree's tasks only run on that worker.
- `make cluster` checks under random kills that every job's end or loss is told exactly once.
- A third-party store implements `hold`, `unhold` and `reap_holds`.
