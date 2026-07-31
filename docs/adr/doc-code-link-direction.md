# Docs point at code; code carries its own rationale, and invariants are enforced by tests

## Context

Two mechanisms could keep a decision connected to the code it governs: a reference layer that cites the decision file at every governed callsite, or tests that fail when the decision is violated. Only one of them can actually stop a violation, and maintaining both costs more than it returns.

## Decision

Documentation references code; code does not depend on documentation. A decision file may name the modules it governs. Nothing requires a source comment to cite a decision file, no guard checks that such citations resolve, and no behaviour depends on one existing — where a file name does turn up in a comment it is ordinary prose, a pointer rather than a contract.

Cross-cutting invariants are carried by structural tests: the import-linter contracts, the decision-union shape test, the decision-handler AST guards. Each states the rule it protects in its failure message, so the reasoning arrives at the moment a change trips it. Module-local rationale is written out in the nearest docstring — the reason itself, not a path to where the reason is kept.

## Rationale

A comment pointer enforces nothing. Only a failing test blocks a violation, and the load-bearing invariants have such tests. The pointer's other job, explaining why, is done better by a guard's failure message — which arrives exactly when it is needed — or by local prose, which is read without a detour. A code-to-doc reference layer therefore adds no enforcement while charging real costs: it presses documentation structure into the source, couples code to file names that are free to change, and needs a guard of its own to keep the references from rotting.

The asymmetry is principled. Documentation exists to talk about code, so a doc-to-code reference runs with the grain; making code talk about documentation runs against it, in exchange for something already delivered elsewhere.

## Alternatives considered

1. **Bidirectional links plus a dangling-reference guard** — every governed callsite cites its decision file, and a test asserts that each path resolves. Weighed and rejected: the citations enforce nothing, so this is a second mechanism doing no enforcement work, paid for with source pollution, file-name coupling, and a guard whose only job is to stop the layer rotting.
2. **Coverage enforcement in the same direction** — a guard asserting that every governed callsite carries a citation. Rejected for the same reason, with a worse failure mode: it makes the reference layer mandatory, so no decision file can be renamed or merged without a repo-wide edit.
3. **Keeping rationale only in the decision file and having code link to it rather than restate it.** Rejected: it puts the explanation an indirection away from the reader who most needs it, who is editing the line, not browsing the documentation.

## Consequences

- An invariant that spans modules needs a structural test, not a comment. A decision without one is unenforced, whatever any file says about it.
- A decision file's `Consequences` section names modules in prose and never claims that the code points back at it.
