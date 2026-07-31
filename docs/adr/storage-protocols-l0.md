# The three storage seams are typed at L0, split by capability, with a dedicated system-write path

## Context

EventLog, ContentStore and Dispatcher are the three seams every other module talks through. Their typed boundary decides two things at once: whether a backend other than the in-memory reference can be substituted at all, and whether the type of a callsite reveals what that callsite may do with the seam.

## Decision

The typed boundaries live in `noeta.protocols` — `event_log`, `content_store`, `dispatcher` — as L0 Protocols that depend on nothing else in the project.

- **EventLog is split by capability.** `EventLogReader` (read plus the fold-baseline lookup), `EventLogWriter`, `EventLogSubscriber` and `EventLogTaskIndex` are separate Protocols, with combined aliases for the few callsites that genuinely need several at once. A read-only consumer such as fold or the LLM cursor takes only the Reader, so write authority is visible in the signature. Enumeration is deliberately not implied by reading: holding one task's stream must not confer the ability to list every task.
- **Two write paths, distinguished by method rather than by flag.** `emit` is the business write, with three layers of concurrency protection — lease validity, the `expected_seq` optimistic lock and idempotency-key retry dedup. `system_emit` is the cross-stream system write: no lease check, no idempotency, no expected sequence, and both `actor` and `origin` are required, so a system writer must declare its identity and its role. Genesis and child task creation, observer writes onto a parent stream, and the execution layer's control-plane markers all go through it.
- **The reverse dependency on Dispatcher is one method wide.** `LeaseRegistry` exposes only lease validation and is the sole edge from EventLog back to Dispatcher; a dispatcher satisfies both Protocols on one class at no extra cost.
- **The Protocols stay minimal.** ContentStore is `put` / `get` / `get_many`; Dispatcher's debug helpers — task status, wake condition, suspend reason — stay off its Protocol.
- **Isolation is mechanical.** An import contract forbids the kernel bands from importing `noeta.storage` at all; they see only the L0 Protocols. The durable sqlite and postgres backends live in the `storage` built-in plugin, fenced off by the universal contract that nothing statically imports `noeta.builtins`. The SDK assembly boundary is the one place that names a concrete backend, and it injects it.
- **Shared backend rules have a public entry.** `noeta.storage.spi` fronts the rules every backend must apply — typed payload restore, the payload cap, the stale-reclaim terminal decision and wake matching. The in-memory reference backend and the durable adapters route through the same facade, so an external backend author needs only `noeta.protocols` plus this module.

## Rationale

- **A capability split is what makes a backend substitutable.** Implementing subscription is expensive — an in-memory backend can notify inline and synchronously, a SQL backend cannot — and a consumer that only reads should not pay for it. Separate Protocols let each backend implement exactly the capabilities it offers, and let the import contracts pin down which module holds write authority.
- **A method is documentation; a boolean is not.** "Set a flag to skip the lease" hides the fact that a write bypasses concurrency control inside an argument, and recovering the distinction afterwards by inspecting an attribution string is fragile. A dedicated method makes the system write a first-class operation whose signature forces the caller to say who is writing and in what role.
- **Narrowing the reverse edge keeps the seams from fusing.** EventLog genuinely needs to ask whether a lease is live; depending on the whole Dispatcher to ask would make the two inseparable.
- **A minimal ContentStore keeps reclamation out of the contract.** Refs stay reachable as long as a fold or resume needs them; encoding deletion into the Protocol forces every backend to answer a lifetime question that belongs to the deployment.

## Alternatives considered

1. **Local structural Protocols, each consumer declaring the slice it uses in its own module.** Weighed and rejected: it type-checks only as long as one adapter happens to satisfy every slice, so the moment a backend is substituted the types stop revealing who reads and who writes, and the import contracts lose any handle on write authority.
2. **One fat EventLog Protocol** carrying read, emit, subscribe, system write and enumeration together. Rejected: the read-only constraint disappears from every signature, a SQL backend is forced to implement the costly subscription capability, and enumeration becomes an implicit consequence of being able to read one stream.
3. **A boolean lease-bypass flag on `emit` instead of a separate system-write method**, keeping the Protocol a one-to-one mirror of the in-memory backend. Rejected: the flag makes the most dangerous write path the least visible one.
4. **A separate snapshot-index Protocol** split out of the reader. Rejected: a snapshot is itself an ordinary EventLog event, so looking one up is a read; splitting it gives fold and its neighbours one more argument to thread without producing a genuinely separate capability.
5. **Putting Dispatcher's debug helpers on the Protocol.** Rejected: it invites callers to ask the dispatcher for task state, which contradicts the EventLog being the source of truth.

## Consequences

- `noeta.protocols.event_log`, `content_store` and `dispatcher` hold the three seams, the capability split, `LeaseRegistry` and `Lease`; `noeta.storage.memory` is the in-memory reference backend and the executable definition of the Protocols' semantics; `noeta.storage.spi` is the shared-rule facade; the sqlite and postgres adapters in the `storage` built-in import nothing beyond the Protocols and that facade, which is the standing proof that the facade suffices for an external author.
- No kernel module may import `noeta.storage`; a read-only consumer takes only the Reader; any new system write goes through `system_emit` with an explicit actor and origin.
- The durable backends resolve write contention differently — a file-wide immediate-transaction writer lock against transaction-scoped advisory locks, one per task stream for the log and one global for the dispatcher state machine. Absorbing that kind of divergence is what the capability split and the shared SPI are for.
