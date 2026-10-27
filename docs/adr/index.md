# docs/adr/ — Architecture Decision Records

This directory holds Noeta's **Architecture Decision Records (ADRs)**: each file captures one stable, cross-module decision — **what the decision is, why the system is shaped that way, and why the alternatives are rejected**. The audience is any agent about to change this code. Before you touch a subsystem, read the matching decision file: it names the constraint the current shape protects and the paths that are ruled out, so a rejected design does not get proposed a second time.

Files are numbered `NNN-<slug>.md` in the order the decisions were made. ADRs 001–031 carry 2026-07-01, the repository's import date, not the decision date; their order within that day is alphabetical. Each carries a one-line status — `accepted`, or `superseded by ADR-NNN` — and a decision is never amended in place: changing it means writing a new ADR that refines or supersedes the old one. A record refined by later ones lists them under `Refined by`; read those too.

## Division of labor with CONTEXT.md

- **`docs/adr/`** (this directory): **why the system is this way**. One decision per file, containing only "why it is this way / why the alternatives are rejected."
- **`CONTEXT.md`**: a glossary that pins down what a term means in this repository.
- **Nearby docstrings**: local rationale that affects only a single file or function lives in that docstring, not here.

Rule of thumb: the wider the impact (spanning multiple modules), the more it belongs in `docs/adr/`; the narrower it is, the closer it should sit to the code itself.

## The decisions

**Foundations** — the invariants everything else is built on:
[event-sourced-truth](009-event-sourced-truth.md) ·
[task-as-only-primitive](024-task-as-only-primitive.md) ·
[single-writer-invariant](019-single-writer-invariant.md) ·
[storage-protocols-l0](021-storage-protocols-l0.md) [+059](059-content-retention-outside-the-protocol.md) ·
[provider-neutral](016-provider-neutral.md) ·
[doc-code-link-direction](006-doc-code-link-direction.md)

**Engine & execution**:
[engine-policy-dataflow](007-engine-policy-dataflow.md) ·
[engine-per-turn](046-engine-per-turn.md) ·
[worker-lease-model](029-worker-lease-model.md) ·
[worker-queue-routing](041-worker-queue-routing.md) [+057](057-queue-recovery-reads-tails-for-waiting-parents.md) ·
[step-attempt-recovery](032-step-attempt-recovery.md) [+065](065-crash-recovery-keeps-finished-tool-calls.md) ·
[mid-turn-goal-injection](040-mid-turn-goal-injection.md) ·
[terminal-answer-on-a-parked-turn](045-terminal-answer-on-a-parked-turn.md) ·
[interrupt-responsiveness](042-interrupt-responsiveness.md) [+058](058-sandbox-foreground-shell-interrupt.md) ·
[multi-host-lease-fencing](034-multi-host-lease-fencing.md) [+056](056-lease-fencing-dropped-connections.md) ·
[subtask-fanout-and-durable-wake](022-subtask-fanout-and-durable-wake.md) ·
[subtask-parallel-execution](023-subtask-parallel-execution.md) ·
[background-subagent](002-background-subagent.md) ·
[transport-neutral-fanout](027-transport-neutral-fanout.md) ·
[workflow-orchestration](030-workflow-orchestration.md) ·
[conversation-rewind-and-file-checkpoint](005-conversation-rewind-and-file-checkpoint.md) ·
[replay-verify-tolerance](017-replay-verify-tolerance.md)

**Context & memory**:
[unified-context-supply](028-unified-context-supply.md) [+054](054-memory-index-frozen-per-task.md) ·
[context-compaction](003-context-compaction.md) [+049](049-compaction-summarize-without-tool-choice.md) [+050](050-compaction-count-based-second-valve.md) [+060](060-compaction-absolute-auto-compact-window.md) [+061](061-compaction-clear-point-on-its-own-number.md) [+063](063-compaction-arguments-stay-as-sent.md) [+064](064-compaction-restores-recent-files.md) [+066](066-compaction-batched-clearing-and-claude-code-margin.md) ·
[anchored-content-placement](036-anchored-content-placement.md) ·
[memory-consolidation](035-memory-consolidation.md) ·
[skill-resource-on-demand](020-skill-resource-on-demand.md) ·
[model-driven-skill-invocation](013-model-driven-skill-invocation.md) [+047](047-skill-menu-budget-and-keep-order.md) [+048](048-skill-menu-short-summaries-and-usage-rank.md) [+062](062-skill-listing-as-recorded-content.md) ·
[skill-directory-tiers](044-skill-directory-tiers.md)

**Tools, agents & providers**:
[tool-and-agent-catalog](025-tool-and-agent-catalog.md) ·
[tool-description-canonical](026-tool-description-canonical.md) ·
[control-tools-neutral-mechanism](004-control-tools-neutral-mechanism.md) [+051](051-control-call-answered-in-place.md) ·
[control-tool-contributions-and-activation-identity](039-control-tool-contributions-and-activation-identity.md) ·
[agent-identity-and-provenance](001-agent-identity-and-provenance.md) ·
[provider-adapters-and-multimodal](015-provider-adapters-and-multimodal.md) ·
[model-catalog-extension](043-model-catalog-extension.md) ·
[guard-observer-hooks](010-guard-observer-hooks.md) [+052](052-declarative-hooks-are-host-configuration.md) ·
[event-origin-marker](008-event-origin-marker.md) ·
[mcp-connectors](012-mcp-connectors.md) [+053](053-mcp-deferred-schemas.md) ·
[plugin-contribution-bundles](038-plugin-contribution-bundles.md)

**Boundaries, workspace & sandbox**:
[library-sdk-architecture](011-library-sdk-architecture.md) ·
[package-layout](014-package-layout.md) ·
[execution-environment-seam](033-execution-environment-seam.md) [+055](055-execenv-optional-backend-capabilities.md) ·
[workspace-and-session-path](031-workspace-and-session-path.md) ·
[workspace-write-authorization](037-workspace-write-authorization.md) ·
[shell-permission-and-background](018-shell-permission-and-background.md)

## ADR template

Next free number, `NNN-<slug>.md`. A shipped decision that is costly to reverse and will be questioned later earns a file — never layout, wording, or flows.

```markdown
# ADR-NNN: <one-line title: the decision itself>

- Status: accepted
- Date: YYYY-MM-DD

## Context

The problem and the constraints the decision answers to. A refinement names the ADR it refines.

## Decision

The conclusion, stated in the present tense ("the system is this way"), not "we will…".

## Rationale

The core invariant or benefit this decision protects.

## Alternatives considered

Every option seriously weighed and rejected, together with **why it is rejected**.

## Consequences

The constraints, costs, and follow-on points this decision creates.
```

The status is one line with nothing after it (`scripts/check-docs.sh` enforces the form). Superseding: write the new ADR, then change the old one's status to `superseded by ADR-NNN`.

## Writing discipline

- **Use the present tense.** A decision file describes the system as it stands, not a changelog.
- **Write the live design and the rejected alternatives** — those two, and nothing else. The shape that exists and the shapes that lost out are both load-bearing; the route between them is not.
- **No process numbering.** "Steps 3A/3B", "issue 14 §C", "phase 1, first cut" — construction bookkeeping belongs nowhere in a decision file.
- **No references to spec documents.** `docs/specs/` is one-shot working material and must never be cited from a decision file; a decision worth keeping is written out here in full.
- **Point at code.** A decision file may name modules and cite repo paths (`scripts/check-docs.sh` flags a path that stops resolving), but code needn't cite this directory — a comment that names an ADR is plain prose, not a contract ([ADR-006](006-doc-code-link-direction.md)).
- **Don't redefine terms.** Term meanings live in CONTEXT.md; decision files use them directly, adding a one-line anchor where needed.
- **Prose is in English**, with technical terms kept in their original form (code identifiers / APIs / library / tool / command names / file paths, plus fixed architecture terms like module, interface, seam, adapter, deep module).
