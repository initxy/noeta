# Truth is event-sourced: a small-payload EventLog plus a content-addressed ContentStore, with the Snapshot as an ordinary EventLog event

## Context

Task state must be rebuildable from the log alone — every wake, resume, reconnect and inspect folds the stream back into a `Task`. Two forces pull against that. Payloads are not uniformly small: one step of a long-running task carries tens of KB of tool output and a workspace snapshot runs to megabytes. And a worker leases one segment at a time, so a fold happens on every wake, where replaying a thousand events from scratch costs hundreds of milliseconds.

## Decision

Truth is split across two storage seams, and `fold(event_log, content_store, task_id) → Task` takes no other input.

- **EventLog** — an append-only stream of decisions, action intents and state changes. One payload is capped at `EVENT_PAYLOAD_MAX_BYTES` (4 KB), measured on its canonical bytes; every backend applies the cap on emit through the shared rule in `noeta.storage.spi`. Anything larger belongs in the ContentStore behind a `ContentRef`.
- **ContentStore** — content-addressed, immutable, hash-deduplicated storage for the bodies: LLM responses, tool results, fetched documents, context-plan and snapshot bodies. Refs are SHA-256 and dedup keys on the hash alone. The Protocol is `put` / `get` / `get_many` and nothing else — reclamation stays on the adapter, and existence is implicit in `get` raising. `get_many` is required rather than optional because the traversals that dominate read cost know every ref they will dereference before they process a single event.
- **A Snapshot is an EventLog event**, not a parallel mechanism: it carries a `state_ref` into the ContentStore body holding the serialized four-slice state, and fold starts from the highest-seq fold baseline, replaying only what follows. The baseline set is wider than the accelerator — conversation rewind, the crash-recovery seal and the history a forked conversation inherits re-base fold through the same lookup, so every member carries a rehydration body. The Engine snapshots before every suspend, before every terminal, and once consecutive tool-call iterations reach the mid-loop threshold without releasing the lease; the write is an ordinary lease-checked business emit, so a stale lease is rejected like any other.
- **Canonical serialization has one implementation.** `noeta.protocols.canonical` renders any typed value to stable bytes, and three consumers share it: the snapshot body, the ContentStore hash and the payload-cap measurement. A value round-trips only if it declares a canonical tag and registers a restorer; an optional field grown on an existing value is omitted when unset, so older recordings stay byte-equal.

## Rationale

- **The two layers exist so "the log is the truth" and "the log stays small" can both hold.** Inline bodies make a long task's stream unindexable and cannot carry a megabyte-scale snapshot at all; a mutable state row with an audit trail beside it makes the fold equation unavailable.
- **The snapshot is the compaction boundary that keeps fold viable under the lease model.**
- **Rooting the snapshot in the EventLog avoids a second authority.** As one event plus one body, its position in seq order settles which state is current and which write wins; a parallel snapshot store needs both answers defined separately.
- **The canonical single point is what keeps content hashes and snapshots stable.** Serializing around it drops the type tags, so the hash drifts and a snapshot no longer folds back to the value it was taken from. Every new typed field inherits that obligation.

## Alternatives considered

1. **A single-layer EventLog holding all content inline.** Weighed and rejected: a long-running task's stream bloats to the point of hurting backup, indexing and portability, and a megabyte-scale workspace snapshot fits under no workable envelope cap.
2. **State-first with events optional** — a mutable Task row plus an audit log. Rejected: state can no longer be derived from the log, which forfeits resume and snapshot rebuild.
3. **No snapshot, folding from scratch every time.** Rejected: combined with leasing one segment at a time, a long-running task becomes unusable.
4. **A separate snapshot table or service beside the EventLog.** Rejected: it adds a consistency dimension — which store is authoritative, which write wins — that seq order otherwise answers for free.
5. **The snapshot body inline in the event payload.** Rejected: it breaks the payload cap, and a megabyte state does not fit.
6. **Leaving batch reads out of the ContentStore Protocol**, letting shared code loop over `get`. Rejected: only an adapter can collapse a batch into one query, so a shared-code loop silently costs N round-trips at exactly the callsites that asked for a batch.

## Consequences

- The two-layer boundary and the serialization single point live in `noeta.protocols`; the fold and snapshot paths in `noeta.core`; the seams themselves in the in-memory reference backend and the durable adapters of the `storage` built-in, all applying the payload cap through `noeta.storage.spi`.
- Any value over 4 KB must be offloaded and any new typed value must register a canonical tag, or the round-trip or the cap check fails. That is the standing cost the two-layer model charges every new field.
- The fold-baseline event set and the SQL adapters' partial snapshot index must list the same types: a partial index is chosen only when its predicate matches the live query.
