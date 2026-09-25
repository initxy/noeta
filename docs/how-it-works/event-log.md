# The event log

Noeta never stores a Task's current state. It appends everything that happens to
the Task's own **EventLog** and rebuilds state from that log on demand:

> state = fold(the Task's events)

The log is the master copy; the state object is a throwaway projection. Crash
recovery, replay and audit are all the same operation — a fold.

<NtEventLog />

## What it guarantees

- **Byte-equal replay.** The same log folds to byte-identical state in any process
  on any machine. Fold reads only the log and the content store — no clock, no
  randomness, no network, no model calls.
- **Nothing is edited in place.** Corrections, rewinds and compaction are new
  events. The original records stay on the stream.
- **One writer per Task.** Every append presents the worker's lease; a worker
  whose lease was reclaimed is rejected at the append.
- **Snapshots are only a speed-up.** Delete every snapshot and behaviour is
  unchanged, just slower.

## What the log holds

A typical stream, one line per record:

| `seq` | type | records |
| --- | --- | --- |
| 1 | `TaskCreated` | goal, agent name, parent task |
| 2 | `MessagesAppended` | the user's message |
| 3 | `ContextPlanComposed` | a reference to exactly what the model was shown |
| 4 | `LLMRequestFinished` | the model's reply and token usage |
| 5 | `ToolCallStarted` | e.g. `Read(file_path="README.md")` |
| … | | the loop continues |
| 41 | `TaskSuspended` | the Task is waiting |
| 42 | `TaskWoken` | what it waited for arrived |
| 58 | `TaskCompleted` | the final answer |

Each record is an `EventEnvelope` carrying `seq` (assigned by the log at append
time), `type`, `actor`, `origin` (`engine` / `llm` / `observer` / `tool` /
`system`) and a small payload. Payloads are capped at 4 KB
(`EVENT_PAYLOAD_MAX_BYTES`); anything larger — a full model response, a big tool
output, a snapshot body — goes to the content-addressed **ContentStore**, and the
event carries a `ContentRef` to it.

## Four slices, changed only by fold

Task state is split into four typed slices. Only fold changes them, so nothing
can change state without leaving an event behind:

| Slice | Changes come from | Holds |
| --- | --- | --- |
| `RuntimeState` | what the Engine records | rolling messages, per-turn usage |
| `TaskState` | Policy, only through a `TaskStatePatch` on its decision | goal, phase, todos, active content |
| `ContextState` | the composed context the Engine records | context plan ref, compaction summary, content anchors |
| `GovernanceState` | accumulated from the whole stream | cost, iteration and token counters, subtask results |

The policy decides what to change, but the Engine records it as a
`TaskStatePatched` event and fold applies it. Deciding and recording are two
separate rights held by two separate components.

## Recovering from a crash

<NtRecovery />

1. Worker A is killed mid-step. Its heartbeat stops and its lease expires.
2. The stale sweep puts the Task back on the ready queue.
3. Worker B leases it and folds the log — which every step does anyway.
4. B seals the interrupted attempt with a `StepAttemptAbandoned` event. If the
   guards would allow every tool call in that attempt without approval, the step
   is re-run automatically — and the re-run may repeat a call that had already
   run. If any call would need approval or be denied, the attempt spawned a
   subtask, or it names an unknown tool, the Task is parked for a human to
   resume. Three seals in a row always park, so a crash loop cannot retry
   forever.

There is no recovery code path beyond this, because there was never anything to
"save". The edges of the guarantee are in
[Known limitations](../operations/limitations.md).

## Snapshots

Replaying a long log from the top gets slow, so fold also restores from the
newest **baseline** event and replays only the tail after it:

| Baseline | Written when |
| --- | --- |
| `TaskSnapshot` | before every suspend and terminal event, and every 20 consecutive tool-call turns |
| `TaskRewound` | the conversation was rewound to an earlier turn |
| `StepAttemptAbandoned` | an interrupted attempt was sealed |
| `TaskForked` | a new Task branched off this one |

The test suite folds with `ignore_snapshots=True` and checks both paths produce
the same bytes. A snapshot that lacks fields today's fold needs is discarded in
favour of a full replay: slower, never wrong. The same rules keep a Task
suspended six months ago foldable under today's code.

## What this means for you

- Read a Task's full history with `Client.events` and its messages with
  `Client.messages`; that is the same data recovery uses.
- Any process that can read the store can resume any Task, so scaling out is a
  storage choice, not a code change.
- Hooks that need to *change* what happens belong in a policy or a guard, never
  in an observer — see [The engine](engine.md).

Design records:
[event-sourced truth](https://github.com/initxy/noeta/blob/main/docs/adr/event-sourced-truth.md) ·
[single-writer invariant](https://github.com/initxy/noeta/blob/main/docs/adr/single-writer-invariant.md) ·
[step-attempt recovery](https://github.com/initxy/noeta/blob/main/docs/adr/step-attempt-recovery.md)

## Next

- [Tasks and waking](tasks-and-waking.md) — how a Task waits and resumes exactly once.
- [The engine](engine.md) — what writes the events.
- [Deploy](../guides/deploy.md) — run the workers that do the recovery.
