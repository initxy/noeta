# Fold & snapshot

**fold** is the function that turns an EventLog back into task state. Its
input is deliberately minimal: one EventLog, one ContentStore, one task id —
and nothing else. No clock, no randomness, no external IO, and it never
re-calls a provider. That purity buys a concrete capability: the same log,
folded in any process on any machine, yields **byte-identical state**.

Because of this, resume has no dedicated "load state" logic at all. To recover
a suspended Task, fold it; to show a Task in the web UI or over an SSE
reconnect, fold it; to audit after the fact, still fold it. State is forever a
computed projection, not a separately stored copy that must be kept in
lockstep with the log — and the whole class of "copy out of sync with the
log" bugs disappears with it.

## Two paths, one result

Folding the whole log from the top makes long tasks slower and slower, so fold
keeps a snapshot fast path:

- **From-top path** — bootstrap empty state from the genesis event, replay
  everything.
- **Snapshot path** — restore state from the latest snapshot, replay only the
  tail events after it. A snapshot is itself an ordinary event whose body
  lives in the ContentStore, written before each suspend.

One iron rule sits over both: **the two paths must fold to byte-equal state**.
fold keeps a switch that forces the full replay, and tests use it to
cross-check the snapshot path. The rule pins the snapshot's status to
"performance accelerator," never a second source of truth: delete every
snapshot and behavior is unchanged, only slower.

The same priority handles version drift. When fold meets an old snapshot that
predates newer state fields, it discards the snapshot and falls back to full
replay until a new-version snapshot takes over. Better slow than wrong.

## Canonical rendering

"Byte-equal" needs a backstop, and that layer is called **canonical**: render
any typed value into a stable byte form — keys sorted, separators tight,
UTF-8 throughout. Equivalent objects therefore render to exactly the same
bytes, and the hash of the same content is identical on any machine at any
time. Content addressing leans on canonical to deduplicate, snapshots lean on
it to cross-check, and the reproducibility of the whole event-sourced design
rests on this thin layer. (How old recordings stay foldable as fields are
added and removed is covered in the
[architecture overview](../architecture/overview.md).)

## When fold runs

Every wake, every SSE reconnect, every inspect, and the start of every Engine
step — fold is the single rebuild mechanism behind suspend/resume and
multi-turn conversation. It folds forward only.

Related: [Event sourcing](event-sourcing.md) ·
[Wake & resume](wake-resume.md) ·
[Engine & execution](engine-execution.md)
