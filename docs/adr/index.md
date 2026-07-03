# docs/adr/ — Architecture Decision Records

This directory holds Noeta's **Architecture Decision Records (ADRs)**: each file captures one stable, cross-module decision — **what was decided, why it was decided that way, and why the alternatives were rejected**. The audience is any agent about to change this code (including Claude Code itself): before you touch a subsystem, read the matching decision file so you understand where things currently stand and which paths have already been ruled out — don't walk back down a dead end someone already explored (Chesterton's fence).

## Division of labor with CONTEXT.md

- **`docs/adr/`** (this directory): **why it was decided this way**, organized by topic. One topic per file, containing only "why it is this way / why the alternatives were rejected."
- **`CONTEXT.md`**: a glossary that pins down what a term **currently means** in this repository.
- **Nearby docstrings**: local rationale that affects only a single file or function lives in that docstring, not here.

Rule of thumb: the wider the impact (spanning multiple modules), the more it belongs in `docs/adr/`; the narrower it is, the closer it should sit to the code itself.

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
