# Long-term memory is curated by a background consolidation agent that archives and never deletes

## Context

The long-term memory store is file-based, model-managed, and written during ordinary sessions (`unified-context-supply.md`). Left alone it only accumulates: duplicates pile up, and a fact that turns out to be wrong stays recallable forever. Curation is expensive — it reads across sessions and rewrites files — so it must not sit on the interactive path, and it must not be able to destroy anything.

## Decision

### Consolidation is an ordinary agent on an ordinary root task

Consolidation runs as a normal root-level task on the resident worker pool — no separate scheduler, no engine hook, no in-turn side effect. Its agent, `__consolidation__`, declares `tools=()` and activates only `memory`, so its entire tool surface is the memory pack; its goal carries a host-built digest of recent session activity. Every effect it has on the store goes through the same `memory_write` / `memory_archive` tools the interactive agent uses: one mutation surface, two callers.

### The trigger is a host-observed pause plus a debounce marker

An interactive session has no terminal event — it rests at a trailing next-goal `suspended`, and a close is an advisory marker. So the trigger hooks whatever seams a host has for observing a session pausing, and every one of them funnels into a single guard: read `.consolidation-state.json` in the memory root and proceed only when the last recorded run is older than the debounce threshold (24 hours by default). The marker is written at enqueue time, before the run is seeded, so a slow in-flight run debounces its own turn boundaries. The debounce makes any pause seam behave as "the first pause after the threshold" — periodic behaviour without a timer, and an idle deployment never wakes. A corrupt or missing marker reads as due, never as an exception on the trigger path.

### Retire by archiving, never delete

Neither the consolidation agent nor the interactive agent can destroy a memory. The heaviest operation either holds is `memory_archive`, which moves the file into `archive/` under the memory root, where the index's non-recursive glob, recall and search no longer see it but a human can inspect or restore it. Merging duplicates means writing the merged memory and archiving the originals. Physical deletion is a human act.

### The toggle is host configuration, not agent identity

The `memory` activation in an agent's `plugins` tuple is the memory master switch, and it is identity. Whether consolidation runs is not: the SDK exposes `run_consolidation` as the host-callable entry, with the marker helpers, the debounce guard and the digest builder exported separately for hosts that orchestrate their own schedule. This is the same layering as the workspace-instructions switch.

### Accepted weak consistency

- Consolidation and a live session may write the same store concurrently: file-per-memory keeps the blast radius to one file, last writer wins, and the consolidation agent re-reads before rewriting.
- A running session keeps the index resident it was seeded with; recall reads the store at call time, so it is always current, and a new session gets the new index. The `evolving` drift policy exists for exactly this.
- Consolidation never injects into any live session's context — it only touches disk. The append-only red line is untouched.

## Rationale

- **Reuse over invention.** A pause seam plus a marker file gives periodic background behaviour with zero scheduling machinery; the resident worker pool already drives a root task; the memory tools already confine writes to the store. The only genuinely distinct pieces are the digest builder and the trigger guard.
- **One mutation surface keeps the safety argument small.** Because consolidation can act only through the slug-confined memory tools, "what can a bad consolidation run do?" reduces to "what can any memory-enabled agent do" — and archive-not-delete bounds that to reversible operations.
- **Write-side curation, read-side simplicity.** Recall and search stay cheap and deterministic because the background pass keeps the store small and current — the same reasoning that puts compaction on the write side of the ledger.
- **An enqueue-time marker favours under-triggering.** Between "might skip a day when a run fails" and "might storm-enqueue while a run is slow", the former is benign for a curation job.

## Alternatives considered

1. **Timer- or cron-driven consolidation.** Rejected: it needs a resident scheduling registration and wakes idle deployments, for no behavioural gain over a debounced pause trigger.
2. **Synchronous extraction on the message path.** Rejected: it adds LLM latency and cost to every exchange, and duplicates a decision the interactive agent already makes with `memory_write` under its memory policy prompt.
3. **Consolidation as an engine or runtime feature.** Rejected: the runtime is single-task and must stay neutral; reading across sessions and deciding when to curate is host business, matching where recall injection already lives.
4. **Letting consolidation delete files.** Rejected: an LLM curator will sometimes be wrong, and archiving keeps every mistake reversible at the cost of one subdirectory.
5. **An in-turn background subagent as the vehicle.** Rejected: those are children of a live session and die with it, whereas consolidation is a standalone root concern.

## Consequences

- The digest builder, marker helpers and run entry point live in `noeta.client.consolidation`; the agent definition and its curation prompt live in `noeta.presets`. The runtime is unchanged.
- The reserved `__` prefix on the agent name keeps it out of a parent's spawnable roster and out of a host's advertised agent list: resolvable for a host-seeded root task, never model- or user-selectable.
- The digest is capped by session count and per-session characters, tail-truncated, and states its window and both caps in its own header, so the consolidation agent never mistakes a window for the whole history.
- A multi-tenant host scopes one pass per store: filter the digest to that tenant's root sessions, point the memory root at that tenant's directory so the per-root marker debounces it independently, and bind the seeded task to the same root before any worker can claim it.
