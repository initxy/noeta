# ADR-019: Tenant limits and providers are enforced by the kernel

- Status: accepted
- Date: 2026-10-28
- Refines: [ADR-007](007-tenants.md)
- Refined by: [ADR-022](022-the-model-catalog-is-kernel-data.md) (the model catalog stays in
  the kernel), [ADR-024](024-a-hold-takes-back-lapsed-claims-and-a-held-tree-stays-on-its-holders-queues.md)
  (the running limit counts live leases only),
  [ADR-025](025-every-model-call-checks-the-budget-before-it-is-made.md) (the budget is checked
  before every model call)

## Context

ADR-007 gives each tenant a running limit, a monthly token and cost budget and its own
provider, and the code (`noeta/core/tenants.py`, `Runtime.budget_refusal`, `Runtime.charge`,
`Runtime.provider` in `noeta/core/runner/runtime.py`) keeps all three in the kernel, with the
usage counted in a store table (`tenant_usage`) that is in no task's log. The kernel is meant
to own only the task log, scheduling and recovery, with every capability a plugin, and task
state only a fold of the log. So either these are scheduling or they belong behind a plugin
hook.

## Decision

They stay in the kernel, as scheduling of shared resources:

- The running limit is part of the claim (the store skips a tenant's tasks at its limit).
- Every model call goes through the kernel: the turn's call (`call_model`) and every
  extension's `TaskRun.complete` (summaries, judges) and token count. The kernel picks each
  call's provider by the task's tenant, checks the budget before the turn's call and counts
  every call's usage after it.
- `tenant_usage` is a per-tenant, per-month counter beside the logs, like the `tasks` index
  and leases: no task's fold reads it, and its only effect on a task, a refused call, is
  written to that task's log (`budget_spent` with the reason), so the log alone still replays
  the task.

## Rationale

A limit is only as good as the calls it sees. A plugin sees the turn's model request
(`before_model`) but not the calls other extensions make through `TaskRun.complete`, nor which
key a call is sent with; a tenant's own provider is a promise that no call of its tasks
borrows the server's key, which only the place every call passes through can keep. The same
holds for the running limit, which only the claim can make exact across workers.

The counter is not task state: it aggregates many tasks of a tenant, and no task's behavior
is derived from it except through a refusal the log records. It is not derivable from the
logs either (extension calls are not logged as steps), so it is kept, not recomputed.

Rejected: a budget plugin on `before_model` (misses extension calls and cannot choose the
key); summing usage from the logs at each call (extension calls are not in them, and it reads
every task of the tenant).

## Consequences

- Counting is best effort: a worker that dies between a reply and its count loses that
  count, and a call that starts under the budget may end over it (ADR-007).
- A store without the optional `tenant_usage` / `add_tenant_usage` members has no budgets.
