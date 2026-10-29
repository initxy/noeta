# ADR-021: A tenant reads all but a protected set and writes only its own places

- Status: superseded by ADR-028
- Date: 2026-10-29
- Refines: [ADR-014](014-tenants-separated-at-the-api-tools-need-an-os-boundary.md),
  [ADR-018](018-each-tenant-has-its-own-places-on-disk.md)

## Context

ADR-014 fences a non-default tenant's file tools off from a fixed set of places (noeta's home,
the store's files, `/etc/noeta`, `/proc`, other tenants' workspaces) and ADR-018 adds its own
temp and project directories. That left the server's own directory (with its `.env`) and the
credential stores in home directories readable. A first fix turned the fence around into an
allow list (the workspace, the tenant's own corners and a few system paths) and hid every
home directory from commands; it broke ordinary work, since tenants' inputs live in `/opt`,
`/srv`, `/var`, data disks and `/tmp`, and the goal is reasonable protection, not a jail.

## Decision

- **Reads**: a non-default tenant's file tools and sandboxed commands read what the OS user can,
  but a protected set: noeta's home but the task's own corners, the store's files, `/etc/noeta`,
  `/proc` (a command gets its own), noeta's temp places but the tenant's own, other tenants'
  workspaces, the server's own directories (the worker's cwd, serve's cwd and `--allow-root`;
  a workspace inside one stays the tenant's), the sandbox's credential stores
  (`DEFAULT_DENY_READ` in `noeta/toolkit/sandbox.py`) under every home directory, and the
  operator's `denyRead` (with `Read(...)` deny rules and `credentials` files), whether or not
  the sandbox is on.
- **Writes**: only the tenant's workspace, the task's own corners (ADR-018) and the tenant's temp
  directory; the operator's `additionalDirectories`, `allowWrite` and `allowRead` are not the
  tenant's.
- As before, the check runs ahead of every rule, mode and approval, and again on the file
  actually opened (`noeta/toolkit/tenant_fence.py`).

## Rationale

What a tenant must not reach is noeta's own state, other tenants' work, the server's
directories and credentials; a list of those is short and stays right as the machine changes,
while an allow list has to guess where every deployment keeps its data. Credential stores are
what matters in a home directory, so those are hidden, not whole homes.

Rejected: the system-path allow list (`/opt`, `/srv`, `/var`, data disks, `/tmp` are where
tenants' inputs live); hiding whole home directories; matching credential names anywhere on
disk (a project's own `.npmrc` is the tenant's).

## Consequences

- Credentials kept under other names or outside home directories are readable unless the
  operator's `denyRead` names them.
- Files the OS user can read outside the protected set are shared by all tenants;
  `docs/server.md` ("What separates tenants") says so.
