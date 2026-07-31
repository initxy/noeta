# Envelope fan-out knows only `EventEnvelope`; every wire format is a consumer of it

## Context

One publisher — the EventLog — has to reach N live subscribers: an SSE stream in
an embedding host, a stdio writer, a live timeline in a terminal. Each wants a
different wire format, but they all want the same thing from the layer
underneath: every envelope, in order, without any one slow reader stalling the
writer that produced it.

## Decision

The fan-out layer is transport-neutral, and its naming says so:
`EnvelopeBroadcaster` (bounded fan-out that knows only `EventEnvelope`),
`EventFanout` (an Observer that subscribes to the EventLog and forwards each
envelope to the broadcaster), and `FanoutSubscription` (one consumer's
bounded-queue view).

- **A transport is a consumer, not a layer.** Wire framing, socket writes and
  protocol lifecycle live in a host-layer adapter that subscribes to the
  broadcaster and iterates its subscription. A stdio-NDJSON surface is a peer
  consumer that serializes each envelope into one line, and it costs the fan-out
  layer no change at all.
- **`EventEnvelope` is canonical; a wire format is a projection of it**, never a
  source of truth.
- **Each subscription owns its bounded queue.** The broadcaster runs no worker
  thread: publishing walks the subscription list under a briefly held mutex and
  enqueues without blocking, closing and dropping any subscription whose queue is
  full or already shut. A slow consumer loses its stream; the publisher never
  waits on it.
- **The Observer never raises back into the writer.** Observer callbacks fire
  after commit and outside the EventLog writer lock, so a broadcaster failure is
  logged and the envelope dropped rather than propagated.

## Rationale

- **A transport-shaped name would misstate the contract.** This layer touches no
  HTTP, socket, framing or JSON; it only knows `EventEnvelope`. Naming it after
  one wire format implies the canonical fan-out is nailed to that format, and
  gets more wrong with every consumer that is not that format. A neutral name
  lets a terminal timeline and a stdio surface both hang off the broadcaster
  without being implied to belong to some other protocol — the same shape as
  provider and transport neutrality elsewhere: one canonical form, the wire a
  projection of it.

## Alternatives considered

1. **Name and shape the fan-out around one wire format.** Rejected: it misstates
   what the layer knows, contradicts transport neutrality, and degrades with each
   non-matching consumer added.
2. **A transport-shaped fan-out with a neutral abstraction beside it.** Rejected:
   two shapes for one thing invites a second fan-out implementation and a fork,
   which is exactly what a single canonical envelope forbids.
3. **Transport-named aliases pointing at the neutral types.** Rejected: these are
   internal wiring, not public API, and an alias keeps reviving the wrong mental
   model.
4. **Give the broadcaster a worker thread that calls consumer callbacks.**
   Rejected: a slow socket write would then stall the publisher. Per-subscription
   bounded queues plus drop-on-full keep the cost of a slow consumer local to
   that consumer.

## Consequences

- The three types live in `noeta.observers.fanout`; a host-layer transport
  adapter owns its own framing and subscribes to the broadcaster.
- Backpressure is a drop, not a stall: a consumer that cannot keep up is closed,
  and reconnecting is its own problem to solve.
