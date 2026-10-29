# ADR-006: Claims fold from versioned snapshots; a snapshot may be missing, never wrong

- Status: accepted
- Refined by: ADR-012
- Date: 2026-10-28

## Context

A claim folds the task's whole log (and every conversation it continues). Long tasks have tens
of thousands of events, and tasks are claimed again after every park, crash or move between
workers, so the fold cost grows with the task's age.

## Decision

- The runner keeps one fold snapshot per task (`noeta/core/snapshots.py`, a `snapshots` table
  in each store), written every `Runtime.snapshot_every` events (default 100), always folded
  from stored events, never from live state, and kept only if it reads back equal.
- A claim folds the newest snapshot plus the events after it.
- A snapshot's version hashes the kernel's fold code, the agent, each plugin's pin and its
  classes' source; on any mismatch it is ignored and the log is folded from the start.
- The test suite folds every log it loads both ways and compares.

## Rationale

The log stays the only truth (ADR-001); a snapshot is a cache of a fold of an immutable log
prefix, so writing it needs no claim and a newer one simply wins. Versioning by the code that
folds means an upgrade or a plugin change can only make a snapshot unused, never apply stale
fold rules. Measured on a 20,002-event log: sqlite claims 120 ms -> 21 ms, Postgres 197 ms ->
119 ms.

Rejected: mutable state rows as the truth (gives up ADR-001); snapshots versioned by a manual
number (forgetting to bump it yields a wrong fold); snapshots without a both-ways test.

## Consequences

- A plugin's state must round-trip through the snapshot encoding, or that task gets no
  snapshot (correct, only slower).
- Snapshots hold the whole conversation, so a very long task's claim on Postgres is bound by
  transfer size.
