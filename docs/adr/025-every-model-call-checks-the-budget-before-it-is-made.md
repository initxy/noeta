# ADR-025: Every model call checks the tenant's budget before it is made

- Status: accepted
- Date: 2026-10-29
- Refines: [ADR-019](019-tenant-limits-are-enforced-by-the-kernel.md)

## Context

ADR-019 has the kernel check a tenant's budget "before the turn's call" and count every call
after it. The check sat after the step's request hooks, so a compaction's summary (made from
`before_model` or `/compact`) ran past a spent budget, and an extension's call through
`TaskRun.complete` was counted but never refused. A budget that only the turn's own call obeys
is not a limit on what the tenant spends.

## Decision

- The kernel checks the budget before every model call a task makes:
  - the turn's call, in `call_model` (`noeta/core/runner/model.py`), before the request hooks
    run, so a hook's own call (a compaction) is not made either; a refusal fails the turn with
    `budget_spent`, as before;
  - every extension call (`TaskView.complete`), in `TaskRun.complete`
    (`noeta/core/runner/task.py`), which raises `BudgetSpent` (`noeta/core/errors.py`, a
    `Fatal`) without calling the provider. An extension handles it as any other
    non-retryable model error.
- Usage is counted as soon as a call returns, before its reply is appended, so a call whose
  reply is never logged (a lost claim, an empty reply) is still counted.
- A token count asked of the provider is not a model call: it is neither checked nor counted.

## Rationale

The check has to sit where every call passes, which is the same reason ADR-019 keeps the
budget in the kernel; checking before the hooks covers the calls the hooks make without each
hook knowing about budgets. A typed `Fatal` lets an extension tell "the call was refused" from
a provider failure while needing no new handling to stop retrying.

Rejected: checking in each extension that calls a model (a third-party one would forget, and
the kernel could not promise the limit); returning an empty reply from `complete` past the
budget (an extension would act on a summary or judgment that was never made).

## Consequences

- An extension that calls `TaskView.complete` must expect `BudgetSpent`; compaction treats it
  as a failed summary and fails the turn.
- Counting stays best effort (ADR-019): calls already running when the budget runs out still
  finish and are counted, so a tenant can end a month somewhat over its budget.
