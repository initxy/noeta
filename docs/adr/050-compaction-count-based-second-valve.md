# ADR-050: A count-based second valve

- Status: accepted
- Date: 2026-09-25

## Context

Refines [ADR-003](003-context-compaction.md) (Context compaction: a token-only trigger, real recorded usage converted into the estimator's unit, and a protected verbatim tail). Where the two disagree, this record wins.

## Decision

This reverses [ADR-003](003-context-compaction.md)'s "relief valve, not an always-on clamp" stance for tool outputs, and its alternative 9's rejection of a separate micro-compaction layer.

- **What changed.** The composer's prune has a second valve. Once the request size — the same `max(estimate, recorded baseline)` reading, in the same unit, that the relief-valve gate uses — reaches `microcompact_fraction` of the usable window, every tool output older than the newest `microcompact_keep_recent` tool-result *blocks* (blocks, not messages, so one parallel batch cannot shelter all its results) is cleared with the existing lean marker, size floor and cleared-output refs. The relief valve at the water mark is unchanged and stays the backstop; a block either valve selects is cleared.
- **Why.** In a measured production session 72 % of the message bytes were tool outputs, and the relief valve only opened at 87 % of a 1M window: for most of the session every request re-sent the bulk of every file ever read. Waiting for the water mark optimised for never re-reading, at the price of carrying stale output on every round-trip.
- **The accepted trade-off.** An output the model needs again after it has been cleared is a `RecallHistory` page-back, not a re-run. The raw history keeps every body verbatim, so the recall tool's range widens from the collapsed prefix to `[0, max(summary_boundary, cleared_boundary))`, where `cleared_boundary` is one past the newest raw-history message whose output the prune cleared; a single-message call (`limit` 1) returns a block whole, up to the per-result cap. The `collapsed-context` reminder gains one line and now renders whenever cleared outputs exist, before any summary. Clearing the oldest kept result as each new one arrives also rewrites a few messages near the end of the cached prefix per round-trip; that cost is small next to re-sending the bulk.
- **Determinism.** The valve is a pure function of the composed messages and two recorded numbers, so a resumed run clears the same blocks and records the same refs as the live one.
- **Knobs.** `CompactionConfig.microcompact_fraction` (default `0.5`) and `CompactionConfig.microcompact_keep_recent` (default `5`; `None` disables the valve), both set by `derive_compaction_config` in the `providers` built-in. `COMPACTION_OFF` leaves the valve off. The fractions are derived from the window a host may cap (see [ADR-060](060-compaction-absolute-auto-compact-window.md)); the microcompaction threshold may also be set on its own (see [ADR-061](061-compaction-clear-point-on-its-own-number.md)).

## Refined by

- [ADR-060](060-compaction-absolute-auto-compact-window.md) — An absolute auto-compact window
- [ADR-061](061-compaction-clear-point-on-its-own-number.md) — The clear point on its own number
