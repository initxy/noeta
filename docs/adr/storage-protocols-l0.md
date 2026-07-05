# The typed boundaries of the three storage seams live at L0, with capability splitting and system_emit

## Context

EventLog / ContentStore / Dispatcher are Noeta's three most important seams. Early on, each consumer wrote a local structural Protocol (something like `_EventLog`) in its own module; this worked only because InMemory happened to satisfy every slice at once — the moment you swapped backends, the types no longer revealed who reads and who writes. This was a hidden landmine: read/write permissions were invisible, and import-linter couldn't precisely pin down who used the write permission.

## Decision

The typed boundaries of EventLog / ContentStore / Dispatcher are **L0 Protocols** (`noeta/protocols/event_log.py` / `content_store.py` / `dispatcher.py`), not local structural Protocols each consumer writes itself.

- **EventLog is capability-split**: into `EventLogReader` (reads + `find_latest_snapshot`) / `EventLogWriter` (business writes + system writes) / `EventLogSubscriber`, plus the combined alias `EventLog`. A read-only consumer (fold / cursor) takes only the Reader in its signature, so write permission is visible in the types.
- **Two write paths**: `emit(...)` is the business write, with three layers of concurrency protection (optimistic lock `expected_seq` / lease / idempotency key `idempotency_key`); `system_emit(...)` is a cross-stream system write, with no lease check, no idempotency, no expected_seq. **Cross-stream writes are promoted from a `bypass_lease` flag to a dedicated method** — the `bypass_lease` field is removed permanently, and all genesis `TaskCreated` / subtask `TaskCreated` / observer writes to the parent stream go through `system_emit`.
- **Narrowing the reverse dependency on Dispatcher**: a `LeaseRegistry` Protocol containing only the single method `is_lease_valid` is split off from Dispatcher, serving as the **only** point of reverse dependency from EventLog to Dispatcher. InMemoryDispatcher implements `Dispatcher + LeaseRegistry` in one class, at zero code cost.
- **ContentStore has only `put + get`** (no `delete / list`); Dispatcher's debug helpers (`task_status` / `wake_on` / `suspend_reason`) **do not** enter the Protocol.
- **Isolation is enforced by the `storage-adapters-isolated` contract in `.importlinter`**: every kernel layer is forbidden from importing `noeta.storage`; production code imports a concrete adapter in 0 places, and only tests inject InMemory.

## Rationale

- **Local structural Protocols are a hidden landmine.** Each consumer writing its own `_EventLog` Protocol in its own module survives only because InMemory satisfies every slice at once — swap the backend and the types no longer reveal who reads and who writes. Raising it to L0 + capability splitting lets import-linter precisely pin down which module uses the write permission, and bakes fold / cursor / shadow's read-only constraint into their signatures.
- **Capability splitting makes swapping backends feasible.** Implementing `subscribe` is expensive (InMemory uses an inline synchronous callback; Sqlite needs LISTEN/NOTIFY); a read-only consumer that takes only the Reader isn't dragged down by that cost. A backend implements only the capabilities it needs.
- **`system_emit` replaces the `bypass_lease` flag because the flag's semantics were too weak.** "Set a boolean to bypass the lease" hides "this is a system write" inside a parameter; a dedicated method promotes it to a first-class operation where the signature is the documentation — a system writer must explicitly declare its identity (`actor`) and role (`origin`, see `docs/adr/event-origin-marker.md`).
- **`LeaseRegistry` narrows the reverse dependency to a single method.** EventLog needs to ask Dispatcher "is this lease still valid," but shouldn't depend on the entire Dispatcher in return. Narrowing to the single method `is_lease_valid` minimizes the reverse-coupling surface.

## Alternatives considered

1. **A fat monolithic EventLog Protocol** (read / emit / subscribe / system_emit all stuffed into one Protocol). Rejected: the read-only constraint disappears from the signature; the Sqlite backend is forced to implement the costly `subscribe` capability; import-linter loses the ability to precisely pin down the write permission.
2. **Keep the `bypass_lease` flag and skip `system_emit`** (Protocol maps one-to-one to InMemory). Rejected: filtering cross-stream writes by the actor string has proven fragile; a "temporary" `bypass_lease` field will have to be replaced eventually anyway — clean it out in one shot.
3. **Split `find_latest_snapshot` into a separate `SnapshotIndex` Protocol** / **also put Dispatcher's debug helpers into the Protocol**. Rejected: the former makes fold + shadow take one more argument without gaining a new object (a snapshot is itself a first-class EventLog event, so keeping it in the Reader is semantically aligned, see `docs/adr/event-sourced-truth.md`); the latter would encourage "ask the dispatcher for state," violating "the EventLog is the single source of truth."

## Consequences

- Where this bears weight: `noeta.protocols.event_log` / `content_store` / `dispatcher` are the L0 Protocols of the three seams (including `EventLogReader/Writer/Subscriber`, `LeaseRegistry`, `Lease`); `noeta.storage.memory` is the InMemory adapter (one class implementing `Dispatcher + LeaseRegistry`), `noeta.storage.sqlite.*` is the Sqlite adapter, `noeta.storage.postgres.*` is the Postgres adapter (psycopg; MVCC replaces sqlite's file-wide `BEGIN IMMEDIATE` writer lock with transaction-scoped advisory locks — per task stream for the EventLog, one global lock for the Dispatcher state machine), all isolated by `storage-adapters-isolated`; every `system_emit` call site (Engine genesis + cross-stream child `TaskCreated`, `ChildLifecycleObserver`, and the control-plane markers in the execution layer's `noeta.execution.subtask_drain` / `noeta.execution.driver`) is where a system write happens.
- Constraints: no kernel layer may import `noeta.storage`, only the L0 Protocol; a new system write must go through `system_emit` and explicitly declare `actor` and `origin`; a read-only consumer takes only the Reader in its signature.
