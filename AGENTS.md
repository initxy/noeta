## Communication

- Keep answers concise and direct; lead with the conclusion and the next step.
- Don't add unrelated explanation; when unsure, state your assumptions and the risks.
- Conversation may happen in whatever language the other side uses; everything committed to the repo (see Language) is English.

## Language

- All repository artifacts are written in English: docs (`CONTEXT.md`, ADRs, `docs/implementation-specs/`, handoffs), code comments, and identifiers.
- Keep technical terms in their canonical English form and spell them consistently throughout: code identifiers (function / class / file / variable names), API / library / tool / command names, file paths, and fixed architecture terms (module, interface, seam, adapter, deep module, etc.). Don't invent alternate names or switch between synonyms.
- Write clear, idiomatic English prose — not word-for-word translation from another language.
- Pick one spelling per term and use it everywhere; don't mix variants.

## Workflow

- When the idea isn't clear yet, first converge on a short spec: the goal, scope, key decisions, and acceptance criteria.
- Once the goal is clear, implement against that spec; for large tasks with clean boundaries, split work across subagents while the main agent integrates and verifies.
- When done, review the result against the spec, the actual code changes, and the verification output (`make check`; see CONTRIBUTING.md).
- For architecture improvements, refactoring direction, and module design, start from the existing decisions (see Context / ADR) and the engineering constraints below; when work must continue in a later session, leave a written handoff of current state, decisions made, and next steps.

## Context / ADR

- Read `CONTEXT.md` first whenever you touch domain concepts, system boundaries, or stable conventions.
- Read `docs/adr/` first whenever a long-term architecture trade-off is involved.
- New stable terms go into `CONTEXT.md`; long-term decisions go into an ADR; one-off details are not persisted.
- Write complex or cross-session implementation specs into `docs/implementation-specs/`.

## Engineering constraints

- Prefer existing patterns in the repo, keep changes focused, and avoid unrelated refactors.
- Architecturally, prefer deep modules: a small interface hiding a substantial implementation.
- The interface is the test surface; don't introduce a seam without a real need to substitute the implementation.
- After a change, run verification matched to the risk; if you can't verify, say why.

## Release

- A merged behavior change to runtime / sdk / agent should be followed by a release; bumps are patch by default — minor/major is the maintainer's explicit call.
- Read `docs/releasing.md` before cutting a release; it holds the full procedure.
