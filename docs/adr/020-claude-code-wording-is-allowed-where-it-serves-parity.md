# ADR-020: Claude Code's wording is allowed in prompt text where it serves parity

- Status: accepted
- Date: 2026-10-28
- Refines: [ADR-003](003-claude-code-is-the-model-visible-contract.md)

## Context

ADR-003 requires every prompt text to be in noeta's own words, and a check
(`scripts/parity/own_words.py`) failed `make parity` and `make check` on any run of text shared
with Claude Code's recorded requests. Keeping texts clear of that check took repeated rewording
rounds, and a text reworded only to pass it can drift from the behavior it describes; a
paraphrase that keeps the sentences and swaps words passes the check without being any more
our own.

## Decision

- What matters in a model-visible text is that it produces the behavior ADR-003 makes the
  contract. Claude Code's wording, verbatim or close, is allowed where it serves that parity;
  rewording is not required.
- `scripts/parity/own_words.py` stays as an advisory report: `make parity` prints the shared
  runs and always exits 0, and no test fails on them.
- Everything else in ADR-003 holds: the model-visible behavior is the contract, differences
  are bugs unless listed in `parity/deviations.md`, and recordings and anything derived from
  their wording stay out of the repository (`scripts/parity/README.md`).

## Rationale

Models are tuned on the texts as Claude Code words them, so the closest wording is the
lowest-risk way to get the same behavior; rewording costs eval runs and risks regressions for
no behavioral gain. A report still shows where texts come from, for a licensing review before
the repository is published.

Rejected: a stricter check on word runs (catches more paraphrase, but forces more rewording of
texts whose behavior is already right); dropping the report (loses the overview for that
review).

## Consequences

- Whether shipping Claude Code's wording is acceptable for a public release is the owner's
  licensing call, made with the report's output in hand.
- Reviews judge prompt texts by the behavior they produce (`make eval`, `make parity`), not by
  their wording's origin.
