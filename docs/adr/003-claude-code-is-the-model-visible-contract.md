# ADR-003: Claude Code's model-visible behavior is the contract; prompt text is our own words

- Status: accepted
- Date: 2026-10-28
- Refined by: [ADR-020](020-claude-code-wording-is-allowed-where-it-serves-parity.md)
  (Claude Code's wording is allowed where it serves parity; the own-words check is advisory)

## Context

Models are trained and tuned against the tools and prompts of the most-used coding agent;
their strongest priors are on its tool names, schemas, message shapes and conventions. A
harness that invents its own pays for it in model quality, and users have to learn a new
configuration surface. Copying another product's prompt text verbatim is not an option.

## Decision

- What the model sees follows the latest Claude Code default configuration: tool names and
  schemas, the order and roles of prompt parts, reminders and notices, compaction, the request
  shape a provider receives. A difference is a bug unless it is listed with its reason in
  `parity/deviations.md`.
- Every prompt text is written in noeta's own words, keeping Claude Code's rules and section
  order (`parity/rules/`).
- `make parity` replays Claude Code's recorded scenarios offline against noeta and reports
  every request difference (`scripts/parity/README.md`).
- The implementation underneath is free to differ where noeta's is better (the event log,
  recovery, many machines).

## Rationale

Matching the model-visible surface lets noeta inherit the models' tuning and Claude Code's
users' habits and files (skills, agents, hooks, settings formats) without inheriting its
architecture. A recorded, replayable comparison turns "matches Claude Code" from a claim into a
gate. Own wording keeps the project free of copied text.

Rejected: an independent prompt and tool design (measurably worse with current models and a
new surface for users); verbatim copies of Claude Code's prompts (licensing).

## Consequences

- A Claude Code release can open parity gaps; they are closed against new recordings.
- Changes to model-facing text or provider wire behavior run `make eval` and `make parity`
  in addition to `make check`.
- Model-visible tool names are exempt from noeta's naming rules (`CONTEXT.md`).
