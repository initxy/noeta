# ADR-065: Crash recovery keeps a recorded tool batch: finished calls keep their results, a call cut short gets an unknown-outcome result, and nothing that started runs again

- Status: accepted
- Date: 2026-10-26

## Context

Refines [ADR-032](032-step-attempt-recovery.md) (a mid-step crash is recovered per attempt: seal, then re-drive when the guard chain proves it safe, otherwise park). Where the two disagree, this record wins.

Under ADR-032 the unit of recovery is the whole interrupted attempt. A crash in the middle of a tool batch seals the attempt — re-basing to before its `ContextPlanComposed` — and the re-drive asks the model again. The model sees none of the batch, so it may issue the same calls, and the ones that already finished run a second time. The classifier was meant to stop exactly that, but its rule is "would this call run without a human", and in a gate-free permission mode (`bypassPermissions`, which production hosts run) every call passes: a finished `Bash`, `Write` or `git push` was re-run silently. A note telling the re-driven model that a call "ran, but its result could not be read" softened this without closing it.

## Decision

- **A recorded tool batch is kept, not re-decided.** The batch is recorded when the interrupted attempt carries the assistant message with its `tool_use` blocks and the batch got far enough to matter — at least one `ToolCallStarted`, or its tool-result message already landed. Recovery then does not seal. The decision, the decision's state patch, and every recorded result stay live history, and each call gets exactly one result in one batched message:
  - a call with a recorded `ToolResultRecorded` keeps that result, re-rendered the way the live batch renders it (inline limit, spill file, the tool's size cap), and is never run again;
  - a call that started without a recorded result gets a failed result saying a crash interrupted it and its outcome is unknown, to check its effect before running it again — it is never run again;
  - a call with a recorded `ToolCallDenied` keeps the recorded reason;
  - a call that never started runs now through the live guard chain, exactly as it would have — so it may be denied, or suspend the task on its approval — provided a plain tool answers to its name. A control tool or a routing tool (`route_call`) was translated by the Policy, which recovery does not re-enter, so such a call gets a failed result saying it did not run and should be issued again if still needed. An unknown name gets the same. When a stop is already armed — a forced stop recovers the step it abandoned this way — the never-started calls are closed unrun, as a stop closes any batch.
  After that the step continues at its next decide. When the tool-result message already landed, nothing is appended; the step just continues.
- **Everything else is ADR-032 unchanged.** An attempt with no recorded batch — the crash fell in the LLM call, or between the decision and its first call, so nothing ran — is sealed and re-driven, which re-decides at no side-effect cost. A spawn in the tail (`SubtaskSpawned`, `BackgroundSubagentStarted`) still seals and parks: a child is a side effect, and the classifier fails closed on it. An interrupted approval execution still parks on its own approval handle. The classifier, the seal, the park and its completion are untouched.
- **Classification has nothing left to park in a kept batch.** It existed to stop a re-drive from repeating side effects, and a kept batch repeats none: what started is never re-run, and what never started meets the same guard chain it would have met live. A guard configuration that changed since the crash is therefore handled by the live chain itself (a deny is a denial result, an approval requirement is a suspend), not by a park.
- **The crash-loop cap counts kept batches too.** A kept batch leaves no seal, so the cap of three now counts the seals in the window plus the earlier live attempts in it that hold a `ToolCallStarted` without a recorded result — each one a crash the batch was resumed across. Past the cap the attempt is sealed with `abandon_cap` and parked, as before. This is what stops a model that keeps re-issuing a call that kills the worker. A crash that loses no call (between calls, or after the result message) cannot repeat at the same point, because the resumed batch moves past it.
- **No new event type.** The batch is read back from events every live batch already writes: the assistant `MessagesAppended`, `ToolCallStarted`, `ToolResultRecorded`, `ToolCallDenied`, and the batched result `MessagesAppended`. The synthesized results ride that last message. A crash during recovery itself re-enters with more of the batch recorded, so recovery recurses without any marker of its own.

## Rationale

- **The duplicate side effect is the hazard, and the stream already proves which calls ran.** `ToolCallStarted` is written before a tool body runs and `ToolResultRecorded` after it, so the stream separates "finished, here is the result", "started, outcome unknown", and "never started". Re-deciding throws that knowledge away and lets the model reproduce the side effect; keeping the batch turns it into the conversation's own record.
- **An unknown outcome belongs in the tool result, where the model reads results.** It is the truthful answer to that `tool_use`, it costs no prompt text on any turn without a crash, and it replaces the separate re-drive note, which only hinted at what the tool result now states.
- **A never-started call has no side effects to protect.** Running it through the live guard chain gives it exactly the treatment it would have had, so recovery is never more permissive than live execution — the property ADR-032's classifier protected — and the model does not spend a round re-issuing it.
- **Keeping the decision also keeps determinism.** The kept path appends only ordinary events through the ordinary handler, so fold, replay, and the snapshot-accelerated fold agree as they do for any batch.

## Alternatives considered

1. **Keep ADR-032's per-attempt seal and widen the classifier** (treat every finished non-read-only call as needing a human). Rejected: every mid-batch crash in a production host would park for a human, for a state the stream fully explains.
2. **Seal, but replay the decision on the re-based state.** Rejected: the seal discards the decision's state patch and assistant message along with the results, so the replay has to rebuild what the stream already holds, and a second crash would face the same problem again.
3. **Record the Policy's whole `ToolCallsDecision` (routed calls and pre-answered control results) as a new event, so recovery can rerun every never-started call exactly.** Rejected for now: a new event type in every recorded batch, for the narrow case of a control or routing call cut off before it started, which the model can simply re-issue.
4. **Answer never-started calls without running them.** Rejected: running them through the live guard chain is exactly as safe and saves the model a round.

## Consequences

- Runtime: reading the batch back and sorting its calls live in `noeta.runtime.attempt` (`scan_recorded_batch`, `plan_batch_completion`, the cap's `resumed_count`); the resume continuation lives in `noeta.runtime.worker` (`_resume_recorded_batch`, reliability kind `attempt_resumed`); appending the batch's results and running the never-started calls goes through `handle_tool_calls` via `complete_interrupted_tool_batch` in `noeta.core.engine`.
- A Policy that returns `tool_calls` without an assistant message records no batch, so its interrupted attempts still take ADR-032's seal-and-re-drive path.
- The kept results come first in the batched message, then the results of calls run during recovery; every `tool_use` still has exactly one result.
- A recorded result that carried images loses them on re-render: `ToolResultRecorded` does not record images.
- Post-tool content discovery runs only for calls executed during recovery, not for results kept from before the crash.
- Recovery still compensates nothing: a call cut short may have partly applied, and its result tells the model to check before retrying.
