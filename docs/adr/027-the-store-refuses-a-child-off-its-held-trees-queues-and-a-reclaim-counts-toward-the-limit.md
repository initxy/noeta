# ADR-027: The store refuses a child off its held tree's queues, and a reclaim counts toward the limit

- Status: accepted
- Date: 2026-10-29
- Refines: [ADR-024](024-a-hold-takes-back-lapsed-claims-and-a-held-tree-stays-on-its-holders-queues.md)

## Context

ADR-024 checks a child's queue in the runner, before the child is written: a tool call's
children before its `after_tool` hooks run, an extension's `TaskView.spawn(queue=)` when it is
staged. Another task of the tree on the same worker can take the hold in between, and the
hold's own queue check cannot see a child not yet written, so the child lands where no worker
will claim it while the tree is held. ADR-024 also left the claim of a lapsed running task
outside the tenant's running limit: with lapsed tasks no longer counted, a worker could take
back a dead worker's task while its tenant's next task ran, two running at a limit of one.

## Decision

- A hold keeps the queues it was taken with (`tree_holds.queues`, a column added in place;
  NULL from an older hold, which is not checked). `Store.append` raises
  `noeta.core.errors.UnservedQueue` for a created child of a held tree on a queue the hold did
  not name, writing nothing (`noeta/core/store.py`). On Postgres the append first takes the
  tree's hold lock shared, as a claim does, so a hold being taken is waited for and seen
  (`noeta/backends/stores/postgres.py`).
- The runner turns a refused tool call into ADR-024's tool error naming the queue, dropping
  the records its hooks staged with the child, and drops a refused hook flush with a warning
  (`noeta/core/runner/calls.py`). ADR-024's earlier checks stay, so the usual refusal comes
  before the hooks run.
- Taking back a running task whose lease ran out is limited by its tenant's running limit
  like any claim, on every store; Postgres counts under the tenant lock for every claim of a
  limited tenant.

The store contract tests (`noeta/testing/store_contract.py`) check both rules.

## Rationale

Only the transaction that writes the child can see a hold taken a moment earlier, so the
store is the one place the queue rule holds without a gap. A lapsed task holds no worker,
but taking it back starts it running again; counting that claim is what makes the limit a
bound rather than a target.

Rejected: serializing child-creating appends with `Runtime.hold` by its per-tree lock (one
process only, and every append path would need it); bumping the schema version for the
column (an additive column older workers ignore). ADR-024's rejection of checking at append
stands for dropping a child after its parent's records are pending; refusing the whole
append, as here, writes neither.

## Consequences

- ADR-024's consequence that a reclaimed task may briefly run one over its tenant's limit no
  longer holds.
- An older worker's hold names no queues, so the store does not check its tree's children;
  the runner's earlier checks still do.
- A third-party store keeps `hold`'s queues and refuses such an append in the same
  transaction that writes the child, and counts a reclaim toward the limit.
