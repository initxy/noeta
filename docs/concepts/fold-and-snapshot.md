# Fold & snapshot

**fold** is the function that turns an EventLog back into Task state. Its input is
deliberately minimal — `fold(event_log, content_store, task_id)` and nothing
else. No clock, no randomness, no external IO beyond those two stores, and it
never re-calls a provider. That purity buys a concrete capability: the same log,
folded in any process on any machine, yields **byte-identical state**.

Because of this, resume has no dedicated "load state" logic at all. To recover a
suspended Task, fold it; to render it in a host UI, fold it; to audit it after
the fact, still fold it. State is always a computed projection, not a separately
stored copy that must be kept in lockstep with the log — and the whole class of
"copy out of sync with the log" bugs disappears with it.

## Two paths, one result

Folding the whole log from the top makes long tasks slower and slower, so fold
keeps a baseline fast path:

- **From-top path** — bootstrap empty state from the `TaskCreated` genesis event,
  then replay everything.
- **Baseline path** — restore state from the newest baseline event, then replay
  only the tail after it.

A baseline is an ordinary event carrying a `state_ref` into the ContentStore.
Four types qualify: `TaskSnapshot`, `TaskRewound`, `StepAttemptAbandoned`, and
`TaskForked`. `TaskSnapshot` is pure acceleration — the Engine writes one before
every terminal event, before every suspend, and mid-loop once consecutive
tool-call turns cross `CONSECUTIVE_TOOL_CALLS_SNAPSHOT_THRESHOLD` (20), so a
Policy that never yields still leaves a usable resume point. The other three are
meaningful re-base markers, and fold applies them on *both* paths: a rewound
conversation, an abandoned step attempt, and a forked task all name a new
baseline rather than editing what came before.

One iron rule sits over both paths: **they must fold to byte-equal state**.
`fold(..., ignore_snapshots=True)` forces the full replay, and tests use it to
cross-check. The rule pins `TaskSnapshot`'s status as a performance accelerator,
never a second source of truth: delete every one of them and behavior is
unchanged, only slower.

The same priority handles state fold cannot trust. A baseline body missing fields
fold relies on is discarded in favor of a full replay, until a freshly written
baseline reactivates the fast path. Better slow than wrong.

## Canonical rendering

"Byte-equal" needs a backstop, and that layer is **canonical**: render any typed
value into a stable byte form — JSON with sorted keys, compact separators, UTF-8
throughout. Equivalent objects therefore render to exactly the same bytes, and
the hash of the same content is identical on any machine at any time. Content
addressing leans on canonical to deduplicate, and baseline bodies round-trip
through it so tagged value types (`ContentRef`, wake conditions, subtask results,
typed message blocks) keep their identity across the boundary. The reproducibility
of the whole event-sourced design rests on this thin layer. (How recordings stay
foldable as fields are added and removed is covered in the
[architecture overview](../architecture/overview.md).)

## When fold runs

Every wake, every inspect, and the start of every leased step. A point-in-time
read — "how did this task stand as of seq N?" — folds through a bounded read-only
view of the same events rather than teaching fold a cap. It folds forward only:
nothing on the stream is ever rewritten.

Related: [Event sourcing](event-sourcing.md) ·
[Wake & resume](wake-resume.md) ·
[Engine & execution](engine-execution.md)
