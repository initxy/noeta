# docs/adr/ — Architecture Decision Records

This directory holds Noeta's **Architecture Decision Records (ADRs)**: each file captures one stable, cross-module decision — **what was decided, why it was decided that way, and why the alternatives were rejected**. The audience is any agent about to change this code (including Claude Code itself): before you touch a subsystem, read the matching decision file so you understand where things currently stand and which paths have already been ruled out — don't walk back down a dead end someone already explored (Chesterton's fence).

## Division of labor with CONTEXT.md

- **`docs/adr/`** (this directory): **why it was decided this way**, organized by topic. One topic per file, containing only "why it is this way / why the alternatives were rejected."
- **`CONTEXT.md`**: a glossary that pins down what a term **currently means** in this repository.
- **Nearby docstrings**: local rationale that affects only a single file or function lives in that docstring, not here.

Rule of thumb: the wider the impact (spanning multiple modules), the more it belongs in `docs/adr/`; the narrower it is, the closer it should sit to the code itself.

## Status

A decision file is **live** unless it says otherwise. When a later decision
overrides part or all of an earlier one, the earlier file gets a `> **Status:**`
blockquote directly under its title — nothing else changes, so the original
reasoning stays readable:

```markdown
# <the original title>

> **Status: superseded by [runtime-sdk-app-restructure.md](runtime-sdk-app-restructure.md)** (<what changed>).
> <one or two sentences: which part is dead, which part still holds.>
```

Two rules make this useful rather than decorative:

- **Say what survived.** Most supersessions are partial — the wire changed, the
  invariant did not. A blanket "superseded" throws away a rationale that is
  still load-bearing.
- **Never delete a superseded file.** The point of a decision record is the
  rejected alternatives; deleting it invites someone to re-walk the dead end
  (the whole Chesterton's fence purpose of this directory).

Without this marker the only way to discover that a decision had been overturned
was to read all 40+ files and notice the contradiction.

## The decisions

**Foundations** — the invariants everything else is built on:
[event-sourced-truth](event-sourced-truth.md) ·
[task-as-only-primitive](task-as-only-primitive.md) ·
[single-writer-invariant](single-writer-invariant.md) ·
[storage-protocols-l0](storage-protocols-l0.md) ·
[provider-neutral](provider-neutral.md) ·
[doc-code-link-direction](doc-code-link-direction.md)

**Engine & execution**:
[engine-policy-dataflow](engine-policy-dataflow.md) ·
[worker-lease-model](worker-lease-model.md) ·
[step-attempt-recovery](step-attempt-recovery.md) ·
[multi-host-lease-fencing](multi-host-lease-fencing.md) ·
[subtask-fanout-and-durable-wake](subtask-fanout-and-durable-wake.md) ·
[subtask-parallel-execution](subtask-parallel-execution.md) ·
[background-subagent](background-subagent.md) ·
[transport-neutral-fanout](transport-neutral-fanout.md) ·
[workflow-orchestration](workflow-orchestration.md) ·
[conversation-rewind-and-file-checkpoint](conversation-rewind-and-file-checkpoint.md) ·
[replay-verify-tolerance](replay-verify-tolerance.md)

**Context & memory**:
[unified-context-supply](unified-context-supply.md) ·
[context-compaction](context-compaction.md) ·
[anchored-content-placement](anchored-content-placement.md) ·
[memory-consolidation](memory-consolidation.md) ·
[skill-resource-on-demand](skill-resource-on-demand.md) ·
[model-driven-skill-invocation](model-driven-skill-invocation.md)

**Tools, agents & providers**:
[tool-and-agent-catalog](tool-and-agent-catalog.md) ·
[tool-description-canonical](tool-description-canonical.md) ·
[control-tools-neutral-mechanism](control-tools-neutral-mechanism.md) ·
[control-tool-contributions-and-activation-identity](control-tool-contributions-and-activation-identity.md) ·
[agent-identity-and-provenance](agent-identity-and-provenance.md) ·
[provider-adapters-and-multimodal](provider-adapters-and-multimodal.md) ·
[guard-observer-hooks](guard-observer-hooks.md) ·
[event-origin-marker](event-origin-marker.md) ·
[mcp-connectors](mcp-connectors.md) ·
[plugin-contribution-bundles](plugin-contribution-bundles.md)

**Boundaries, workspace & sandbox**:
[library-sdk-architecture](library-sdk-architecture.md) ·
[package-layout](package-layout.md) ·
[runtime-sdk-app-restructure](runtime-sdk-app-restructure.md) ·
[execution-environment-seam](execution-environment-seam.md) ·
[workspace-and-session-path](workspace-and-session-path.md) ·
[workspace-write-authorization](workspace-write-authorization.md) ·
[shell-permission-and-background](shell-permission-and-background.md)

## ADR template

One topic per file, named with a topic slug (e.g. `provider-neutral.md`). Every file has at least a `Decision` and a `Rationale` section:

```markdown
# <one-line title: the decision itself>

## Context

The problem, constraints, and circumstances that triggered this decision.

## Decision

The current conclusion, stated in the present tense ("the system is this way"), not "we will…".

## Rationale

The core invariant or benefit this decision protects. This is the lifeblood of the Chesterton's fence — write it out fully, and don't cut it just because it "looks obvious."

## Alternatives considered

Every option that was seriously weighed and then rejected, together with **why it was rejected**, so nobody proposes the same dead end again.

## Consequences

The constraints, costs, and follow-on points this decision creates. When you need to point at where something lands, just name the module in prose.
```

`Context` / `Alternatives considered` / `Consequences` can be trimmed depending on complexity; `Decision` and `Rationale` are mandatory.

## Writing discipline

- **Keep the why, drop the how-we-got-here.** Process numbering that only mattered during one construction effort — "the refactor split into steps 3A/3B," "issue 14 §C," "Phase 1, first cut" — never belongs in a decision file.
- **Use the present tense.** A decision describes the system as it is now, not a changelog.
- **Don't reference code, and don't get referenced by code.** A decision file may name modules, but it never says "the code comment already points back to this file"; the code side likewise never references this directory (see doc-code-link-direction.md).
- **Don't redefine terms.** Term meanings live in CONTEXT.md; decision files use them directly, adding a one-line anchor where needed.
- **Prose is in English**, with technical terms kept in their original form (code identifiers / APIs / library / tool / command names / file paths, plus fixed architecture terms like module, interface, seam, adapter, deep module).
