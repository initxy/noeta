# ADR-012: Snapshots are written as the state grows and deleted when the task ends

- Status: accepted
- Date: 2026-10-28
- Refines: ADR-006

## Context

ADR-006 wrote a task's snapshot every `Runtime.snapshot_every` events and never deleted one.
Each snapshot is the whole folded state, so on a 21,627-event log (7.7 MiB of events) the runner
wrote 216 snapshots, 699 MiB in all: 91 times the log. Ended tasks kept theirs forever.

## Decision

- A new snapshot is due once the log is at least `snapshot_every` events past the newest one
  and those events' encoded bytes are at least `SNAPSHOT_GROWTH` (0.1) of its size
  (`noeta/core/runner/task.py`).
- The append that ends a task deletes its snapshot in the same transaction; `put_snapshot`
  keeps none for an ended task.

## Rationale

Tying the interval to the snapshot's size makes the sizes grow geometrically, so the bytes
written stay within about `1 + 1 / SNAPSHOT_GROWTH` times the log's, while a claim folds events
worth at most a tenth of its snapshot. Measured on the log above: 47 snapshots, 75 MiB written
instead of 699 MiB (9.7x the log instead of 91x); the claim went from 19 to 27 ms on sqlite. An ended task is never claimed again,
so its snapshot is only space.

Rejected: a larger fixed interval (still quadratic); a share of a half (2.5x the log, but a
claim folded up to a third of the log, 96 ms).

## Consequences

- A long task's claim folds up to roughly a tenth of its state's bytes in events after its
  snapshot.
