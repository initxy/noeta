# ADR-066: Batched clearing of re-fetchable results, a summarize input that matches the view, and Claude Code's margin

- Status: accepted
- Date: 2026-10-26

## Context

Refines [ADR-003](003-context-compaction.md) (Context compaction: a token-only trigger, real recorded usage converted into the estimator's unit, and a protected verbatim tail), [ADR-050](050-compaction-count-based-second-valve.md) (A count-based second valve) and [ADR-060](060-compaction-absolute-auto-compact-window.md) (An absolute auto-compact window). Where they disagree, this record wins.

Three things went wrong on long tasks. The summarize input was cut from the raw history, so every output the valves had cleared came back in it — a request far past the window the main loop never sent, failing as `compaction_summary_failed` (seen in production at 850K tokens). Once the second valve opened it cleared one more result per step, rewriting the cached prefix on every round-trip, and it cleared any tool's output, including a subagent's report or a user's answer that cannot be fetched again. And the usable window subtracted the model's whole output ceiling plus a 2K buffer, from an `auto_compact_window` cap too: a 200K cap on a model with a 128K ceiling compacted at 70K.

## Decision

- **The summarize input matches the view.** Every tool result the composed View shows cleared is sent cleared to the summarizer too (`_as_composed` in `noeta/builtins/react/impl/react.py`) — a pure function of the View, so a resume rebuilds the same request. A summarize call that still answers with a context overflow is retried up to three times, each time dropping the oldest fifth (at least one) of the rounds behind a leading `[earlier conversation truncated for compaction retry]` line, as Claude Code does. A round opens at every message that is not a tool result, so no result is left without its call; the previous note, when there is one, is kept. The verbatim-constraint post-check still runs over the untruncated input.
- **Only re-fetchable results are cleared.** A tool declares `clearable = True` (`noeta.protocols.tool.is_clearable`); undeclared means no. Read, Glob, Grep, Bash, BashOutput, Edit, Write, WebFetch and WebSearch declare it — Claude Code's set. Both valves clear only these results, and the keep-recent places of [ADR-050](050-compaction-count-based-second-valve.md) count only these, so a run of small or unclearable results no longer pushes a fresh read out. The composer reads the declaration off its own tool table, so the kernel imports no built-in.
- **The second valve clears in batches.** Past its threshold, the clearable results older than the newest five are cut, oldest first, into batches of at least 20,000 estimated tokens (`microcompact_batch_tokens`); every complete batch is cleared and the last, incomplete one is not. Between two batches the composed history stays byte-identical, so the cached prefix survives; when a batch fills it clears at once. A batch's extent depends only on the results before it, so the cuts never move as the history grows and a resume makes the same ones.
- **Claude Code's margin.** The usable window — the summarize trigger, and the base of the tail and the valve thresholds — is `window − min(output ceiling, 20,000) − 13,000`, whether the window is the model's or an `auto_compact_window` cap. `CompactionConfig` keeps `max_output_tokens` as the model's ceiling because it rides `max_tokens` on the wire; `derive_compaction_config` sets the signed `compaction_buffer` that turns the kernel's `window − max_output_tokens − compaction_buffer` into that reserve.

## Rationale

- A summarize request larger than the main loop's request defeats the reason it exists; matching the view keeps it about one window wide, and the overflow retry is the backstop for the rest. The dropped rounds stay recallable with `RecallHistory`.
- Clearing pays off only for output the model can fetch again with one call. A report or an answer cannot be, and losing it costs more than the bytes it saves.
- One rewrite per 20K tokens of new clearable output is a cache write the clearing pays for; one rewrite per step was not.
- A response rarely reaches a 64K or 128K output ceiling, so reserving all of it wasted a large share of a capped window.

## Alternatives considered

1. **Batch in real tokens, converted by the observed density.** Rejected: the density is re-measured on every round-trip, so density-converted cuts slide from one step to the next and the view churns — the thing batching exists to stop. The estimate is stable; the threshold is a cache-economics knob, not a window limit.
2. **Clear down to the newest five at the moment the valve opens, then batch.** Rejected: the opening step is not recorded, and re-deriving it needs the real count of every earlier request. Cutting batches from the oldest result leaves under one batch uncleared at the opening, and stays a pure function of the composed messages.
3. **Summarize the raw history and rely on the overflow retry.** Rejected: dropping a fifth of the rounds at a time cannot shrink an input that is several windows wide in three tries, and what it drops is the start of the task.
4. **Mark tools as not clearable instead.** Rejected: a new or third-party tool (an MCP server, a host tool) would then be cleared by default, and its output is the kind most likely not to be fetchable again.

## Consequences

- `ThreeSegmentComposer` takes `microcompact_batch_tokens` (default 20,000); `derive_compaction_config` does not set it.
- A host tool whose output is cheap to fetch again opts in with `clearable = True`.
- Which results are cleared changed, not the composed structure, so the composer version is unchanged ([ADR-003](003-context-compaction.md)).
