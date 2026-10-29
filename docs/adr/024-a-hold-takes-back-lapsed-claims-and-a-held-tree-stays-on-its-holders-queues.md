# ADR-024: A hold takes back lapsed claims, and a held tree stays on its holder's queues

- Status: accepted
- Date: 2026-10-29
- Refines: [ADR-015](015-a-held-tree-runs-only-on-its-holder.md),
  [ADR-019](019-tenant-limits-are-enforced-by-the-kernel.md) (the running limit)
- Refined by: [ADR-027](027-the-store-refuses-a-child-off-its-held-trees-queues-and-a-reclaim-counts-toward-the-limit.md), [ADR-028](028-no-tenants-in-noeta.md) (no running limit)

## Context

ADR-015 refuses a hold while another worker has an unexpired lease on a task of the tree. A
worker that stalled past its lease (a long pause, a frozen process) no longer blocks the hold,
but it still has its claim's epoch: once it wakes it could renew or append, and the tree would
run on two workers after all. Queues open a second way off the holder: no other worker claims
a held tree's tasks, so a task of the tree on a queue the holder does not serve would never run,
and a child created onto such a queue while the tree is held would wait forever.

## Decision

- In the hold's transaction, before the live-lease check, every task of the tree `running` on
  another worker whose lease has run out gets `owner_epoch + 1` and goes back to `ready`
  (announced `released`), so the stalled worker's next `renew` or `append` fails on the epoch.
  On Postgres the hold locks those rows `NOWAIT` and, if a claim or renewal in flight has one,
  rolls back and tries again, so it never waits on a row while holding the tree's lock that a
  claim then waits for (`noeta/backends/stores/postgres.py`).
- `Store.hold(queues=)`: taking a hold is refused while an unended task of the tree is on a
  queue the holder does not serve; renewal does not check.
- A child goes to its parent's queue unless one is named (`Claim.queue`, filled by every
  store). While a worker holds a tree, creating a child on a queue it does not serve fails,
  staging nothing: a tool call with a tool error naming the queue, an extension's
  `TaskView.spawn(queue=)` with `ValueError`, both through `TaskRun.serves_queue`.
- A tenant's running limit counts only running tasks with a live lease (`lease_until >= now`),
  on every store; a lapsed claim is not running.

The store contract tests (`noeta/testing/store_contract.py`) check each store rule.

## Rationale

The lease only says a worker may be dead; the epoch decides who writes, so taking a lapsed claim
back by its epoch is what makes ADR-015's "one tree, one worker" hold against a worker that
merely stopped for a while. Checking queues when the hold is taken, and refusing a child onto a
foreign queue while it is held, keeps every task of a held tree claimable by its holder, which
is the only worker allowed to claim it.

Rejected: `renew` refusing while another worker holds the tree (the stalled worker could still
append until its next renewal, and every renewal would read `tree_holds`); checking queues on
renewal (it would stop a running background job); letting a claim take a held tree's task on a
queue the holder does not serve (it would run off the holder); checking a spawn's queue at
append (the child's record is already pending, and dropping it would leave the parent waiting
on a child that never exists).

## Consequences

- A child the host routes onto another queue while its tree is not held may still be pending
  when a hold is asked for; the hold is then refused as in ADR-011 and the model reads why.
- A task whose lease ran out may be reclaimed while its tenant's next task runs, briefly one
  over the running limit, as a dead worker's task was before.
- A third-party store implements the lapsed-claim takeback, `hold(queues=)` and the live-lease
  count in the transactions named above.
