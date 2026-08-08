# docs/adr/ — Architecture Decision Records

This directory holds Noeta's **Architecture Decision Records (ADRs)**: each file captures one stable, cross-module decision — **what the decision is, why the system is shaped that way, and why the alternatives are rejected**. The audience is any agent about to change this code. Before you touch a subsystem, read the matching decision file: it names the constraint the current shape protects and the paths that are ruled out, so a rejected design does not get proposed a second time.

Every file here is live. A decision file describes part of the system as it stands.

## Division of labor with CONTEXT.md

- **`docs/adr/`** (this directory): **why the system is this way**, organized by topic. One topic per file, containing only "why it is this way / why the alternatives are rejected."
- **`CONTEXT.md`**: a glossary that pins down what a term means in this repository.
- **Nearby docstrings**: local rationale that affects only a single file or function lives in that docstring, not here.

Rule of thumb: the wider the impact (spanning multiple modules), the more it belongs in `docs/adr/`; the narrower it is, the closer it should sit to the code itself.

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
[worker-queue-routing](worker-queue-routing.md) ·
[step-attempt-recovery](step-attempt-recovery.md) ·
[mid-turn-goal-injection](mid-turn-goal-injection.md) ·
[interrupt-responsiveness](interrupt-responsiveness.md) ·
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
[model-driven-skill-invocation](model-driven-skill-invocation.md) ·
[skill-directory-tiers](skill-directory-tiers.md)

**Tools, agents & providers**:
[tool-and-agent-catalog](tool-and-agent-catalog.md) ·
[tool-description-canonical](tool-description-canonical.md) ·
[control-tools-neutral-mechanism](control-tools-neutral-mechanism.md) ·
[control-tool-contributions-and-activation-identity](control-tool-contributions-and-activation-identity.md) ·
[agent-identity-and-provenance](agent-identity-and-provenance.md) ·
[provider-adapters-and-multimodal](provider-adapters-and-multimodal.md) ·
[model-catalog-extension](model-catalog-extension.md) ·
[guard-observer-hooks](guard-observer-hooks.md) ·
[event-origin-marker](event-origin-marker.md) ·
[mcp-connectors](mcp-connectors.md) ·
[plugin-contribution-bundles](plugin-contribution-bundles.md)

**Boundaries, workspace & sandbox**:
[library-sdk-architecture](library-sdk-architecture.md) ·
[package-layout](package-layout.md) ·
[execution-environment-seam](execution-environment-seam.md) ·
[workspace-and-session-path](workspace-and-session-path.md) ·
[workspace-write-authorization](workspace-write-authorization.md) ·
[shell-permission-and-background](shell-permission-and-background.md)

## ADR template

One topic per file, named with a topic slug (e.g. `provider-neutral.md`). Every file has at least a `Decision` and a `Rationale` section:

```markdown
# <one-line title: the decision itself>

## Context

The problem and the constraints the decision answers to.

## Decision

The conclusion, stated in the present tense ("the system is this way"), not "we will…".

## Rationale

The core invariant or benefit this decision protects. Write it out fully; don't cut it because it "looks obvious."

## Alternatives considered

Every option seriously weighed and rejected, together with **why it is rejected**, so nobody proposes the same dead end again.

## Consequences

The constraints, costs, and follow-on points this decision creates. When you need to point at where something lands, just name the module in prose.
```

`Context` / `Alternatives considered` / `Consequences` can be trimmed depending on complexity; `Decision` and `Rationale` are mandatory.

## Writing discipline

- **Use the present tense.** A decision file describes the system as it stands, not a changelog.
- **Write the live design and the rejected alternatives** — those two, and nothing else. The shape that exists and the shapes that lost out are both load-bearing; the route between them is not.
- **No process numbering.** "Steps 3A/3B", "issue 14 §C", "phase 1, first cut" — construction bookkeeping belongs nowhere in a decision file.
- **No references to spec documents.** `docs/implementation-specs/` holds only in-flight work and must never be cited from a decision file; a decision worth keeping is written out here in full.
- **Don't reference code, and don't get referenced by code.** A decision file may name modules, but it never says "the code comment points back to this file"; the code side likewise never references this directory (see doc-code-link-direction.md).
- **Don't redefine terms.** Term meanings live in CONTEXT.md; decision files use them directly, adding a one-line anchor where needed.
- **Prose is in English**, with technical terms kept in their original form (code identifiers / APIs / library / tool / command names / file paths, plus fixed architecture terms like module, interface, seam, adapter, deep module).
