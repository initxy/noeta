# Context compaction aligned with Claude Code: dropping the count gate, upgrading token counting, and the trade-offs between shape and template

## Context

noeta already has "single-pass summary compaction triggered by token count": when estimated tokens ≥ the available window, the old prefix beyond a protected tail is handed to the LLM to summarize into a `Compacted` event, deterministically replayable (this mechanism is described in `docs/adr/unified-context-supply.md`). This decision makes alignment adjustments on top of it.

Guiding principle: **align with Claude's *behavior* (context is never silently discarded; a long session still remembers its early goal), rather than copying its *implementation* mechanism by mechanism**—any implementation that conflicts with noeta's three invariants is rewritten or dropped. The three invariants are: events are immutable (`docs/adr/event-sourced-truth.md`), state is deterministically folded/rebuilt from the EventLog, and provider neutrality (`docs/adr/provider-neutral.md`).

## Decision

- **Drop the "message count" dimension and trigger purely by token count (aligning with Claude).** Remove the `max_history_messages=50` gate—it is decoupled from tokens, triggers far earlier than the token gate, and often strips away early context before the token summarizer ever gets a chance to run. With it gone, the token gate is the only entrance. The passive-overflow backstop ("compact and retry" when the provider actually returns an over-limit error) is kept.
- **Upgrade token counting to "recorded real usage + incremental estimation" (aligning with Claude).** Naive `chars ÷ 4` systematically underestimates context with cache, structured blocks, or images. Change it to "the real usage recorded on the previous turn + this turn's new messages estimated at chars/4"; on the first turn there is no history, so it falls back to pure estimation. **Key fact**: `LLMResponseRecorded` already records the real usage of every response into the event stream (replay reads it back byte-identically), so using it does not break determinism.
- **Keep "preserve a verbatim tail," don't copy Claude's full summary (deliberately not aligning).** noeta protects a verbatim tail (`tail_token_budget = available // _TAIL_FRACTION_DENOM`, which since the v1 wrap-up is **one third** of the available window—see "Revision—tail budget" below; originally a half) and only summarizes the old prefix before that tail; it does not, like Claude, summarize nearly all messages into a single event and keep no verbatim text.
- **Adopt Claude's sectioned template, trimmed to a lean subset (partial alignment).** Adopt the sectioned structure, trimmed to: Primary Request & Intent / Key Technical Concepts / Files & Code (including a list of relevant file paths) / Errors & Fixes / All user messages / Pending Tasks / decisions and constraints. **Drop Current Work / Next Step** (delegated to the verbatim tail). **Keep** noeta's existing `enforce_verbatim_constraints`—something Claude doesn't have (mechanically re-inserting any safety constraint the model dropped from the summary).
- **Don't build a microcompact layer—`_prune_tail` already is one; clear tool output into a placeholder marker rather than an empty string (detail alignment).** On every assembly, the composer's `_prune_tail` clears the tool-result output beyond the tail budget while keeping the skeleton—that is microcompact. The detail worth copying: don't clear output into an empty string `""` (the model may misread it as "this tool returned nothing at the time"); use an explicit placeholder marker so the model reads "there **was** content here, and it was omitted." The marker is **lean**—just `[tool output cleared]`, with no hash (see "Revision—lean cleared marker" below).
- **Don't do "re-inject after compaction"; keep only a path list for files in the summary (mostly a false requirement).**
  - Files: Claude backfills current content by re-reading from disk; noeta cannot re-read from disk (it breaks determinism), and dereferencing from the ContentStore yields only a stale snapshot (feeding stale content to an agent that is editing that very file is worse than not feeding it). **Approach**: the summary's "Files & Code" section keeps only a list of relevant file paths, and the model fetches the current version with `read` when it needs to.
  - skill / memory / instructions: the composer's `semi_stable` segment is independently re-rendered on every assembly, so they never enter the summarized history—no such problem.
  - plan / async subagent state: these are state fields, not history messages, so they don't go through compaction.

### Red lines (not to be crossed)

- **Keep the passive-overflow backstop** (the counting upgrade makes counting more accurate, but "this turn's additions" are still an estimate, so provider over-limit retry is the last line of defense).
- **Compaction must remain deterministically replayable**: boundary computation, token estimation, constraint re-insertion, and the recording/replay of the summary LLM call are all preserved as-is; any "re-read disk live / re-count provider tokens live" is forbidden.
- **`enforce_verbatim_constraints` must not be removed in the name of aligning with Claude** (verbatim re-insertion of safety constraints is exactly where noeta is stronger than Claude).
- **Cleared output must still be dereferenceable for audit**—but the reference lives in *internal provenance* (the body pruned by prune goes into `ContextPlan.cleared_outputs`; over-threshold truncation goes into `ToolResultRecorded.output_ref`), and it is **never** put into the model-facing marker (see "Revision—lean cleared marker" below).
- **Provider neutrality**: don't introduce Anthropic-only mechanisms (server-side `context_management`, forking to reuse the prompt cache, wording tuned specifically for Claude); the summary wording must be portable across providers.

## Revision—lean cleared marker (composer v5)

The original placeholder marker embedded a ContentStore ref into the model-facing string: `[tool output cleared; full_ref=<hash>]`, and the over-threshold truncation suffix likewise carried `full bytes at content ref <hash>`. This was wrong: **the model has no tool that can dereference a content hash**—it recovers omitted content by re-running the tool / re-reading the path (exactly like Claude), never by hash. So the hash in the prompt is dead weight: at best it wastes tokens, at worst it lures the model into hallucinating a nonexistent "fetch by hash" capability.

Decision (composer version `three_segment.v4` → `v5`):

- The model-facing marker is **lean**: `[tool output cleared]`, with no hash. It still distinguishes "content was omitted" from an empty string ("the tool returned nothing"), which is the only information the model can act on.
- The full-body ref moves to **internal provenance**, out of the prompt:
  - The body pruned by prune → `ContextPlan.cleared_outputs` (a new list field, parallel to `dropped_messages`; `_restore` reads it with `.get`, so a pre-v5 plan body still deserializes).
  - Over-threshold truncation already records the body separately into `ToolResultRecorded.output_ref`, so its inline `content ref <hash>` is pure redundancy—removed.
- audit / trace dereference the original body through these internal refs; the red line ("cleared output must still be dereferenceable") is kept, only its **storage location** changed and it moved out of the model-facing text.

This is the safe, deterministic slice of Claude's *History Snip* (removing redundant bookkeeping). The other slice—dropping a spent tool round-trip (a `tool_use` + its already-cleared `tool_result`)—was shaped and then **rejected**: once prune has cleared the output and large arguments are already offloaded (`arguments_ref`), all a spent round-trip leaves inline is the tool name, one small argument, the `[tool output cleared]` shell, and the message envelope—a few dozen tokens per pair, at a real fidelity cost (this segment hasn't been summarized yet, so the model would lose these call records before the next compaction). Not worth it at noeta's current offload depth; revisit only if real long-session data shows this long-tail bookkeeping actually accumulates into something material.

## Revision—tail budget (1/2 → 1/3)

The protected verbatim tail was originally `available // 2` (half the available window). This is heavier than needed: the original reason for "keep a big tail" (noeta can't re-read disk at compose time) does **not** require a half—the summary already keeps file paths, and the model re-reads the current version with `read` (the same effect as Claude's backfill, only model-driven rather than compose-driven). Spending half the window on verbatim recent text is a lot of context that a smaller tail could free.

Decision: the default drops to `available // _TAIL_FRACTION_DENOM`, with `_TAIL_FRACTION_DENOM = 3` (one third). Properties:

- **Frees window** (about 1/6 of the available window) for actual work / summary headroom.
- **Compaction triggers less often, not more**—this is a common misread. After a compaction, the live context is `summary (small) + tail`; a smaller tail means more headroom before the next trigger, so compaction is sparser. Each summary's prefix is larger (each call is slightly pricier). The trigger threshold (`estimate ≥ available_window`) is unchanged—tail and trigger are orthogonal knobs.
- **Cost**: recent verbatim fidelity drops. `1/3` is a conservative first step toward Claude's near-zero tail; tune it toward `1/4` later if real session data supports it.
- Still strictly satisfies `0 < tail < available`, so the summary always has a non-empty prefix.
- v1 only changes this constant (no new config surface). A future per-session override (a `derive_compaction_config(model, *, tail_fraction_denom=…)` threaded down from Options) is deferred. Don't bump `composer_version`—this changes the tail size, not the composed structure.

## Rationale

- **The real pain point is the gate, not the summary**: a measured 74-turn task had 0 `Compacted` events while the cumulative dropped count of `tail_window(limit=50)` climbed from 1 to 97, silently discarding the original requirement understanding—the count gate strips away early context before the token gate triggers.
- **Real-usage counting costs no determinism**: the original "for determinism we can only use chars/4" concern only holds for "re-count live," not for "read back an already-recorded number"; once the count gate is gone, counting is the only gate and must be more accurate.
- **Preserving the tail**: (a) the verbatim tail is the highest-fidelity record of recent state, always more trustworthy than a one-line Current Work in a summary; (b) Claude can afford "no verbatim" because it re-reads disk to backfill during compaction—which is exactly what noeta can't do for determinism. We can't copy the backfill, so we can't copy its most aggressive move; the verbatim tail compensates.
- **Cutting Current Work / Next Step**: recent state already lies verbatim in the tail, so having the summary restate it means using an estimate to restate text that already exists verbatim—wasteful and potentially conflicting.
- **Not backfilling files, only keeping paths**: re-reading disk breaks determinism; dereferencing stale bytes from the ContentStore harms an agent editing that very file.

## Alternatives considered

1. **Keep the 50-count gate but change its action from a bare drop to triggering the summary.** Rejected: turning the gate into a compaction trigger is more robust but diverges from Claude; the passive-overflow backstop already absorbs estimation error, so the count gate's marginal value is too low.
2. **Keep using pure chars/4.** Rejected: with the count gate gone, counting is the only gate and must be more accurate; real usage is already in the event stream and replay-safe.
3. **Fully align with Claude's full summary and keep no verbatim.** Rejected: noeta can't backfill files, and copying aggressive compaction would lose the safety buffer.
4. **Copy Claude's 9-section template verbatim.** Rejected: Current Work / Next Step duplicate the verbatim tail—using an estimate to restate text that already exists verbatim.
5. **Build a separate standalone microcompact layer.** Rejected: `_prune_tail` already covers it and is more continuous; empty string → placeholder marker is pure gain at zero architectural cost.
6. **Backfill by dereferencing stale bytes from the ContentStore / re-reading disk.** Rejected: stale content harms an agent editing the file / breaks determinism.

## Consequences

- Trigger and template logic land in `noeta.policies.react` (dropping the `max_history_messages` gate, `enforce_verbatim_constraints`, the lean summary template); token counting lands in `noeta.protocols.token_estimate` (real usage + incremental estimation).
- The cleared marker and tail land in `noeta.context.composer` (`_prune_tail` → the lean `_CLEARED_MARKER`; the cleared body ref is returned to `ContextPlan.cleared_outputs`); the new `cleared_outputs` field is in `noeta.protocols.context_plan`; the over-threshold truncation's lean suffix is in `noeta.core._decision_handlers` (`truncate_tool_output`, with the full body in `ToolResultRecorded.output_ref`); `tail_token_budget` is computed by `noeta.execution.builder`.
- The composer version goes from `three_segment.v4` to `v5`, but a pre-v5 plan body still deserializes (`_restore` reads `cleared_outputs` with `.get`).
