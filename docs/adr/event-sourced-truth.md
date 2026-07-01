# Truth is event-sourced: a two-layer EventLog + ContentStore, with the Snapshot as a first-class EventLog event

## Context

Noeta's core promise is "the EventLog is the single source of truth, and task state can always be folded back from the log." But the EventLog can't hold large objects: a single step in a long-running task can reach tens of KB, and a Workspace snapshot can reach several MB. At the same time, under the "a worker leases one segment at a time" model (see `docs/adr/worker-lease-model.md`), every wake / SSE reconnect / resume / inspect requires a fold, and folding a thousand-event task purely from scratch takes hundreds of milliseconds—unusable. How truth is stored and how rebuilding is accelerated are two sides of the same problem.

## Decision

Noeta splits the source of truth into two layers, and `fold(EventLog, ContentStore) → Task` depends on no other external IO:

- **EventLog**: an immutable, append-only stream of decisions, action intents, and state changes. A single event payload is **≤ 4 KB** (a protocol-level hard constraint).
- **ContentStore**: content-addressed, immutable, hash-deduplicated storage for large objects (LLM response bodies, tool result bodies, provider-fetched documents, Snapshot bodies, ContextPlan bodies). Any object over 4 KB **must** be offloaded to the ContentStore and referenced by a `ContentRef`.

**A Snapshot is not a separate mechanism** but a special EventLog event `TaskSnapshot`, whose payload carries only `state_ref: ContentRef` pointing to the serialized full Task state in the ContentStore. A fold starts from the most recent snapshot and folds only the events after it. A Snapshot **must** be written before suspend, before terminal, and when a continuous tool loop exceeds 20 steps; the write reuses the EventLog's `expected_seq` optimistic lock, so a stale-lease snapshot write is rejected.

**Canonical serialization is a single-point invariant**: turning any Noeta-shape typed value into stable bytes goes **only** through `noeta/protocols/canonical.py:to_canonical_bytes()`. Three downstream consumers reuse the same implementation: (1) the Snapshot body into the ContentStore; (2) the ContentStore content hash; (3) the EventLog's 4 KB size check. For a typed value to round-trip, it must be registered via `__canonical_tag__` + `register()`.

## Rationale

- **The EventLog can't hold large objects, yet state must still fold out of events.** A single-layer EventLog as truth explodes: tens of KB per step in a long-running task, several MB for a Workspace snapshot. Conversely, a "state-first, events-optional" mutable Task row makes "fold state back from the log" impossible. Two layers store the authoritative decision stream and the large objects separately—holding the 4 KB limit while keeping the log the basis for state rebuild.
- **Without snapshots, fold can't keep up.** Under the "a worker leases one segment at a time" model, every wake / SSE reconnect / resume / inspect requires a fold; folding a long-running thousand-event task purely from scratch takes hundreds of milliseconds each time—unusable. The Snapshot is the "compaction boundary" that lets fold start from the most recent snapshot.
- **The Snapshot must reuse the EventLog rather than build its own storage**, or it adds a consistency dimension (who is authoritative—the snapshot or the EventLog; who wins a concurrent write). Making it "one EventLog event + one ContentStore body" naturally roots authority in the EventLog's seq order.
- **The canonical single point is the root of content-hash and snapshot stability.** Bypassing `to_canonical_bytes` for `dataclasses.asdict` or hand-written JSON loses the type tags, causing the ContentStore hash to drift and the snapshot round-trip to fail (the same typed value no longer folds back to the original). Any newly-added typed protocol field (e.g. the Block subclasses fixed by `docs/adr/provider-neutral.md`) must obey this.

## Alternatives considered

1. **The EventLog as the single source of truth** (stuffing all content into event payloads). Rejected: the EventLog of a long-running task bloats, hurting migration/backup/indexing; and a several-MB Workspace snapshot simply won't fit.
2. **State-first, events-optional** (a mutable Task row + an optional audit log). Rejected: state can no longer fold back from the log, conflicting with the founding core promise "the EventLog is the single source of truth."
3. **No snapshot, fold from scratch every time.** Rejected: layered on "lease one segment at a time," a long-running task becomes outright unusable.
4. **Put the Snapshot in a separate table/service** (stored in parallel to the EventLog). Rejected: adds a consistency dimension—who is authoritative and who wins a concurrent write both need separate definition.
5. **Stuff the Snapshot body directly into the event payload.** Rejected: violates the 4 KB limit; a several-MB state won't fit.

## Consequences

- Load-bearing landings: `noeta.protocols.content_store`, `noeta.protocols.canonical`, `noeta.protocols.event_log` are the two-layer protocol proper plus the canonical single point; `noeta.core.fold`, `noeta.core.snapshot`, `noeta.runtime.compaction` are the fold / snapshot / compaction paths; `noeta.storage.sqlite.eventlog`, `noeta.storage.memory` are the backend implementations, where the 4 KB size check and ContentStore offload land.
- Constraint: any object over 4 KB must be offloaded, and a new typed value must register a canonical tag, or round-trip / the size check will fail—this is the two-layer model's hard requirement for every new field.
