# ADR-063: Arguments stay as sent

- Status: accepted
- Date: 2026-10-15

## Context

Refines [ADR-003](003-context-compaction.md) (Context compaction: a token-only trigger, real recorded usage converted into the estimator's unit, and a protected verbatim tail) and [ADR-061](061-compaction-clear-point-on-its-own-number.md) (The clear point on its own number). Where they disagree, this record wins.

## Decision

- **What changed.** The argument clearing of [ADR-061](061-compaction-clear-point-on-its-own-number.md) is withdrawn: a tool_use block keeps its arguments whatever the valves do; only tool outputs are cleared.
- **Why.** A worker read the `[cleared, N chars]` marker in its own history as what its `Write` had left on disk and wrote the marker into files. Rather than word the marker around that, the prune follows Claude Code, which clears old tool results only and leaves tool inputs for the summary to cover.
