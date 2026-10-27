# ADR-064: Recently used files come back after a compaction

- Status: accepted
- Date: 2026-10-15

## Context

Refines [ADR-003](003-context-compaction.md) (Context compaction: a token-only trigger, real recorded usage converted into the estimator's unit, and a protected verbatim tail). Where the two disagree, this record wins.

## Decision

- **Claude Code's post-compaction file restore, copied.** When a compaction summarizes, the Policy re-reads the files the newest successful `Read` / `Edit` / `Write` calls touched — at most 5, each within 5,000 tokens, 50,000 in all (Claude Code 2.1.300's limits), skipping a file whose `Read` the kept tail already shows and the instruction files the instructions resident carries — through the `Read` tool, so a sandboxed workspace is read where it lives. Each comes back as Claude Code's pair, "Called the Read tool with the following input: …" and "Result of calling the Read tool: …", or as its "too large to include" note.
- **Recorded, not recomposed.** The texts ride `CompactionRequestedDecision.reattached`, and the handler appends them after `Compacted` as host-injected user turns — the same place Claude Code puts them, after the kept messages. They are history from then on: a resume folds them back and never reads the disk, and the next compaction summarizes them like any other turn. This is a read at decide time, like the summarize round-trip, not a read at compose time, so the composer stays a pure function of recorded state.
- **Bounded by the protected tail too.** The restore total is also capped at the protected tail budget, so on a small window the restore alone can never refill the window and re-trigger compaction; at a 200k window the tail budget is above 50,000 and Claude Code's numbers apply unchanged.
- **No todo attachment.** Claude Code 2.1.300 attaches no todo list after a compaction; the list comes back through its todo reminder, which counts from the compaction boundary — and so does noeta's (`todo-reminder`).
