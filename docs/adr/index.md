# Architecture decisions

Each record holds one shipped decision that is costly to reverse: what was decided, why, and
which alternatives were rejected, so a rejected design is not proposed again. Terms are defined
in [CONTEXT.md](../../CONTEXT.md); how the pieces fit is [docs/architecture.md](../architecture.md).

Rules (`AGENTS.md`, enforced by `scripts/check-docs.sh`): files are `NNN-<slug>.md`; each has a
`- Status:` line reading `accepted` or `superseded by ADR-NNN`; a decision is never amended in
place — changing it is a new ADR that refines or supersedes it, and the old one names it.
Numbering restarted at 1.0; the 0.x records are in the repository history before `eff3ec4`
and describe a different codebase.

## Kernel

- [001](001-task-is-an-event-log-with-one-writer.md) A task is an append-only event log with one writer and an inbox
- [005](005-holds-with-epochs-and-hold-lost.md) Process-bound work holds its task tree, with epochs and a hold_lost notice
  (refined by 011)
- [006](006-fold-snapshots.md) Claims fold from versioned snapshots; a snapshot may be missing, never wrong
  (refined by 012)
- [011](011-one-holding-worker-per-tree.md) A task tree has at most one holding worker
  (refined by 015)
- [015](015-a-held-tree-runs-only-on-its-holder.md) A held tree's tasks run only on its holder
- [012](012-snapshots-follow-state-size.md) Snapshots are written as the state grows and deleted when the task ends

## Extensibility and behavior

- [002](002-one-extension-interface-and-entry-points.md) Every capability is a plugin behind one Extension interface, found through entry points
  (refined by 017)
- [017](017-deferred-tool-loading-is-a-plugin.md) Deferred tool loading is a plugin; extensions change the tool list through tool_changes
- [016](016-deferred-tool-loading-stays-in-the-kernel-for-now.md) Deferred tool loading stays in the kernel for now
  (superseded by 017)
- [003](003-claude-code-is-the-model-visible-contract.md) Claude Code's model-visible behavior is the contract; prompt text is our own words
  (refined by 020)
- [020](020-claude-code-wording-is-allowed-where-it-serves-parity.md) Claude Code's wording is allowed in prompt text where it serves parity
- [004](004-noeta-reads-only-its-own-config-places.md) noeta reads only its own configuration places, in Claude Code's formats

## Security and operation

- [007](007-tenants.md) Tasks belong to a tenant; a server token selects it and nothing crosses it
  (refined by 014, 019)
- [014](014-tenants-separated-at-the-api-tools-need-an-os-boundary.md) Tenants are separated at the API; tools need an OS boundary
  (refined by 018)
- [018](018-each-tenant-has-its-own-places-on-disk.md) Each tenant has its own places on disk, and a workspace it must be given
- [019](019-tenant-limits-are-enforced-by-the-kernel.md) Tenant limits and providers are enforced by the kernel
- [008](008-sandbox-protected-path-placeholders.md) The sandbox keeps code-running paths read-only and covers missing ones with placeholders
- [009](009-serve-requires-a-token.md) The HTTP API always requires a bearer token
  (refined by 013)
- [013](013-server-token-kept-as-a-digest.md) The server token is kept as a digest; clients bring it themselves
- [010](010-nothing-uploaded-by-default.md) noeta sends nothing anywhere but to the configured model provider
