# Memory consolidation: background rewriting of the long-term memory store

## Context

Memory v1 (see `docs/adr/unified-context-supply.md`, "Memory v1, four parts") gave the agent a file-based long-term memory with model-managed write/read tools, a resident index, and auto-recall. What it deliberately left out is any process that maintains the store over time: memories only accumulate, duplicates pile up, and facts that later turn out to be wrong stay recallable forever. The industry pattern that emerged for this (OpenAI's background memory rewriting, Claude Code's transcript-driven memory maintenance, Letta's sleep-time compute) is an **asynchronous background pass** that reads recent activity and rewrites the memory store — expensive curation moved off the interactive path.

Memory v2 (spec: `docs/implementation-specs/2026-07-10-memory-v2.md`) adds that pass, plus the primitives it needs (`memory_search`, `memory_archive`, frontmatter). This ADR records the long-term trade-offs: when consolidation runs, what runs it, what it is allowed to do, and which weak-consistency effects we accept.

## Decision

### Consolidation is an ordinary agent on an ordinary root task

Consolidation runs as a normal root-level task on the resident worker pool — not a new scheduler, not an engine hook, not an in-turn side effect. Its agent (`__consolidation__`, an `AgentDefinition` with `tools=()` and `Capabilities(memory=True)`) sees exactly the memory tool pack and nothing else; its goal carries a host-built digest of recent session activity. All of its effects on the store go through the same `memory_write` / `memory_archive` tools the interactive agent uses — one mutation surface, two callers.

### Trigger: session-stop seams with a debounce marker, no new scheduling

There is no terminal event for an interactive session (it rests at a trailing next-goal `suspended`; `close` is an advisory marker). So the trigger hooks the two existing seams where the host observes a session pausing — the explicit close cascade and the turn-boundary (drive completion back to suspended) — and both funnel into one guard: read `.consolidation-state.json` in the memory root, and only proceed when the last run is older than the debounce threshold (default 24h). The marker is written at enqueue time, so a slow consolidation run cannot be re-triggered while in flight. Because the marker only lands after the digest build, the backend also serializes passes with a non-blocking in-process lock: two near-simultaneous session stops (parallel workers, or close landing next to a turn boundary) cannot both read the stale marker and double-enqueue — the loser drops its attempt. The debounce makes the turn-boundary hook equivalent to "the first turn boundary after the threshold" — periodic behavior without a timer, and an idle deployment never wakes.

### Retire by archiving, never delete

Neither the consolidation agent nor the interactive agent can destroy a memory. The heaviest operation either holds is `memory_archive`: move the file into `archive/` under the memory root, where the index's non-recursive glob, recall, and search no longer see it, but a human can inspect or restore it. Merging duplicates = write the merged memory, archive the originals. Physical deletion is reserved for humans.

### The toggle is host configuration, not agent identity

`Capabilities.memory` remains the memory master switch (agent identity). Consolidation is a **product behavior**: the served backend gets a `memory_consolidation` config (default on when memory is on, env-off), and the SDK exposes an explicit entry point for hosts that orchestrate their own runs — the same layering as the instructions-file switch (`instructions_enabled`).

### Accepted weak consistency

- Consolidation and a live session may write the same store concurrently: file-per-memory keeps the blast radius to one file, last-writer-wins, and the consolidation agent re-reads before rewriting.
- A running session keeps its wiring-time index snapshot after a consolidation pass (the `evolving` drift policy exists exactly for this); recall reads the store at call time, so it is always current; new sessions get the new index.
- Consolidation never injects into any live session's context — it only touches disk. The append-only red line is untouched.

## Rationale

- **Reuse over invention.** Session-stop seams + a marker file give periodic background behavior with zero new scheduling machinery; the resident worker pool already knows how to drive a root task; the memory tools already confine writes to the store. The only genuinely new pieces are the digest exporter and the trigger guard.
- **One mutation surface keeps the safety argument small.** Because consolidation can only act through the slug-confined memory tools, "what can a bad consolidation run do?" reduces to "what can any memory-enabled agent do" — and archive-not-delete bounds that to reversible operations.
- **Write-side curation, read-side simplicity.** Recall and search stay cheap and deterministic because the background pass keeps the store small and current — the same reasoning that put compaction on the write side of the ledger.
- **Enqueue-time marker favors under-triggering.** Between "might skip a day when a run fails" and "might storm-enqueue while a run is slow," the former is benign for a curation job.

## Alternatives considered

1. **Timer/cron-driven consolidation.** Rejected: needs a resident scheduling registration, wakes idle deployments, and adds machinery for no behavioral gain over debounced session-stop triggering.
2. **Synchronous extraction on the message path (Mem0-style pipeline).** Rejected: adds LLM latency and cost to every exchange, and duplicates a decision the interactive agent already makes with `memory_write` under the policy prompt.
3. **Consolidation as an engine/runtime feature.** Rejected: the runtime is single-task and must stay neutral; reading across sessions and deciding when to curate is host business (SDK + product layers), matching where recall injection already lives.
4. **Letting consolidation delete files.** Rejected: an LLM curator will sometimes be wrong; archive keeps every mistake reversible at the cost of a subdirectory.
5. **In-turn background subagent as the vehicle.** Rejected: those are children of a live session and die with it; consolidation is a standalone root concern.

## Consequences

- The digest exporter, marker helpers, and run entry point live in the SDK (`noeta.client.consolidation`); the agent definition and its prompt live in `noeta.presets`; the trigger guard and config live in the noeta-agent backend. The runtime is unchanged.
- The registry advertises agents to the product's agent list, so reserved (`__`-prefixed) names are filtered at the backend's `agent_names()` seam; `__consolidation__` is resolvable but not user-selectable.
- The backend needs the resolved memory root for the marker; the SDK exposes it (`memory_root()`), mirroring the recall-context precedence (`memory_dir` > `global_memory_dir` > default).
- Digest size is capped (sessions × per-session token budget); what was dropped is stated in the digest so the consolidation agent never mistakes a window for the whole history.
