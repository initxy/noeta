---
status: done
created: 2026-10-29
branch: os-drop-tenants
---

# Drop tenants

## Goal

Remove the tenants feature entirely: an open-source Claude Code serves one party per store and
server, and a host that needs to partition users builds it on the extension points.

## Non-goals

Changing the sandbox, holds, queues, snapshots, takeover or the single bearer token beyond
removing what only tenants used.

## Context

Owner decision, 2026-10-29: tenants (added before 1.0, never released) are removed; "if
someone wants to build on it, they extend it themselves". It reverses the same day's "tenants
stay" in `docs/specs/2026-10-29-open-source-follow-ups.md`.

## Decisions

- D1 Schema version 4 drops `tasks.tenant`, its index and `tenant_usage` on open (sqlite below
  3.35 keeps the unread column). Older binaries refuse a version 4 store.
- D2 `TaskCreated.tenant` is gone; old logs still decode (msgspec ignores the unknown field).
- D3 One token: `create_app(token=)` or `create_app(token_sha256=)`; `noeta token` and
  `~/.noeta/tokens.json` / `~/.noeta/tenants/` are gone; `[server]` settings and
  `noeta worker --server-dir` existed only to hide the server from tenants and are gone.
- D4 No fence: `TenantFence`, `LocalExecEnv(fence=, temp_dir=)`, the tenant sandbox fields and
  `run_in_tenant_sandbox` go; callers run through `env.run` as for every other task.
- D5 No budgets (`budget_spent`, `BudgetSpent`) and no per-tenant provider; the runtime's
  `provider_for` serves every task.
- D6 The bubblewrap / Seatbelt sandbox stays: it is Claude Code's `sandbox` setting.
- D7 `Mcp(scope=)` partitions stay (host code's own, from 0.6); only the `tenant:` prefix goes.
- D8 ADR-028 supersedes ADR-007, 014, 018, 019, 021, 022, 025 (022 named a removed module;
  028 restates its placement) and refines 009, 013, 024, 027.

## Plan

- [x] Kernel: events, state, store protocol, client, runner, runtime, errors, texts.
- [x] Stores: sqlite and Postgres columns, claim, usage; migration to version 4 with tests.
- [x] Toolkit, exec envs and sandbox: fence, tenant temp dirs, tenant sandbox fields.
- [x] Plugins: permissions, MCP, memory, skills, compaction, fs, environment, workflow,
  checkpoints, shell, schedule.
- [x] Host and CLI: HTTP guard, routes, stream-json, ACP, TUI, `noeta serve` / `worker`,
  `noeta token`, config.
- [x] Tests and store contract; docs (CONTEXT, architecture, server, README, CHANGELOG, ADRs).

## Acceptance criteria

- `grep -ri tenant noeta tests` finds only the version 4 migration and the classifier prompt's
  own wording about cloud tenants.
- `make check` green; `make parity` 0; `make cluster` passes; store tests pass on Postgres.

## Risks

- A running older worker writing to a store just moved to version 4 fails its inserts: stop
  every worker before upgrading (as for every schema change, `docs/server.md`).

## Progress log
- 2026-10-29 Removed on `os-drop-tenants`: make check 2424 passed / 60 skipped (88.40%); with Postgres 16 2484 passed; `make parity` 0 mismatches; `make cluster` 13/13. No separate reviewer was run (the work was done by a worker that may not start sub-agents); self-reviewed, which added the 404 for an unknown task's `/events` and `/pending` (the scoped client gave it before).
