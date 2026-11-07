# ADR-032: A task that keeps killing its worker ends at a restart limit

- Status: accepted
- Date: 2026-11-06
- Refines: [ADR-001](001-task-is-an-event-log-with-one-writer.md)

## Context

A worker that dies leaves its claim to run out, and another worker takes the task over
(ADR-001). The kernel already bounds two repeats of one call: a model call with no reply
(`texts.MAX_MODEL_ATTEMPTS`) and a `retry_safe` tool's starts (`tool.MAX_ATTEMPTS`). A run
that kills its worker anywhere else (a plugin hook, building the request, a model that calls a
process-killing command again after "outcome unknown", a hang past the lease) was taken over
again and again, and with many workers took each of them down in turn.

## Decision

- `Store.claim` says whether the claim before was given back: `Claim.reclaimed` is true when the
  row still had a lease (no release, no handing-back append), read in the claim's transaction
  (`noeta/backends/stores/`). A third-party store that leaves it `False` gets no limit.
- The worker that will run the task (after the plugin check) records `core/reclaimed` before
  `on_start` (`noeta/core/runner/task.py`). The count is in the log: written by the next
  worker, fenced by its epoch like any append, the same on every store.
- The fold counts them (`TaskState.reclaims`) until a step finishes after one: a model reply
  recorded after the latest `reclaimed` whose calls all have results. Parking does not reset it.
- A task gets `Runtime.restart_limit` such restarts in a row (default 3, `0` no limit;
  `noeta worker --restart-limit`); the pickup after one more ends it `failed` with
  `restart_limit` through the usual end, its open calls answered, so a parent hears a failed
  `ChildEnded`. The default sits above the per-call bounds (3 model calls, 3 starts of a
  `retry_safe` call), so those act first.

## Rationale

Only the store can tell a crash from a clean hand-back (a release on shutdown writes no event);
only the log keeps a count that survives the crash it counts and that racing workers cannot both
bump. A step started after the pickup is the progress a crash loop never makes, while a healthy
task makes it right after one lost machine. A `Waiting` is no proof of a clean stop: a crash can
fall between it and the hand-back.

Rejected: a counter column bumped by every claim of a lapsed row (it would also count the
claims of workers that leave the task to another and write nothing: a rolling upgrade, a
missing plugin); counting in worker
memory, as `plugin_missing` does (per worker, lost with the crash); resetting on any append
(a looping run appends before it crashes, as pi-durable's reset on a commit does); failing only
an interactive task's turn, as `MAX_MODEL_ATTEMPTS` does (each later message would crash a worker
again before anything could stop it).

## Consequences

- A crash before the next worker writes anything (folding a log too large for its memory) is not
  counted.
- A machine lost four times within one step ends a healthy task too.
- Workers older than this one leave a task with a `reclaimed` record to newer workers
  (`NewerRecord`).
