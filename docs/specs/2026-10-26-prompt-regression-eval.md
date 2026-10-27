---
status: proposed
created: 2026-10-26
---

# A prompt-regression eval that runs from this repo

## Goal

A change to model-facing prompt text (the main prompt, tool descriptions, reminders,
compaction instructions) or to a provider adapter's wire behavior gets a quality
signal from one command in this repo, so an agent can verify it instead of shipping
on `make check` alone.

## Non-goals

- Replacing the public benchmark runs in `docs/benchmarks.md` (Terminal-Bench /
  SWE-bench through `noeta-agent`'s harbor adapter); those stay the headline numbers.
- Putting a live-LLM step into `make check` or CI.

## Context

`make check` covers kernel and SDK behavior well (≈4700 tests, coverage ≥ 85,
mypy --strict, import topology). It cannot judge prompt quality: the `-m live` tests
(`make e2e`) assert structural invariants only, and the benchmarks live in another
repo, run by hand. Prompt changes shipped in 0.6.28 went out with the benchmark rerun
still owed. Today the only honest gate for such a change is the note `AGENTS.md`
("Gates") asks for.

## Decisions

## Plan

- [ ] Pick 10–20 fixed scenarios that exercise prompt-sensitive behavior (delegation,
      tool choice, compaction continuity, refusing to treat tool output as instructions).
- [ ] Decide scoring: deterministic checks over the event log where possible, a judge
      model only where not; record the score per scenario.
- [ ] Add `make eval` running them against a configured model, writing a comparable
      summary (baseline file committed, diff printed).
- [ ] Replace the "state that the benchmark was not rerun" gate in `AGENTS.md` with
      "run `make eval`; no scenario regresses".

## Acceptance criteria

- `make eval` runs end to end with one model credential and finishes in under ~15 minutes.
- Re-running on an unchanged tree reproduces the baseline within a stated tolerance.
- A deliberately degraded main prompt (e.g. delegation rules removed) shows a regression.

## Risks

- Run-to-run variance hides small regressions; the tolerance must be measured, not guessed.
- Cost per run; keep the set small.

## Progress log
