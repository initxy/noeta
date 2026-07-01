# Docs point to code, code never points back to docs; invariants are enforced by tests

## Context

There was once a bidirectional-reference scheme: every governed code point referenced `docs/decisions/<slug>.md`, and a `tests/test_decisions_refs.py` asserted that every path resolved. This reference layer enforced no invariant, yet it carried the costs of polluting the source, coupling code to decision file names, and requiring its own guard to maintain. This decision fixes the single direction of references between docs and code.

## Decision

The link between persisted docs and source is one-directional: a decision file **may** point to the code it governs (an optional "callsites this decision governs" section); **source does not point back to `docs/adr/`**. Cross-cutting invariants are enforced by structural tests (import-linter contracts, plus the decision-union / handler-AST guards), and each guard's failure message names the relevant decision, delivering the "why" exactly at the moment a change trips it. Module-local "why" is written as inline prose in the nearest docstring—stating the rationale itself directly, rather than giving a path to a decision.

There is no `tests/test_decisions_refs.py`, and no rule requires code comments to reference a decision path. Old `docs/decisions/*.md` references still lingering in the source (the old name before that directory was renamed to `docs/adr/`) are harmless prose, digested naturally as files get rewritten.

## Rationale

A code-comment pointer enforces nothing—only a failing test can block a violation, and Noeta's load-bearing invariants already have such tests. The pointer's only other job, "explaining why," is more reliably delivered from a guard's failure message (at the moment of violation) or from inline local prose. So the "code → doc" reference layer carries no enforcement and only cost: it pollutes the source, couples code to decision file names, and needs its own guard (`test_decisions_refs.py`) to keep those ~2000 scattered references from rotting. Remove this layer and the whole second mechanism disappears, with no loss of enforcement.

This asymmetry is principled: the purpose of docs is to talk about code, so "doc → code" goes with the grain; making code talk about docs is coupling against the grain, and its value is already realized elsewhere.

## Alternatives considered

1. **Bidirectional links + a dangling-reference guard** (the old scheme: every governed code point references `docs/decisions/<slug>.md`, and `test_decisions_refs.py` asserts every path resolves). Rejected: these references enforce no invariant—structural tests do—so the reference layer plus its guard is a second mechanism doing no enforcement work, at the cost of source pollution and file-name coupling.
2. **Rewrite all ~2000 existing references out of the source right now.** Rejected for this round: these references are woven into explanatory prose, and each deletion needs judgment and a rewritten sentence—a repo-wide change disproportionate to its harm. Let them digest naturally; the rule change only stops new ones.

## Consequences

- This rule is itself the embodiment of "docs may reference code, code does not point back to docs"; all ADRs in this batch follow it—the `Consequences` section names modules in prose rather than claiming code points back to this file.
- Cross-cutting invariants are borne by structural tests: import-linter contracts and the decision-union / handler-AST guards; each guard's failure message names the relevant decision.
- Old `docs/decisions/*.md` references are not specially cleaned up; they digest naturally as files are rewritten.
