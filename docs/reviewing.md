# Reviewing

Acceptance, not a line-by-line recheck: machines check what machines can; this
covers only the judgments the gates can't.

## Prerequisites

- Review in a fresh context — a new session or subagent, not the one that wrote
  the change; self-review grades soft.
- Run the automated gates (`make check`) first. **Not green → rejected, no human review.**
- Find the spec (default: search `docs/specs/`). With no requirement source,
  review by code quality and risk, and say plainly that requirement conformance
  can't be judged.

## Checks

1. Acceptance criteria: check off each with evidence (test, output, code
   location); "should be fine" is not evidence.
2. Ledger: spec status, Plan checkboxes, and Progress log match reality — a stale
   ledger is a revise, not a nit.
3. Scope: creep into Non-goals, unrelated refactors mixed in.
4. Risks the gates can't cover: boundaries, data migration, compatibility,
   security, concurrency, error handling.
5. Tests: critical path and failure path of new behavior covered.

## Findings

Sorted by severity, only actionable issues:

- [P1] Title — file/location; problem / impact / suggestion

P0 data loss, serious security, core flow broken; P1 must fix before merge;
P2 boundary issue or test gap; P3 minor, non-blocking.

## Verdict

One of three: **mergeable / revise and re-review / needs re-shaping**. No issues →
say explicitly none were found.

## Wrap-up (only when mergeable)

- Flip the spec to `done` and move it into `docs/specs/archive/` — one action.
- Distill from the spec's Decisions only what survived: stable terms and boundaries
  into `CONTEXT.md` (within its budget); shipped decisions costly to reverse into
  ADRs (a changed decision is a new ADR; mark the old one `superseded`).
- Large feature → suggest a scoped gc over the changed area.
- Behavior change to `packages/noeta-runtime` or `packages/noeta-sdk` → propose a
  release per `docs/releasing.md` (an approval gate: the maintainer signs off).
