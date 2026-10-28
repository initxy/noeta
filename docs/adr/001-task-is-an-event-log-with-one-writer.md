# ADR-001: A task is an append-only event log with one writer and an inbox

- Status: accepted
- Date: 2026-10-28

## Context

An agent task runs for minutes to days, calls tools with side effects, waits for people, and
must survive crashes and move between processes and machines. Somewhere its state has to be
the truth: in process memory, in mutable rows, or in a log.

## Decision

- A task is an append-only log of a small fixed set of event types (`noeta/core/events.py`).
  The loop state is a pure fold of the log and the model request a pure function of that state
  (`noeta/core/loop/`). The `tasks` row is a projection written in the same transaction.
- Exactly one writer: a worker claims the task (`Store.claim` / `renew` / `release`), and every
  append is conditional on the expected sequence number and the claim's epoch.
- The inbox is the only way in: messages, answers, cancels, child ends, timers and plugin
  signals are posted, idempotent on a dedupe key, and only the runner takes them.
- A tool call is logged as started before it runs; recovery answers an unfinished call
  "outcome unknown" unless the tool looks the outcome up (`Tool.recover`, by a stable
  idempotency key) or declares it safe to run again (`retry_safe`).

## Rationale

A log makes crash recovery, resumption on another machine, forks, rewind, audit and exact
prompt-cache reuse the same operation: fold the log. Conditional appends make "one writer"
a property the store enforces, not a convention a frozen process can break. One way in means
every front end, parent, timer and plugin is serialized through the same ordered, deduplicated
queue, so there is no second path to race.

Rejected: mutable state rows updated in place (no history to recover or fork from, and two
writers race silently); in-memory sessions with periodic checkpoints (a crash loses everything
since the checkpoint and re-runs side effects); letting front ends append events directly (no
ordering against the runner, no dedupe).

## Consequences

- Plugin state is never a new event type; plugins record `ExtensionRecorded` entries and fold
  them in their own `reduce` (ADR-002).
- Anything that cannot be rebuilt from the log (a process-local background job) needs an
  explicit mechanism: holds (ADR-005).
- Long logs make claims slower; snapshots address that without changing the truth (ADR-006).
- Side effects are at-most-once unless a tool opts into recovery or retry.
