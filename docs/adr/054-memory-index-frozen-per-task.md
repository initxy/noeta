# ADR-054: The memory index is frozen per task

- Status: accepted
- Date: 2026-09-25

## Context

Refines [ADR-028](028-unified-context-supply.md) (Resident context content rides one generic append-recorded channel, and the composer stays a read-only fold). Where the two disagree, this record wins.

## Decision

The memory index resident now records activate-once (`record_content(refresh=False)`), like the environment block. Before, the init hook refreshed it whenever the store changed (D9 in the archived `2026-08-04-claude-code-mechanism-alignment` spec), so a page written mid-task reached the index on the next turn — but the index lists pages most recently updated first, so every `memory_write` moved the semi-stable bytes, and every turn after it re-cached the prompt from the index block on: in a long session, the whole conversation.

D9's guarantee moves to the volatile suffix. A second `turn_intake` provider of the memory built-in, `memory_index_delta_provider`, compares the index the task shows (the bytes at the resident's active hash, resolved from the content store) with the live store's `index_snapshot()`, and records one `origin="system"` line after the goal naming what changed: `Memory index changed this task: +release-steps (Release steps), ~deploy-notes, -old-page` — `+` a new page with its description, `~` a page whose index line changed, `-` a page no longer in the store. A body-only rewrite that leaves the index line alone is not listed. The line is capped at 512 bytes, dropping the oldest changes into a closing count; it is silent when nothing changed and while the same line is still in the history the model sees. The comparison reads recorded state, never disk time, so the same log and store give the same line on resume, and the recorded line folds back without re-running the provider. Two limits follow from reading the snapshot's own lines: a page the snapshot listed by name only cannot show a changed description, and when the snapshot dropped pages outright a page it does not list is not claimed as new (`memory_search` finds it, as the snapshot's closing line says). Tier-1 recalled bodies keep their refresh — a rewritten page must stop contradicting its resident; only the index freezes.
