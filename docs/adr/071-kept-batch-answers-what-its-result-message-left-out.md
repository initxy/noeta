# ADR-071: A kept tool batch is complete only when its result messages answer every call

- Status: accepted
- Date: 2026-10-26

## Context

Refines [ADR-065](065-crash-recovery-keeps-finished-tool-calls.md), which treats a kept batch whose tool-result message landed as finished: nothing is appended and the step continues. Two live paths write a result message that leaves a call out. A batch that meets an approval answers the other calls, then suspends on the approval; a crash between the two left the gated call unanswered and its suspend unwritten, so recovery asked the model with a dangling `tool_use` (a provider 400). A `Task` call that a mixed response defers until its ordinary calls finish is also missing from that message.

## Decision

- A kept batch counts as finished only when its landed result messages answer every `tool_use` of the decision. Otherwise the calls they leave out go through ADR-065's per-call rules, and the landed results are kept as they are, not answered again.
- So an approval whose suspend was lost is met again: the gated call never started, so it goes through the live guard chain, which suspends on the same approval (or denies or runs it, if the configuration changed). Nothing is re-run, and recovery needs no special case for it.
- A deferred `Task` call left out of the message is a control call that never started, so it gets the "not run, issue it again" result.

## Rationale

- The structural rule is one result per `tool_use`, and "a result message landed" does not show that. Checking coverage call by call reuses ADR-065's rules unchanged and needs no new event.

## Alternatives considered

1. **Re-suspend on the approval from the recorded `ToolCallApprovalRequested`.** Rejected: it adds a recovery path that knows about approvals, while the live guard chain already reaches the same suspend and applies any configuration change.
2. **Leave a deferred `Task` call to the next decide, which spawns it.** Rejected: a narrow crash window, and telling the model to issue it again is as safe as ADR-065's treatment of any control call cut short.

## Consequences

- `noeta.runtime.attempt` records which calls a landed message answers (`RecordedBatch.answered`); `plan_batch_completion` skips them.
