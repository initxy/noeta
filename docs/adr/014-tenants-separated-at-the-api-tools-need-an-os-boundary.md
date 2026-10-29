# ADR-014: Tenants are separated at the API; tools need an OS boundary

- Status: superseded by ADR-028
- Refined by: [ADR-018](018-each-tenant-has-its-own-places-on-disk.md)
  (per-tenant project and temp directories; a workspace is required),
  [ADR-021](021-a-tenant-reads-all-but-a-protected-set-and-writes-only-its-own-places.md)
  (the file fence becomes a protected set for reads and the tenant's own places for writes)
- Date: 2026-10-28
- Refines: [ADR-007](007-tenants.md)

## Context

ADR-007 makes the tenant a fact in the log and confines every API request and a task's own
posts to it. Tools are another matter: they run in the worker, as the worker's OS user. A
tenant whose task runs `Bash` (approved by that same tenant) could read `~/.noeta` (tokens,
the sqlite store, other tenants' settings), the worker's environment (provider keys) and any
file that user can; a file tool, a skill's script, `git` hooks or a local MCP server could do
the same. So "nothing crosses it" holds for the API only.

## Decision

- **Start**: `noeta serve` (with its worker) and `noeta worker` refuse to start when tenants
  besides the default one are known (tokens file or tenant directories), unless the sandbox
  confines every command (`enabled`, `failIfUnavailable`, no `allowUnsandboxedCommands`) or
  `[server] shared_os_user_tenants = true` accepts API-only separation.
- **Each task**: a worker set up neither way fails a non-default tenant's turn with
  `tenant_not_isolated` before any model call and denies its pending calls, whether or not it
  knows of tenants.
- **File tools** of a non-default tenant's task are fenced off from noeta's home, the store's
  files, `/etc/noeta`, `/proc` and other tenants' workspaces (`noeta/toolkit/tenant_fence.py`),
  whatever the deployment: a permissions-gate deny ahead of every rule, mode and answer, and
  again on the file actually opened. The task keeps its own corners (its spill directories,
  `plans/<tenant>/`, its `projects/<slug>`, read access to `skills/` and `workflows/`).
- **Commands**: under the sandbox, every program a tool runs that can run the tenant's code
  (`Bash`, a skill's script or inline command, `git` with its hooks, a rewind's `rm`) runs in
  the tenant's confined profile (`TenantFence.confine`): the fenced places unreadable, no
  `excludedCommands`, no way out, a fresh `/proc`.
- **MCP**: such a task gets no server that runs a process on the worker (a `command`, a
  `headers_helper`), whoever configured it; remote servers run in the tenant's own connection
  partition, so no connection or OAuth sign-in is shared.

`docs/server.md` ("What separates tenants") lists, tool by tool, what is and is not a
boundary.

## Rationale

Tenants that share an OS user share everything that user can reach, so API separation alone
is honest only when the operator says the tenants trust each other. Refusing at start makes
the common mistake loud; refusing per task covers a worker that does not know of tenants
(another machine, `serve --no-worker`). The file fence does not depend on the sandbox because
file tools run in-process, where no OS sandbox applies.

Rejected: skipping such tasks at the claim (needs a tenant filter in every store's claim, and
a pool with no set-up worker would leave them pending with nobody told); running stdio MCP
servers in the task's sandbox profile (one process per server and scope is shared across
tasks and would outlive the profile it started under).

## Consequences

- With `shared_os_user_tenants = true`, tenants trust each other and the operator with
  everything the OS user can reach; without the sandbox only the file tools' fence stays.
- A non-default tenant's task cannot use local MCP servers or make worktrees under `.noeta/`
  (read-only in the sandbox).
- Programs that run outside the sandbox by design (hooks and MCP servers of the default
  tenant, the operator's own) are not a tenant boundary.
