# Fold & snapshot

**fold** is the function that turns a Task's EventLog back into Task state.
It is the only way state is ever produced — there is no "load" path beside it.
Recovering a crashed Task, rendering one in a UI, and auditing one a month
later are all the same call.

Its input is deliberately tiny: `fold(event_log, content_store, task_id)`, and
nothing else. No clock, no randomness, no network, no re-calling a model
provider. That poverty is the point — it means the same log folded in any
process on any machine yields **byte-identical state**.

<p align="center">
  <img src="../assets/diagrams/crash-resume.svg" alt="Crash resume — a worker dies mid-step, its lease expires, another worker folds the log, seals the abandoned attempt, and resumes exactly once" width="820">
</p>

## The common case: a machine dies mid-step

The diagram above is the whole recovery story, and it contains no recovery
code. Worker A is advancing a Task and is killed. Its lease stops being
heartbeated and expires. The stale sweep returns the Task to the ready queue.
Worker B leases it, folds the log — which is simply what every leased step does
first — appends a marker sealing the interrupted attempt, and carries on.

Nothing had to be "saved" before the crash, because every durable fact was
already an event. There is no half-written state file to reconcile, and no
class of "the copy drifted from the log" bugs to fix, because there is no copy.

## Two paths, one result

Replaying a long log from the top gets slower and slower, so fold keeps a fast
path:

- **From-top path** — bootstrap empty state from the `TaskCreated` genesis
  event, then replay everything after it.
- **Baseline path** — restore state from the newest baseline event, then replay
  only the tail that follows it.

A baseline is an ordinary event carrying a `state_ref` into the ContentStore.
Four types qualify:

| Baseline event | Why it exists |
| --- | --- |
| `TaskSnapshot` | pure acceleration — nothing else changes |
| `TaskRewound` | the conversation was rewound to an earlier turn |
| `StepAttemptAbandoned` | an interrupted attempt was sealed as dead history |
| `TaskForked` | a new Task branched off this state |

`TaskSnapshot` is written before every terminal event, before every suspend,
and mid-loop once consecutive tool-call turns cross
`CONSECUTIVE_TOOL_CALLS_SNAPSHOT_THRESHOLD` (20) — so a Policy that never
yields still leaves usable resume points. The other three are meaningful
re-base markers, and fold applies them on *both* paths: a rewound conversation,
an abandoned attempt, and a forked Task each name a new baseline rather than
editing what came before.

## The iron rule: both paths fold byte-equal

`fold(..., ignore_snapshots=True)` forces the full replay, and the test suite
uses it to cross-check the two paths against each other.

That rule pins `TaskSnapshot`'s status permanently: it is a performance
accelerator, never a second source of truth. Delete every snapshot in the
system and behaviour is unchanged, only slower.

The same priority governs state fold cannot trust. A baseline body missing
fields fold relies on is discarded in favour of a full replay, and the fast
path reactivates once a fresh baseline is written. Better slow than wrong.

## Canonical rendering

"Byte-equal" needs a backstop, and that layer is **canonical**: rendering any
typed value into a stable byte form — JSON with sorted keys, compact
separators, UTF-8 throughout. Equivalent objects therefore render to exactly
the same bytes, and a hash of the same content is identical on any machine at
any time.

Two things lean on it directly. Content addressing uses it to deduplicate, and
baseline bodies round-trip through it so tagged value types (`ContentRef`, wake
conditions, subtask results, typed message blocks) keep their identity across
the boundary. The reproducibility of the whole event-sourced design rests on
this thin layer. (How recordings stay foldable as fields are added and removed
is covered in [state and writers](../architecture/state-and-writers.md).)

## When fold runs

Every wake, every inspect, and the start of every leased step.

A point-in-time read — "how did this Task stand as of seq N?" — folds through a
bounded read-only view of the same events rather than teaching fold a cap. Fold
itself only ever goes forward, and nothing on the stream is ever rewritten.

## Next

- [Wake & resume](wake-resume.md) — the delivery guarantee that makes the
  recovery above exactly-once.
- [Event sourcing](event-sourcing.md) — the log fold reads from.
- [Deploy a worker](../how-to/deploy-worker.md) — running the drain loop that
  performs the recovery in production.
- [Known limitations](../operations/limitations.md) — the edges of the recovery
  guarantee.
