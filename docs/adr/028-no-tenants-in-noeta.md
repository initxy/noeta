# ADR-028: noeta has no tenants; a host that needs them builds them on the extension points

- Status: accepted
- Date: 2026-10-29
- Refined by: [ADR-034](034-the-server-token-file-holds-the-token-and-a-request-may-name-any-directory.md) (the token file holds the token)
- Supersedes: [ADR-007](007-tenants.md), [ADR-014](014-tenants-separated-at-the-api-tools-need-an-os-boundary.md),
  [ADR-018](018-each-tenant-has-its-own-places-on-disk.md),
  [ADR-019](019-tenant-limits-are-enforced-by-the-kernel.md),
  [ADR-021](021-a-tenant-reads-all-but-a-protected-set-and-writes-only-its-own-places.md),
  [ADR-022](022-the-model-catalog-is-kernel-data.md),
  [ADR-025](025-every-model-call-checks-the-budget-before-it-is-made.md)
- Refines: [ADR-009](009-serve-requires-a-token.md), [ADR-013](013-server-token-kept-as-a-digest.md),
  [ADR-024](024-a-hold-takes-back-lapsed-claims-and-a-held-tree-stays-on-its-holders-queues.md),
  [ADR-027](027-the-store-refuses-a-child-off-its-held-trees-queues-and-a-reclaim-counts-toward-the-limit.md)

## Context

noeta is an open-source Claude Code for servers. Tenants (a task's owner, per-tenant tokens,
workspaces, queues, running limits, monthly budgets and providers, and a file fence between
tenants sharing one OS user) were added before 1.0 and never released. They reached into the
kernel (the event, the store's columns and claim, the runner's provider and budget), every
plugin that touches files, the sandbox and the HTTP layer, for a need Claude Code does not
have. The owner decided they do not belong in noeta.

## Decision

- A store and a server serve one party. `TaskCreated` has no tenant, the stores no
  `tasks.tenant` column or `tenant_usage` table (schema version 4 drops them), `Store.claim`
  no `tenant_limits`, `Client` no `scoped`, `Runtime` no `tenants`, `CommandContext` no
  `tenant`.
- `noeta serve` takes one bearer token, kept as a digest (ADR-009, ADR-013);
  `create_app(token=)` or `create_app(token_sha256=)`. There is no `noeta token` command and
  no `~/.noeta/tenants/`.
- No tenant fence: no `noeta.toolkit.tenant_fence`, no `LocalExecEnv(fence=)`, no tenant
  fields in `SandboxSettings`. The sandbox (Claude Code's `sandbox` settings) stays: it
  confines a task's commands, as Claude Code's does.
- No budgets. The model catalog (`noeta/core/models.py`) stays kernel data: the kernel caps
  every call's output from it (`catalog_output_ceiling` in `noeta/core/runner/runtime.py`)
  and imports nothing above it, so ADR-022's placement holds without the budget.
- Holds and queues are unchanged (ADR-024, ADR-027) without the running limit they also
  served.
- A host that needs to partition users does it with what is already pluggable: one store
  schema or one server per party, queues and workers per party, `Mcp(scope=)` for MCP
  connections, an exec env per task (a container), and an `Extension` for gates, limits and
  accounting.

## Consequences

- Thousands of lines of code and tests and seven ADRs fewer; the kernel and the plugins no longer
  carry a party they never act on.
- A development store at schema version 3 is moved to version 4 in place on first open; on
  sqlite older than 3.35 `tasks.tenant` stays, unread. An older binary refuses a version 4
  store.
- Separating parties that share one OS user is the host's job (separate users, machines or
  containers), not something noeta claims to do.

Rejected: keeping tenants as an optional plugin. The kernel parts (the claim's limit, the
provider per party, scoping every read) cannot be added from a plugin without new kernel seams
that only this feature would use.
