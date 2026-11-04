# ADR-029: A task keeps its own claim while it owns process-bound work

- Status: accepted
- Date: 2026-11-04
- Supersedes: [ADR-005](005-holds-with-epochs-and-hold-lost.md),
  [ADR-011](011-one-holding-worker-per-tree.md),
  [ADR-015](015-a-held-tree-runs-only-on-its-holder.md),
  [ADR-024](024-a-hold-takes-back-lapsed-claims-and-a-held-tree-stays-on-its-holders-queues.md),
  [ADR-027](027-the-store-refuses-a-child-off-its-held-trees-queues-and-a-reclaim-counts-toward-the-limit.md)

## Context

Work that lives only in one worker process (a background shell job, a `Monitor`, an MCP call
parked on the person's answer) has to find its task on that worker at the task's next turn.
Holds pinned the whole task tree to the worker: a table of holds with epochs, a reaper on
every claim, `hold_lost` posts, one holding worker per tree, a per-tree advisory lock, a
check that the holder serves every queue of the tree, and a refusal (`HeldElsewhere`) of
any job a second worker would have started. Each fix to it added another rule, and it
refused background work by where tasks happened to run.

## Decision

- A tool that starts such work keeps its task's claim (`ToolContext.keep()` returns a
  `KeptClaim`, released when the work ends). While a claim is kept, the runner hands the
  task back waiting with `Patch.keep`: the row says `waiting`, but its owner, epoch and
  lease stay, and the runner goes on renewing the lease and watching the task's inbox
  itself. A claim skips a row whose lease has not run out, so no other worker runs the
  task, and a post to it is taken by the same run. Once nothing keeps the claim, the run
  gives it back (`Store.release` leaves the task waiting) and ends.
- Per task, not per tree. A task's jobs are its own (the shell's table is keyed by task);
  a parent stops a child's job by posting `STOP_JOB` to the child; a task's end kills its
  jobs (a sub-agent's when it hands back).
- `Runtime.run_claim` returns once its task waits with a kept claim; the run goes on in the
  background, taking a slot of `concurrency` only while a wake has it stepping (it waits
  for one when they are all taken). `Runtime.let_go` (on the way out of `serve`)
  gives those claims up: the work dies, the tasks stay waiting, and hear of it with whatever
  wakes them next.
- A worker that dies loses its work with it. Its kept lease runs out, which makes the
  waiting task claimable at once; the next claimer has no such job, so the shell posts
  `JOBS_LOST` behind whatever the inbox holds (an end posted before the death still tells
  its own story) and the model is told the job is lost. A job's guard removes its `.alive`
  file every quarter lease and kills the job when it is already gone, so the job dies at
  most half a lease after its worker last re-created the file; the lease, renewed every
  third of it, lasts at least two thirds of it after a worker froze, so a frozen worker's
  job dies before another worker can call it lost.
- No holds table, epochs, reaper, advisory lock or queue check. A 0.6 store's `holds`
  table is dropped when it is upgraded (schema version 5).

## Consequences

- A sub-agent's background job no longer outlives the sub-agent (it used to report to the
  root). A sub-agent that needs its job's result waits for it before it hands back.
- A task waiting on a job costs its worker a lease renewal and an inbox check (on the change
  feed where the store shares one, else every `poll_interval`) per such task.
- A worker frozen past its lease and then resumed finds its claim taken (the epoch fences
  every write) and stops what it kept.

Rejected: ending a job with its task's claim at every wait (simpler still, but every
background dev server would die when its turn ends, on a single machine too); handing a job
over to another worker (a process cannot move).
