# ADR-060: An absolute auto-compact window

- Status: accepted
- Date: 2026-10-09

## Context

Refines [ADR-003](003-context-compaction.md) (Context compaction: a token-only trigger, real recorded usage converted into the estimator's unit, and a protected verbatim tail) and [ADR-050](050-compaction-count-based-second-valve.md) (A count-based second valve). Where they disagree, this record wins.

## Decision

- **What changed.** `HostConfig.auto_compact_window` caps the window every compaction number derives from: `derive_compaction_config(model, auto_compact_window=N)` takes `min(catalog window, N)` as `context_window`, so the usable window, the protected tail and the microcompaction threshold follow it in their usual proportions. The output ceiling stays the model's, and the 1 % skill-roster and memory-index budgets keep sizing against the model's window.
- **Why.** On a 1M-token model the water mark sits near 966K and the second valve at 483K; a worker's whole job ends at 100–300K, so neither layer ever opened and every request re-sent every tool output the task had ever read. Claude Code answers this with an absolute `autoCompactWindow`, a token count capped at the model's window — a fraction of a 1M window would still be hundreds of thousands of tokens.
- **Where it lives.** Wiring, not identity: it changes the composed bytes the way `tool_output_inline_limit` does, so a resumed task must be given the same value, and it is not part of `AgentSpec`.
- **The note gate** of [ADR-003](003-context-compaction.md) also counts localised section headings — a markdown heading or a bold run opening with a section number — so a summarizer that translates the nine titles is not refused; a plain numbered line still counts for nothing, since that is also the shape of a narration's plan. The prompt now asks for the titles verbatim in English.

## Refined by

- [ADR-066](066-compaction-batched-clearing-and-claude-code-margin.md) — Batched clearing of re-fetchable results, a summarize input that matches the view, and Claude Code's margin
