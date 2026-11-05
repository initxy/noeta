# ADR-031: A plugin's background model work returns through the task's inbox

- Status: accepted
- Date: 2026-11-05
- Refines: [ADR-001](001-task-is-an-event-log-with-one-writer.md)

## Context

Precomputed compaction (Claude Code's) writes a summary while the task keeps working, and
swaps it in later. noeta's task has one writer and every request is a function of the folded
log, so a result computed beside the step cannot be written by whoever computed it, and a
step must never depend on work held only in a process's memory. The title plugin already ran
one side request this way; compaction is the first whose result decides later requests.

## Decision

- A plugin starts the background call from a hook (the step's own state and request,
  captured then) and records only a random job id with it; the call itself lives in the
  plugin instance's memory and decides no bytes.
- The outcome, with what the calls cost, is posted to the task's own inbox as an `External`
  signal; the runner takes it at its next boundary like any input, and the plugin's
  `on_event` records it (`ExtensionRecorded`, the cost as `core` `spent`) when it names the
  job the log says is running; any other (outdated, or posted by someone else) leaves only
  its cost. From then on it is ordinary folded state.
- A recorded result names what it was computed from (for a summary: the compaction count and
  a digest of the messages it covers); the step that would use it checks that against the
  current state and ignores it when they differ. A lost call (its process died) is simply
  started again.
- Two small kernel facts support such work: `TaskState.model_called_at` (when the newest
  model call was logged) and `TaskView.next_request` (`Runtime.preview`'s request, for work
  between turns).

## Rationale

The inbox is already the one way in (ADR-001): ordered, deduplicated, taken by the claim
holder only, delivered to whichever worker holds the task when the call finishes; the job id
in the log lets that worker tell the result apart from a stale or forged signal, so no
other `External` of that name can swap a summary in. Validating on use, not on
arrival, makes a result outdated by a later change harmless and needs no cancellation
protocol. A restart loses at most the call in flight; every decided step folds the same.

Rejected: recording the result from the background coroutine (a second writer, or a write
on a stale claim); holding the result in memory until the next step records it (lost when the
task moves to another worker, and a step would depend on process memory); keeping the claim
while the call runs (ADR-029 is for work that cannot be redone; a summary can).

## Consequences

- A result posted to a waiting task wakes it once, to be recorded.
- A crash between taking the signal and recording it loses that result (and its cost record);
  the plugin starts another.
- The result's bytes appear twice in the log (the inbox item and the record).
