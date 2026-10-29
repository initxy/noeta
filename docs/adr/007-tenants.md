# ADR-007: Tasks belong to a tenant; a server token selects it and nothing crosses it

- Status: superseded by ADR-028
- Date: 2026-10-28
- Refined by: [ADR-014](014-tenants-separated-at-the-api-tools-need-an-os-boundary.md)
  ("nothing crosses it" holds for the API; tools need an OS boundary),
  [ADR-019](019-tenant-limits-are-enforced-by-the-kernel.md) (limits and providers are
  kernel scheduling)

## Context

One server and one store may serve several teams or customers. Without a boundary, any
token holder reads every task, a task's `SendMessage` reaches any conversation in the store,
and one heavy user can take every worker and spend the whole model budget.

## Decision

- A task carries its tenant (`TaskCreated.tenant`, default `default`); children inherit it.
- A client scoped to a tenant (`Client.scoped`) and a task's own posts and task lists reach
  only that tenant's tasks; another tenant's task is unknown. Unscoped host code sees the
  whole store.
- On `noeta serve`, a request's bearer token picks its tenant: the server token is
  `default`'s, more come from `noeta token add --tenant` and are stored as digests only.
- Per-tenant settings (`noeta/cli/tenants.py`, limits in `noeta/core/tenants.py`): a workspace
  bounding its tasks' `cwd`, a running limit enforced by the claim across all workers, a
  monthly token and cost budget enforced at the model call, and its own provider.

## Rationale

Making the tenant a fact in the log means every reader, the claim and the budget see the same
boundary, on every machine, with no session state. Mapping tokens to tenants keeps a
single-tenant server unchanged (one token, the default tenant) while allowing more without a
user database. Enforcing the running limit in the claim makes it exact across workers.

Rejected: separate stores per tenant as the only option (heavy for small deployments; still
available for hard isolation); tenant as a request header (trusts the caller); filtering only
in the HTTP layer (a task's own `SendMessage` would cross the boundary).

## Consequences

- The workspace bound is enforced where tasks come in (HTTP), not by the worker; host code
  with an unscoped client can still set any `cwd`.
- A budget counts model calls from when it is set; a call that starts under it may end over.
- `docs/server.md` documents the deployment.
