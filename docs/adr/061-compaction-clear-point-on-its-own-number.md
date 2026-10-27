# ADR-061: The clear point on its own number

- Status: accepted
- Date: 2026-10-10

## Context

Refines [ADR-003](003-context-compaction.md) (Context compaction: a token-only trigger, real recorded usage converted into the estimator's unit, and a protected verbatim tail) and [ADR-050](050-compaction-count-based-second-valve.md) (A count-based second valve). Where they disagree, this record wins.

## Decision

- **What changed.** `HostConfig.clear_tool_outputs_at` sets the request size, in tokens, at which the second valve opens: `derive_compaction_config` turns it into `microcompact_fraction = min(1, N / usable window)`. Unset, the fraction stays `0.5`.
- **Why.** One window number moved both layers together. A worker wants to shed old tool outputs early (~120K) and summarize late (~280K); a fraction of one number cannot say both.
- **Arguments clear too (same day).** Under either valve, a tool_use's argument strings of 400 characters or more become `[cleared, N chars]`, keys and small values kept, so the call still pairs with its result. The newest assistant turn that called a tool is never touched: it is the turn whose thinking signature a provider checks as sent, and clearing an older one changes the prefix exactly as clearing an older result already does. The arguments need no cleared-output ref; they are recorded with the assistant message. Measured on a worker: one job's final request carried 711K characters of `Write` / `Edit` arguments against 48K of results.
- **The marker is Claude Code's.** A cleared result reads `[Old tool result content cleared]`, byte for byte the reference agent's text; the tool call it answers keeps its arguments, so the model still sees what it already fetched.

## Refined by

- [ADR-063](063-compaction-arguments-stay-as-sent.md) — Arguments stay as sent
