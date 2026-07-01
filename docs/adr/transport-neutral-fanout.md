# Envelope fanout is transport-neutral: SSE and stdio-NDJSON are just consumers

## Context

There is a layer responsible for fanning out one envelope from a single publisher to N subscribers. This layer was originally named with an `Sse` prefix, but it never touches HTTP / socket / SSE framing / JSON — it only knows about `EventEnvelope`. Once a second consuming surface (the CLI live timeline, which has nothing to do with SSE) reused the same broadcaster, the `Sse` name became actively misleading — it implies the canonical fanout is nailed to one particular wire format, contradicting transport neutrality.

## Decision

The layer that fans out one envelope from a single publisher to N subscribers is **transport-neutral**, and its naming follows suit: `EnvelopeBroadcaster` (bounded fanout, knows only `EventEnvelope`) / `EventFanout` (an Observer that subscribes to the EventLog and forwards each envelope to the broadcaster) / `FanoutSubscription` (a single consumer's bounded-queue view).

- **Transport is a consumer, not part of the fanout**: SSE framing, socket writes, and the HTTP lifecycle live in the transport adapter (host layer). That adapter *is* the SSE wire, so it keeps the `Sse` naming. It subscribes to the broadcaster and iterates the subscription. A future stdio-NDJSON surface is a peer consumer: it subscribes to the same broadcaster and serializes each envelope into one line of NDJSON, with **zero change to the fanout layer**.
- **`EventEnvelope` is canonical; the wire format (SSE / NDJSON) is a projection, never the source of truth.**
- **Behavior unchanged**: the subscription's own-queue backpressure model (no broadcaster worker thread; a slow consumer is dropped during publish; publish never blocks the EventLog writer) and the architectural guard are preserved as-is — an AST guard asserts the fanout module does not import `http` / `socket` / `wsgiref`.

## Rationale

- **The `Sse` prefix lied about this layer's contract.** That module never touches HTTP / socket / SSE framing / JSON — it only knows about `EventEnvelope`. The layer boundary already put SSE framing in the host layer, not the fanout layer. Once a second surface (the CLI live timeline, with nothing to do with SSE) reused the same broadcaster, the `Sse` name became actively misleading — it implies the canonical fanout is nailed to one wire format, contradicting transport neutrality.
- **A transport-neutral name makes reuse honest.** The CLI timeline and a future stdio surface can both hang off `EnvelopeBroadcaster` without being implied to be "SSE-related" by an SSE-shaped name. This is the same reasoning as provider / transport neutrality: a single canonical, with the wire as a projection.

## Alternatives considered

1. **Keep the `Sse*` naming.** Rejected: it mis-names this layer, violates transport neutrality, and gets more wrong with every additional non-SSE consumer.
2. **Keep `Sse*` and add a parallel neutral abstraction alongside it.** Rejected: two shapes for one thing invites a second fanout implementation and a fork — exactly what the "single canonical envelope" principle forbids.
3. **Keep a deprecation alias.** Rejected: these are internal wiring, not a public API; a lingering alias would keep reviving the wrong mental model, so the old name is deleted outright.

## Consequences

- Load-bearing landing: `noeta.observers.fanout` (`EnvelopeBroadcaster` / `EventFanout` / `FanoutSubscription`; an AST guard asserts they do not import http/socket); the host layer's SSE transport adapter subscribes to the broadcaster, does the SSE framing, and keeps the `Sse` naming.
- This layer's naming evolution is consistent with the re-layering in `docs/adr/runtime-sdk-app-restructure.md`, but the layer boundary itself did not change: the fanout layer is still transport-neutral, and the wire is still a projection.
