# ADR-015: A held tree's tasks run only on its holder

- Status: accepted
- Date: 2026-10-28
- Refines: ADR-011

## Context

ADR-011 refused a hold while another worker held the tree, but a claim and a hold of one tree
could still pass each other: a worker could take a hold while another worker was running a
sub-agent of the same tree under a fresh lease, and on Postgres a claim that picked its row
before a concurrent hold committed went ahead without seeing it. The tree then ran on two
workers, one of them without the holder's jobs, which is what ADR-011 set out to prevent.

## Decision

- A hold without an epoch is also refused while another worker has an unexpired lease on a
  task of the tree; the check is in the hold's transaction.
- On Postgres, claim and hold serialize per tree on one advisory lock: a hold takes it
  exclusive; a claim takes it shared after picking its row, looks at `tree_holds` again and,
  if another worker now holds the tree, rolls back and claims afresh
  (`noeta/backends/stores/postgres.py`). sqlite writes are already serialized.
- Upgrading a version 1 store posts `hold_lost` (epoch 0) for its old `holds` rows, so a task
  that waited on such a hold is told; the old table is kept, unused.

## Rationale

A tree has one holder only if a hold and a running claim of the same tree can never both
succeed. A lease is what says a worker is running a task, so the hold checks leases; the shared
lock makes the claim's second look and the hold's check see each other's commits without
serializing claims of different trees, or of one tree among themselves.

Rejected: dropping the old `holds` table on upgrade (data deletion needs the maintainer).

## Consequences

- A background command asked for while a sub-agent of the tree runs on another worker is
  refused as in ADR-011, and the model reads why.
- A third-party store's `hold` checks running leases of the tree in the same transaction; the
  store contract tests (`noeta/testing/store_contract.py`) check it.
