# ADR-030: The log is the only full transcript; the fold keeps what the next step needs

- Status: accepted
- Date: 2026-11-05
- Refines: [ADR-006](006-fold-snapshots.md), [ADR-017](017-deferred-tool-loading-is-a-plugin.md),
  [ADR-026](026-a-blocked-prompt-is-erased-by-a-core-record.md)

## Context

The fold kept `TaskState.history`: every message ever added, never compacted, so plugins could
look back past a compaction (tools loaded through `ToolSearch`, memory pages read, turns since a
reminder, the saved transcript). Each snapshot rewrote it whole, so a snapshot grew with the
task's life, not with what the next step needs, and a continued task carried its source's
whole history.

## Decision

- The event log is the only full transcript. The fold keeps the conversation the model sees
  (bounded by compaction), small kernel facts (`TaskState.prompt_seq`, the newest person
  message's seq), and each plugin's own state.
- A plugin that needs a fact from past a compaction keeps it in its own state as it happens
  (`tool_search`'s `loaded`, memory's `read`, written in the same append as the tool result),
  or reads the log where the need is rare (`noeta/plugins/compaction/transcript.py` refolds it
  for the saved transcript, following `history_from` into a source task's log).
- A continued task carries its source's compacted conversation and plugin states
  (`continue_state`), nothing older.
- No compatibility: the snapshot format changed (`noeta/core/snapshots.py` `FORMAT`); an older
  snapshot is refolded from the log (ADR-006: a snapshot may be missing, never wrong).

## Consequences

A snapshot stays within the compacted conversation plus plugin state (a 14-compaction test task:
5.9 KB, was 255 KB). The only per-call growth left is `TaskState.call_ids` (tens of bytes per
call), which keeps gateway-reused tool-call ids unique across the task. A plugin can no longer
scan old messages in memory; it records what it needs when it happens.

## Alternatives rejected

- Keep `history` and snapshot it incrementally (append-only chunks): still an in-memory list
  growing with the task, and a second copy of what the log already holds.
- Read the log on every step for the readers: a log read per model call on a long task.
