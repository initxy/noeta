# Architecture decisions

Each record holds one shipped decision that is costly to reverse: what was decided, why, and
which alternatives were rejected, so a rejected design is not proposed again. Terms are defined
in [CONTEXT.md](../../CONTEXT.md); how the pieces fit is the site's [How it works](../how-it-works/index.md).

Rules (`AGENTS.md`, enforced by `scripts/check-docs.sh`): files are `NNN-<slug>.md`; each has a
`- Status:` line reading `accepted` or `superseded by ADR-NNN`; a decision is never amended in
place — changing it is a new ADR that refines or supersedes it, and the old one names it.
Numbering restarted at 1.0; the 0.x records are in the repository history before `eff3ec4`
and describe a different codebase.

## Kernel

- [001](001-task-is-an-event-log-with-one-writer.md) A task is an append-only event log with one writer and an inbox
  (refined by 026, 031, 032)
- [031](031-background-plugin-work-returns-through-the-inbox.md) A plugin's background model work returns through the task's inbox
- [029](029-a-task-keeps-its-own-claim-while-it-owns-process-bound-work.md) A task keeps its own claim while it owns process-bound work
- [032](032-a-task-that-keeps-killing-its-worker-ends-at-a-restart-limit.md) A task that keeps killing its worker ends at a restart limit
- [006](006-fold-snapshots.md) Claims fold from versioned snapshots; a snapshot may be missing, never wrong
  (refined by 012, 023, 030)
- [012](012-snapshots-follow-state-size.md) Snapshots are written as the state grows and deleted when the task ends
  (refined by 023)
- [023](023-snapshots-per-fold-version-and-newer-logs-left-to-newer-workers.md) Snapshots are kept per fold version; a newer version's log is left to a newer worker
- [030](030-the-log-is-the-only-full-transcript.md) The log is the only full transcript; the fold keeps what the next step needs

## Extensibility and behavior

- [002](002-one-extension-interface-and-entry-points.md) Every capability is a plugin behind one Extension interface, found through entry points
  (refined by 017, 026, 033)
- [033](033-one-plugin-carries-its-front-end-parts-and-is-switched-per-task.md) One plugin carries its front-end parts and is switched per task
- [017](017-deferred-tool-loading-is-a-plugin.md) Deferred tool loading is a plugin; extensions change the tool list through tool_changes
  (refined by 030)
- [026](026-a-blocked-prompt-is-erased-by-a-core-record.md) A blocked prompt is erased by a core record, and plugin state follows it
  (refined by 030)
- [016](016-deferred-tool-loading-stays-in-the-kernel-for-now.md) Deferred tool loading stays in the kernel for now
  (superseded by 017)
- [003](003-claude-code-is-the-model-visible-contract.md) Claude Code's model-visible behavior is the contract; prompt text is our own words
  (refined by 020)
- [020](020-claude-code-wording-is-allowed-where-it-serves-parity.md) Claude Code's wording is allowed in prompt text where it serves parity
- [004](004-noeta-reads-only-its-own-config-places.md) noeta reads only its own configuration places, in Claude Code's formats

## Security and operation

- [028](028-no-tenants-in-noeta.md) noeta has no tenants; a host that needs them builds them on the extension points;
  the model catalog stays kernel data for output ceilings
- [008](008-sandbox-protected-path-placeholders.md) The sandbox keeps code-running paths read-only and covers missing ones with placeholders
- [009](009-serve-requires-a-token.md) The HTTP API always requires a bearer token
  (refined by 013, 028)
- [013](013-server-token-kept-as-a-digest.md) The server token is kept as a digest; clients bring it themselves
  (refined by 028)
- [010](010-nothing-uploaded-by-default.md) noeta sends nothing anywhere but to the configured model provider

## Superseded

- [005](005-holds-with-epochs-and-hold-lost.md), [011](011-one-holding-worker-per-tree.md),
  [015](015-a-held-tree-runs-only-on-its-holder.md),
  [024](024-a-hold-takes-back-lapsed-claims-and-a-held-tree-stays-on-its-holders-queues.md),
  [027](027-the-store-refuses-a-child-off-its-held-trees-queues-and-a-reclaim-counts-toward-the-limit.md)
  Holds of task trees on one worker (superseded by 029)

- [007](007-tenants.md), [014](014-tenants-separated-at-the-api-tools-need-an-os-boundary.md),
  [018](018-each-tenant-has-its-own-places-on-disk.md),
  [019](019-tenant-limits-are-enforced-by-the-kernel.md),
  [021](021-a-tenant-reads-all-but-a-protected-set-and-writes-only-its-own-places.md),
  [022](022-the-model-catalog-is-kernel-data.md),
  [025](025-every-model-call-checks-the-budget-before-it-is-made.md) Tenants, their fence,
  limits and budgets, and the catalog's place by way of the budget (superseded by 028)
