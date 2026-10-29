# ADR-023: Snapshots are kept per fold version; a newer version's log is left to a newer worker

- Status: accepted
- Date: 2026-10-29
- Refines: [ADR-006](006-fold-snapshots.md), [ADR-012](012-snapshots-follow-state-size.md)

## Context

ADR-006 kept one snapshot per task. During a rolling upgrade, workers of two fold versions
claim the same tasks: each ignored the other's snapshot, folded from the start and wrote its
own over it, so the two replaced each other on every claim and neither got a snapshot's
benefit. The same upgrade also hands old workers logs they cannot read: a record kind, tag or
literal that only the newer version writes. Treating such a log as corrupt would end a healthy
task `failed` on whichever old worker claimed it first.

## Decision

- A store keeps snapshots per (task, fold version), at most `SNAPSHOT_VERSIONS` (2) per task
  (`noeta/core/store.py`); a write replaces only its own version's snapshot at a lower seq,
  and past the cap the one at the lowest seq goes. `snapshot(task_id, version=None)` reads one
  version's, or the newest. The runner reads the newest and asks for its own version only when
  that differs. The ending append still deletes every version in its transaction (ADR-012).
  Store schema version 3 (sqlite rebuilds the table, Postgres swaps the primary key; rows are
  kept); the store contract tests (`noeta/testing/store_contract.py`) check it.
- A log or inbox entry this version cannot decode as a newer version's (msgspec's "Invalid
  value" / "Invalid enum value", or a core record of an unknown kind, `records.NewerRecord`)
  makes the worker let the lease run out with nothing written, so a newer worker claims the
  task sooner or later (`noeta/core/runner/task.py`). A worker missing a task's pinned plugin
  backs off the same way.
- A log that no claim can read or fold for any other reason (malformed JSON, a wrong shape),
  or whose pinned plugin is still missing, ends the task `failed` (`unreadable_log`,
  `plugin_missing`) after `LOAD_ATTEMPTS` (3) such claims in a row on one worker (for a
  missing plugin, with the log's last seq unchanged), written from the task row.
- `ToolContext.record` / `TaskView.record` refuse a core record of a kind the core does not
  know (`records.check_writable`), since folding it would leave the task to a newer worker
  for good.

## Rationale

Two versions is what a rolling upgrade needs; the lowest seq is close enough to the least
recently written without another column. A record a newer version wrote cannot be skipped:
it may be a new way to park or end a turn, so skipping it could fold a different task. Backing
off by the lease instead of releasing keeps an old worker from claiming the task again in a
busy loop. A corrupt log is not a newer one, so it still ends after a bounded number of tries
instead of being claimed forever.

Rejected: one row per task with a version-aware overwrite rule (the two versions still replace
each other each claim); a written-at column for eviction; counting failed claims in the store
(a schema change for a rare case); a time bound for a missing plugin (one more clock to set).

## Consequences

- A store holds up to two snapshots of a live task; a third-party store implements the
  `version` argument and the cap.
- During a rolling upgrade a task that a newer worker has touched waits for a newer worker;
  with none left running it waits until one starts.
